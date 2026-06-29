"""
AIピッチイベント + スタートアップ補助金 クローラー
信頼ソース固定（LLMなし・新規APIキーなし）→ Notion DB → Discord 通知

実行方法:
  env未設定時 → dry-run（標準出力のみ）
  env設定時   → Notion登録 + Discord通知

環境変数:
  NOTION_TOKEN               Notion APIトークン（既存kkj-crawlerと共用）
  NOTION_DATABASE_ID_PITCH   ピッチイベント専用NotionデータベースID（新規）
  NOTION_DATABASE_ID_SUBSIDY スタートアップ補助金専用NotionデータベースID（新規）
  DISCORD_WEBHOOK_URL        Discord Webhook URL（既存kkj-crawlerと共用）

ピッチ側と補助金側は別Embed・別Notion DBで出力する。
NOTION_DATABASE_ID_SUBSIDYが未設定の場合、補助金取得・通知はスキップ。
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timezone
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID_PITCH = os.environ.get("NOTION_DATABASE_ID_PITCH", "")
NOTION_DATABASE_ID_SUBSIDY = os.environ.get("NOTION_DATABASE_ID_SUBSIDY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

DRY_RUN = not (NOTION_TOKEN and DISCORD_WEBHOOK_URL)

MAX_ITEMS_DISCORD = 3
CRAWL_DELAY = 2  # ソース間のsleep秒数

# ---------------------------------------------------------------------------
# ピッチ用フィルタキーワード定義
# ---------------------------------------------------------------------------

AI_KEYWORDS = [
    "AI", "人工知能", "生成AI", "LLM", "機械学習", "GenAI",
    "ジェネレーティブ", "深層学習", "ディープラーニング", "自然言語処理",
    "ChatGPT", "AGI", "GPT",
]

PITCH_KEYWORDS = [
    "ピッチ", "pitch", "PITCH",
    "デモデイ", "demo day", "Demo Day", "DemoDay",
    "アクセラレータ", "accelerator", "Accelerator",
    "ピッチコンテスト", "ピッチイベント",
    "LaunchPad", "Launchpad", "LAUNCHPAD",
    "インキュベータ", "Incubate Camp", "incubate camp",
    "スタートアップ応募", "スタートアップ募集", "スタートアップ採択",
    "起業家募集", "エントリー受付", "応募受付", "参加募集",
]

PITCH_EXCLUDE_KEYWORDS = [
    "開催レポート", "開催報告", "開催しました", "終了しました",
    "募集終了", "受付終了", "満員御礼", "締め切りました", "締切ました",
    "締切りました", "終了のお知らせ", "過去のイベント",
    "開催結果", "登壇しました", "登壇報告", "ご報告",
    "審査結果", "採択結果", "受賞", "優勝", "グランプリ",
    "結果発表", "決定しました", "決定！",
]

# ---------------------------------------------------------------------------
# 補助金用フィルタキーワード定義
# ---------------------------------------------------------------------------

SUBSIDY_PASS_KEYWORDS = [
    "スタートアップ", "創業", "新規事業", "AI", "DX", "デジタル",
    "人工知能", "起業", "ベンチャー", "イノベーション", "研究開発",
    "ディープテック",
]

SUBSIDY_EXCLUDE_KEYWORDS = [
    "施設整備", "農業", "漁業", "介護報酬", "介護施設", "介護保険",
    "保育", "給食", "産業廃棄物", "廃棄物", "除排雪", "道路",
    "下水道", "水道事業", "除草", "害虫", "警備", "搬送",
    # エネルギー・インフラ系（スタートアップ文脈でないもの）
    "CO2削減", "CO₂", "二酸化炭素", "水力発電", "再エネ", "再生可能エネルギー",
    "低炭素", "省CO2", "省エネ", "電動化", "太陽光",
    # 地下・インフラ系
    "地下埋設", "ライフライン", "埋設物",
    # 医療・福祉系
    "診療所", "医療機関", "医療ＤＸ", "医療DX",
    # 不動産・企業立地
    "企業立地", "賃貸借型",
    # 保証料・利子系（補助内容が金融のみで事業支援でないもの）
    "信用保証料", "利子補給",
    # 海外ODA系
    "グローバルサウス", "ASEAN",
]

# ---------------------------------------------------------------------------
# 和暦変換ユーティリティ
# ---------------------------------------------------------------------------

WAREKI_MAP = {
    "令和": 2018,
    "平成": 1988,
    "昭和": 1925,
}

ZEN_TO_HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def _zen_to_han(s: str) -> str:
    return s.translate(ZEN_TO_HAN)


def _wareki_to_year(era: str, year_str: str) -> Optional[int]:
    base = WAREKI_MAP.get(era)
    if base is None:
        return None
    y = int(year_str) if year_str != "元" else 1
    return base + y


_RE_WAREKI_DATE = re.compile(
    r"(令和|平成|昭和)\s*([元\d０-９]+)\s*年\s*(\d{1,2}|[０-９]{1,2})\s*月\s*(\d{1,2}|[０-９]{1,2})\s*日"
)
_RE_WESTERN_DATE = re.compile(
    r"(20\d{2})\s*[年/\-]\s*(\d{1,2})\s*[月/\-]\s*(\d{1,2})\s*日?"
)


def extract_deadline(text: str) -> Optional[date]:
    """テキストから締切日を best-effort で抽出する。"""
    if not text:
        return None
    text = _zen_to_han(text)
    candidates: list[date] = []

    for m in _RE_WAREKI_DATE.finditer(text):
        era, y_str, m_str, d_str = m.groups()
        year = _wareki_to_year(era, _zen_to_han(y_str))
        if year:
            try:
                candidates.append(date(year, int(_zen_to_han(m_str)), int(_zen_to_han(d_str))))
            except ValueError:
                pass

    for m in _RE_WESTERN_DATE.finditer(text):
        try:
            candidates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass

    if not candidates:
        return None

    today = date.today()
    future = [d for d in candidates if d >= today]
    if future:
        return min(future)
    return max(candidates)


# ---------------------------------------------------------------------------
# ピッチ用フィルタロジック
# ---------------------------------------------------------------------------

def _contains_any(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    for kw in keywords:
        if kw.lower() in lower:
            return True
    return False


def passes_pitch_filter(title: str, description: str) -> bool:
    """
    ピッチイベント通過条件:
      - タイトルが除外キーワードを含まない
      - AI関連キーワードを含む（タイトル or 本文）
      - ピッチ系キーワードを含む（タイトル or 本文）
    除外判定はタイトルのみで行う（本文中の「結果発表」等で募集記事を落とさないため）。
    """
    if _contains_any(title, PITCH_EXCLUDE_KEYWORDS):
        return False
    combined = f"{title} {description}"
    return _contains_any(combined, AI_KEYWORDS) and _contains_any(combined, PITCH_KEYWORDS)


# ---------------------------------------------------------------------------
# 補助金用フィルタロジック
# ---------------------------------------------------------------------------

def passes_subsidy_filter(title: str, description: str) -> bool:
    """
    補助金通過条件:
      - 除外キーワードを含まない（タイトルのみで判定）
      - 通過キーワードを含む（タイトル or 説明）
    """
    if _contains_any(title, SUBSIDY_EXCLUDE_KEYWORDS):
        return False
    combined = f"{title} {description}"
    return _contains_any(combined, SUBSIDY_PASS_KEYWORDS)


# ---------------------------------------------------------------------------
# ピッチデータソース取得関数
# ---------------------------------------------------------------------------

def fetch_onlab_rss() -> list[dict]:
    """
    Open Network Lab 公式RSSフィード
    URL: https://onlab.jp/feed/
    方式: feedparser (RSS)
    robots.txt: 管理画面のみDisallow、一般クロール許可
    """
    url = "https://onlab.jp/feed/"
    print(f"  [onlab] RSSフィード取得中: {url}")
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        content = ""
        if hasattr(entry, "content"):
            content = entry.content[0].value if entry.content else ""
        full_text = f"{summary} {content}"
        pub_date_str = entry.get("published", "")
        results.append({
            "title": title,
            "url": link,
            "description": full_text,
            "pub_date": pub_date_str,
            "source": "onlab",
            "category": "pitch",
        })
    print(f"  [onlab] {len(results)}件取得")
    return results


def fetch_techwave_rss() -> list[dict]:
    """
    TechWave（スタートアップ・技術系メディア）RSSフィード
    URL: https://techwave.jp/feed/
    方式: feedparser (RSS)
    robots.txt: wp-adminのみDisallow、一般クロール許可
    """
    url = "https://techwave.jp/feed/"
    print(f"  [techwave] RSSフィード取得中: {url}")
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        pub_date_str = entry.get("published", "")
        results.append({
            "title": title,
            "url": link,
            "description": summary,
            "pub_date": pub_date_str,
            "source": "techwave",
            "category": "pitch",
        })
    print(f"  [techwave] {len(results)}件取得")
    return results


def fetch_01booster_rss() -> list[dict]:
    """
    01Booster（スタートアップ支援・アクセラレータ運営）RSSフィード
    URL: https://www.01booster.co.jp/feed/
    方式: feedparser (RSS)
    robots.txt: wp-adminのみDisallow、一般クロール許可
    """
    url = "https://www.01booster.co.jp/feed/"
    print(f"  [01booster] RSSフィード取得中: {url}")
    feed = feedparser.parse(url)
    results = []
    for entry in feed.entries:
        title = entry.get("title", "")
        link = entry.get("link", "")
        summary = entry.get("summary", "") or entry.get("description", "")
        pub_date_str = entry.get("published", "")
        results.append({
            "title": title,
            "url": link,
            "description": summary,
            "pub_date": pub_date_str,
            "source": "01booster",
            "category": "pitch",
        })
    print(f"  [01booster] {len(results)}件取得")
    return results


def fetch_ivs_news() -> list[dict]:
    """
    IVS（Infinity Ventures Summit）ニュースページ
    URL: https://www.ivs.events/news
    方式: BeautifulSoup (HTML scraping)
    robots.txt: Allow: / （全許可）
    対象: IVS LaunchPad（日本最大級ピッチコンテスト）の募集情報等
    """
    url = "https://www.ivs.events/news"
    print(f"  [ivs] ニュースページ取得中: {url}")
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  [ivs] 取得エラー: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    # IVSニュースページのリンク構造: <a href="./news/YYYYMMDD"> に日付・カテゴリ・タイトルが含まれる
    for a_tag in soup.find_all("a", href=True):
        href = a_tag.get("href", "")
        if "/news/" not in href:
            continue
        if href.startswith("./"):
            full_url = "https://www.ivs.events/" + href[2:]
        elif href.startswith("/"):
            full_url = "https://www.ivs.events" + href
        elif href.startswith("http"):
            full_url = href
        else:
            continue

        raw_text = a_tag.get_text(separator=" ", strip=True)
        if not raw_text or len(raw_text) < 5:
            continue

        results.append({
            "title": raw_text,
            "url": full_url,
            "description": raw_text,
            "pub_date": "",
            "source": "ivs",
            "category": "pitch",
        })

    seen = set()
    deduped = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)

    print(f"  [ivs] {len(deduped)}件取得")
    return deduped


# ---------------------------------------------------------------------------
# 補助金データソース取得関数
# ---------------------------------------------------------------------------

# jGrants API 確定仕様（2026-06-29 検証済み）
# エンドポイント: GET https://api.jgrants-portal.go.jp/exp/v1/public/subsidies
# 必須パラメータ: keyword（2文字以上）/ sort（created_date|acceptance_start_datetime|acceptance_end_datetime）/ order（ASC|DESC）/ acceptance（0|1）
# 任意パラメータ: use_purpose / industry / target_number_of_employees / target_area_search / institution_name
# レスポンス主要フィールド: id / title / institution_name / subsidy_max_limit / acceptance_start_datetime / acceptance_end_datetime / target_area_search
# 詳細エンドポイント: GET /exp/v1/public/subsidies/id/{id}（subsidy_rate / front_subsidy_detail_page_url 等が追加で取れる）

JGRANTS_API_BASE = "https://api.jgrants-portal.go.jp/exp/v1/public/subsidies"
JGRANTS_KEYWORDS = ["スタートアップ", "AI", "創業", "DX", "新規事業"]


def fetch_jgrants() -> list[dict]:
    """
    Jグランツ公式API（デジタル庁・認証不要）
    URL: https://api.jgrants-portal.go.jp/exp/v1/public/subsidies
    方式: REST API (JSON)
    認証: 不要
    acceptance=1: 受付中のみ取得
    複数キーワードで検索しID重複排除してマージ。
    """
    print(f"  [jgrants] API取得中 (keywords: {JGRANTS_KEYWORDS})")
    all_items: list[dict] = []
    seen_ids: set[str] = set()

    for kw in JGRANTS_KEYWORDS:
        try:
            resp = requests.get(
                JGRANTS_API_BASE,
                params={
                    "keyword": kw,
                    "sort": "acceptance_end_datetime",
                    "order": "ASC",
                    "acceptance": "1",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("result", []) or []
            count = data.get("metadata", {}).get("resultset", {}).get("count", 0)
            print(f"    keyword={kw}: {count}件")
            for item in items:
                item_id = item.get("id", "")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    # URL はJグランツの案件詳細ページを使用
                    item["_url"] = f"https://www.jgrants-portal.go.jp/subsidy/{item_id}"
                    item["source"] = "jgrants"
                    item["category"] = "subsidy"
                    all_items.append(item)
        except Exception as e:
            print(f"    [jgrants] keyword={kw} エラー: {e}")
        time.sleep(1)

    print(f"  [jgrants] 合計（重複排除後）: {len(all_items)}件")
    return all_items


NEDO_KOUBO_URL = "https://www.nedo.go.jp/koubo/2025_list_10.html"


def fetch_nedo_koubo() -> list[dict]:
    """
    NEDO スタートアップ支援公募一覧
    URL: https://www.nedo.go.jp/koubo/2025_list_10.html
    方式: BeautifulSoup (HTML scraping)
    robots.txt: 404（制限なし）
    対象: ディープテック・スタートアップ向け研究開発公募
    """
    print(f"  [nedo] 公募一覧取得中: {NEDO_KOUBO_URL}")
    try:
        resp = requests.get(NEDO_KOUBO_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  [nedo] 取得エラー: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        print("  [nedo] tableが見つかりません")
        return []

    today = date.today()
    results = []

    for row in table.find_all("tr")[1:]:  # ヘッダースキップ
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        title = cells[0].get_text(strip=True)
        end_str = cells[3].get_text(strip=True)  # 公募締切日

        # 公募開始URLを取得（CA2_xxxxxx.html = 公募開始ページ）
        links = [a["href"] for a in row.find_all("a", href=True)]
        if not links:
            continue
        url = "https://www.nedo.go.jp" + links[0] if links[0].startswith("/") else links[0]

        # 締切日パース（「YYYY年M月D日」形式）
        deadline = extract_deadline(end_str)

        # 締切が過去のものはスキップ（受付中のみ）
        if deadline and deadline < today:
            continue

        results.append({
            "title": title,
            "url": url,
            "description": title,
            "pub_date": "",
            "source": "nedo",
            "category": "subsidy",
            "_deadline": deadline,
            "_institution": "NEDO（国立研究開発法人新エネルギー・産業技術総合開発機構）",
            "_subsidy_max_limit": None,
            "_subsidy_rate": None,
        })

    print(f"  [nedo] {len(results)}件取得（受付中のみ）")
    return results


# ---------------------------------------------------------------------------
# Notion 共通
# ---------------------------------------------------------------------------

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_existing_urls(database_id: str) -> set[str]:
    """指定Notion DBに登録済みのイベントURL一覧を取得（重複排除用）。"""
    urls: set[str] = set()
    payload = {
        "filter": {
            "property": "イベントURL",
            "url": {"is_not_empty": True},
        },
        "page_size": 100,
    }
    has_more = True
    start_cursor = None

    while has_more:
        if start_cursor:
            payload["start_cursor"] = start_cursor
        resp = requests.post(
            f"{NOTION_API}/databases/{database_id}/query",
            headers=_notion_headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for page in data.get("results", []):
            url_prop = page.get("properties", {}).get("イベントURL", {}).get("url")
            if url_prop:
                urls.add(url_prop)
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    return urls


# ---------------------------------------------------------------------------
# Notion ピッチイベント登録
# ---------------------------------------------------------------------------

SOURCE_DISPLAY_MAP = {
    "onlab": "Open Network Lab",
    "techwave": "TechWave",
    "01booster": "01Booster",
    "ivs": "IVS",
    "jgrants": "Jグランツ",
    "nedo": "NEDO",
}


def _infer_pitch_type(title: str, description: str) -> str:
    """タイトル・説明からピッチイベント種別を推定する。"""
    combined = f"{title} {description}".lower()
    if any(kw in combined for kw in ["アクセラ", "accelerator", "インキュベータ", "incubat"]):
        return "アクセラ"
    if any(kw in combined for kw in ["デモデイ", "demo day", "demoday"]):
        return "デモデイ"
    if any(kw in combined for kw in ["コンテスト", "contest", "大賞", "award"]):
        return "コンテスト"
    return "ピッチ"


def create_notion_page_pitch(item: dict, deadline: Optional[date], database_id: str) -> dict:
    """
    ピッチイベントをNotion DBに登録する。
    スキーマ（確定版）:
      イベント名 (Title) / 主催 (Rich text) / 種別 (Select: ピッチ/アクセラ/デモデイ/コンテスト/補助金)
      対象ステージ (Select) / 応募締切 (Date) / 開催日 (Date) / 開催形式 (Select)
      イベントURL (URL・重複キー) / データソース (Select) / ステータス (Select) / 通知日 (Date)
    """
    title = item.get("title", "（名称不明）")[:2000]
    source = item.get("source", "その他")
    event_type = _infer_pitch_type(item.get("title", ""), item.get("description", ""))
    today_str = date.today().isoformat()

    properties: dict = {
        "イベント名": {"title": [{"text": {"content": title}}]},
        "主催": {"rich_text": [{"text": {"content": SOURCE_DISPLAY_MAP.get(source, source)}}]},
        "種別": {"select": {"name": event_type}},
        "対象ステージ": {"select": {"name": "不問"}},
        "開催形式": {"select": {"name": "不明"}},
        "データソース": {"select": {"name": source}},
        "ステータス": {"select": {"name": "新着"}},
        "通知日": {"date": {"start": today_str}},
    }

    url = item.get("url", "")
    if url:
        properties["イベントURL"] = {"url": url}
    if deadline:
        properties["応募締切"] = {"date": {"start": deadline.isoformat()}}

    resp = requests.post(
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": database_id}, "properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Notion 補助金登録
# ---------------------------------------------------------------------------

def _fmt_yen(amount: Optional[int]) -> str:
    """補助上限額を「〇万円」形式に変換する。"""
    if not amount:
        return ""
    if amount >= 100_000_000:
        return f"{amount // 100_000_000}億円"
    if amount >= 10_000:
        return f"{amount // 10_000}万円"
    return f"{amount}円"


def create_notion_page_subsidy(item: dict, deadline: Optional[date], database_id: str) -> dict:
    """
    補助金情報をNotion DBに登録する。
    共通スキーマにプラスして補助金固有プロパティを追加:
      補助上限額 (Rich text) / 補助率 (Rich text) / 実施機関 (Rich text)
    種別は「補助金」固定。
    """
    source = item.get("source", "その他")
    today_str = date.today().isoformat()

    # jgrants と nedo でフィールド名が異なるため吸収
    if source == "jgrants":
        title = item.get("title", "（名称不明）")[:2000]
        url = item.get("_url", "")
        institution = item.get("institution_name") or ""
        max_limit = item.get("subsidy_max_limit")
        subsidy_rate = item.get("subsidy_rate", "")
    else:  # nedo
        title = item.get("title", "（名称不明）")[:2000]
        url = item.get("url", "")
        institution = item.get("_institution", "NEDO")
        max_limit = item.get("_subsidy_max_limit")
        subsidy_rate = item.get("_subsidy_rate", "")

    properties: dict = {
        "イベント名": {"title": [{"text": {"content": title}}]},
        "主催": {"rich_text": [{"text": {"content": institution[:2000] if institution else ""}}]},
        "種別": {"select": {"name": "補助金"}},
        "対象ステージ": {"select": {"name": "不問"}},
        "開催形式": {"select": {"name": "不明"}},
        "データソース": {"select": {"name": source}},
        "ステータス": {"select": {"name": "新着"}},
        "通知日": {"date": {"start": today_str}},
    }

    if url:
        properties["イベントURL"] = {"url": url}
    if deadline:
        properties["応募締切"] = {"date": {"start": deadline.isoformat()}}

    # 補助金固有プロパティ（Notionスキーマに追加が必要な新規プロパティ）
    if max_limit:
        properties["補助上限額"] = {"rich_text": [{"text": {"content": _fmt_yen(max_limit)}}]}
    if subsidy_rate:
        properties["補助率"] = {"rich_text": [{"text": {"content": str(subsidy_rate)[:500]}}]}
    if institution:
        properties["実施機関"] = {"rich_text": [{"text": {"content": institution[:500]}}]}

    resp = requests.post(
        f"{NOTION_API}/pages",
        headers=_notion_headers(),
        json={"parent": {"database_id": database_id}, "properties": properties},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

DISCORD_COLOR_PITCH = 0x00B4D8    # 水色：ピッチイベント
DISCORD_COLOR_SUBSIDY = 0xF4A261  # オレンジ：補助金（🏛️で視覚区別）


def notify_discord_pitch(new_items: list[dict], database_id: str) -> None:
    """Discord Webhookにピッチイベント新着通知を送る。"""
    if not DISCORD_WEBHOOK_URL:
        print("[INFO] DISCORD_WEBHOOK_URL 未設定 — 通知スキップ")
        return
    if not new_items:
        print("[INFO] ピッチ通知対象なし — スキップ")
        return

    today_str = date.today().strftime("%Y-%m-%d")
    desc_lines = []
    for i, item in enumerate(new_items[:MAX_ITEMS_DISCORD], 1):
        title = item.get("title", "（名称不明）")[:80]
        url = item.get("url", "")
        source = SOURCE_DISPLAY_MAP.get(item.get("source", ""), item.get("source", ""))
        deadline = item.get("_deadline")

        line = f"**{i}.** [{title}]({url})"
        line += f"\n　📍 {source}"
        line += f" ／ 締切: {deadline.isoformat()}" if deadline else " ／ 応募締切：要確認（詳細は元ページ）"
        desc_lines.append(line)

    description = "\n\n".join(desc_lines)
    if len(new_items) > MAX_ITEMS_DISCORD:
        description += f"\n\n…ほか **{len(new_items) - MAX_ITEMS_DISCORD}件** はNotionで確認"
    if database_id:
        description += f"\n🗂 [Notion DB](https://www.notion.so/{database_id.replace('-', '')})"

    embed = {
        "title": f"🚀 AIピッチイベント 新着情報 ({today_str})",
        "description": description,
        "color": DISCORD_COLOR_PITCH,
        "footer": {"text": f"合計 {len(new_items)}件 | pitch-event-crawler"},
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
    if resp.status_code == 204:
        print(f"[OK] Discord ピッチ通知送信完了 ({len(new_items)}件)")
    else:
        print(f"[WARN] Discord ピッチ通知失敗: {resp.status_code} {resp.text}")


def notify_discord_subsidy(new_items: list[dict], database_id: str) -> None:
    """Discord Webhookに補助金新着通知を送る（ピッチと別Embed・別色）。"""
    if not DISCORD_WEBHOOK_URL:
        return
    if not new_items:
        print("[INFO] 補助金通知対象なし — スキップ")
        return

    today_str = date.today().strftime("%Y-%m-%d")
    desc_lines = []
    for i, item in enumerate(new_items[:MAX_ITEMS_DISCORD], 1):
        source = item.get("source", "")
        if source == "jgrants":
            title = item.get("title", "（名称不明）")[:80]
            url = item.get("_url", "")
            max_limit = item.get("subsidy_max_limit")
            institution = item.get("institution_name") or ""
        else:
            title = item.get("title", "（名称不明）")[:80]
            url = item.get("url", "")
            max_limit = item.get("_subsidy_max_limit")
            institution = item.get("_institution", "NEDO")

        deadline = item.get("_deadline")
        source_label = SOURCE_DISPLAY_MAP.get(source, source)

        line = f"**{i}.** [{title}]({url})"
        line += f"\n　🏛️ {source_label}"
        if institution:
            line += f" ／ {institution[:40]}"
        line += f" ／ 締切: {deadline.isoformat()}" if deadline else " ／ 締切：要確認"
        if max_limit:
            line += f" ／ 上限: {_fmt_yen(max_limit)}"
        desc_lines.append(line)

    description = "\n\n".join(desc_lines)
    if len(new_items) > MAX_ITEMS_DISCORD:
        description += f"\n\n…ほか **{len(new_items) - MAX_ITEMS_DISCORD}件** はNotionで確認"
    if database_id:
        description += f"\n🗂 [Notion DB](https://www.notion.so/{database_id.replace('-', '')})"

    embed = {
        "title": f"🏛️ スタートアップ補助金 新着情報 ({today_str})",
        "description": description,
        "color": DISCORD_COLOR_SUBSIDY,
        "footer": {"text": f"合計 {len(new_items)}件 | pitch-event-crawler (subsidy)"},
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
    if resp.status_code == 204:
        print(f"[OK] Discord 補助金通知送信完了 ({len(new_items)}件)")
    else:
        print(f"[WARN] Discord 補助金通知失敗: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    today = date.today()
    print(f"=== AIピッチイベント + スタートアップ補助金 クローラー開始 ({today}) ===")

    if DRY_RUN:
        print("[DRY-RUN] NOTION_TOKEN / DISCORD_WEBHOOK_URL のいずれかが未設定")
        print("[DRY-RUN] 実送信せず、フィルタ通過候補を標準出力に表示します\n")

    run_pitch = bool(NOTION_DATABASE_ID_PITCH or DRY_RUN)
    run_subsidy = bool(NOTION_DATABASE_ID_SUBSIDY or DRY_RUN)

    # ========== ピッチイベント ==========
    if run_pitch:
        print("\n" + "=" * 50)
        print("【ピッチイベント】")
        print("=" * 50)

        existing_pitch_urls: set[str] = set()
        if not DRY_RUN and NOTION_DATABASE_ID_PITCH:
            print("[1/4] Notion 既存ピッチレコード取得中…")
            try:
                existing_pitch_urls = get_existing_urls(NOTION_DATABASE_ID_PITCH)
                print(f"  既存: {len(existing_pitch_urls)}件")
            except Exception as e:
                print(f"  [WARN] 取得失敗（続行）: {e}")
        else:
            print("[1/4] Notion既存レコード: dry-runのためスキップ")

        print("[2/4] ピッチデータソース取得中…")
        pitch_raw: list[dict] = []
        for fetch_fn in [fetch_onlab_rss, fetch_techwave_rss, fetch_01booster_rss, fetch_ivs_news]:
            try:
                pitch_raw.extend(fetch_fn())
            except Exception as e:
                print(f"  [{fetch_fn.__name__}] エラー: {e}")
            time.sleep(CRAWL_DELAY)
        print(f"  合計取得: {len(pitch_raw)}件")

        print("[3/4] フィルタリング中…")
        seen_pitch_urls: set[str] = set(existing_pitch_urls)
        pitch_candidates: list[dict] = []

        for item in pitch_raw:
            url = item.get("url", "")
            title = item.get("title", "")
            description = item.get("description", "")

            if url and url in seen_pitch_urls:
                continue
            if url:
                seen_pitch_urls.add(url)

            if not passes_pitch_filter(title, description):
                continue

            deadline = extract_deadline(f"{title} {description}")
            item["_deadline"] = deadline

            if deadline and deadline < today:
                print(f"  [期限切れ除外] 締切{deadline} — {title[:50]}")
                continue

            pitch_candidates.append(item)

        print(f"  フィルタ通過: {len(pitch_candidates)}件")

        print("[4/4] 出力処理…")
        if DRY_RUN:
            print("\n--- dry-run: ピッチフィルタ通過候補 ---")
            for i, item in enumerate(pitch_candidates, 1):
                deadline = item.get("_deadline")
                print(f"\n  [{i}] [{item.get('source','')}] 締切: {deadline.isoformat() if deadline else '不明'}")
                print(f"       タイトル: {item.get('title','')[:80]}")
                print(f"       URL: {item.get('url','')}")
            n_deadline = len([x for x in pitch_candidates if x.get("_deadline")])
            print(f"\n--- dry-run ピッチ完了: {len(pitch_candidates)}件通過 ---")
            print(f"  うち締切判明: {n_deadline}件 / 締切不明: {len(pitch_candidates)-n_deadline}件（全件Discord通知対象）")
        else:
            registered_pitch: list[dict] = []
            for item in pitch_candidates:
                try:
                    create_notion_page_pitch(item, item.get("_deadline"), NOTION_DATABASE_ID_PITCH)
                    registered_pitch.append(item)
                    print(f"  ✓ [{item.get('source','')}] {item.get('title','')[:60]}")
                except Exception as e:
                    print(f"  ✗ {item.get('title','')[:60]} — {e}")
                time.sleep(0.4)
            notify_discord_pitch(registered_pitch, NOTION_DATABASE_ID_PITCH)
            print(f"  ピッチ: {len(registered_pitch)}件登録完了")

    # ========== 補助金 ==========
    if run_subsidy:
        print("\n" + "=" * 50)
        print("【スタートアップ補助金】")
        print("=" * 50)

        existing_subsidy_urls: set[str] = set()
        if not DRY_RUN and NOTION_DATABASE_ID_SUBSIDY:
            print("[1/4] Notion 既存補助金レコード取得中…")
            try:
                existing_subsidy_urls = get_existing_urls(NOTION_DATABASE_ID_SUBSIDY)
                print(f"  既存: {len(existing_subsidy_urls)}件")
            except Exception as e:
                print(f"  [WARN] 取得失敗（続行）: {e}")
        else:
            print("[1/4] Notion既存レコード: dry-runのためスキップ")

        print("[2/4] 補助金データソース取得中…")
        subsidy_raw: list[dict] = []
        for fetch_fn in [fetch_jgrants, fetch_nedo_koubo]:
            try:
                subsidy_raw.extend(fetch_fn())
            except Exception as e:
                print(f"  [{fetch_fn.__name__}] エラー: {e}")
            time.sleep(CRAWL_DELAY)
        print(f"  合計取得: {len(subsidy_raw)}件")

        print("[3/4] フィルタリング中…")
        seen_subsidy_urls: set[str] = set(existing_subsidy_urls)
        subsidy_candidates: list[dict] = []

        for item in subsidy_raw:
            source = item.get("source", "")
            url = item.get("_url", "") if source == "jgrants" else item.get("url", "")
            title = item.get("title", "")
            description = item.get("description", title)

            if url and url in seen_subsidy_urls:
                continue
            if url:
                seen_subsidy_urls.add(url)

            if not passes_subsidy_filter(title, description):
                print(f"  [フィルタ落ち] {title[:50]}")
                continue

            # jGrantsはAPIで受付中フィルタ済み、deadlineはAPIの終了日を使う
            if source == "jgrants":
                end_dt_str = item.get("acceptance_end_datetime", "")
                if end_dt_str:
                    try:
                        # ISO 8601形式 "2027-03-31T08:15:00.000Z" をパース
                        dt = datetime.fromisoformat(end_dt_str.replace("Z", "+00:00"))
                        deadline = dt.astimezone(timezone.utc).date()
                    except ValueError:
                        deadline = extract_deadline(end_dt_str)
                else:
                    deadline = None
                item["_deadline"] = deadline
            else:
                # NEDOはfetch時点でdeadline設定済み
                deadline = item.get("_deadline")

            if deadline and deadline < today:
                print(f"  [期限切れ除外] 締切{deadline} — {title[:50]}")
                continue

            subsidy_candidates.append(item)

        print(f"  フィルタ通過: {len(subsidy_candidates)}件")

        print("[4/4] 出力処理…")
        if DRY_RUN:
            print("\n--- dry-run: 補助金フィルタ通過候補 ---")
            for i, item in enumerate(subsidy_candidates, 1):
                source = item.get("source", "")
                url = item.get("_url", "") if source == "jgrants" else item.get("url", "")
                deadline = item.get("_deadline")
                max_limit = item.get("subsidy_max_limit") if source == "jgrants" else item.get("_subsidy_max_limit")
                print(f"\n  [{i}] [{source}] 締切: {deadline.isoformat() if deadline else '不明'}")
                print(f"       タイトル: {item.get('title','')[:80]}")
                print(f"       URL: {url}")
                if max_limit:
                    print(f"       補助上限: {_fmt_yen(max_limit)}")
            print(f"\n--- dry-run 補助金完了: {len(subsidy_candidates)}件通過 ---")
        else:
            registered_subsidy: list[dict] = []
            for item in subsidy_candidates:
                try:
                    create_notion_page_subsidy(item, item.get("_deadline"), NOTION_DATABASE_ID_SUBSIDY)
                    registered_subsidy.append(item)
                    print(f"  ✓ [{item.get('source','')}] {item.get('title','')[:60]}")
                except Exception as e:
                    print(f"  ✗ {item.get('title','')[:60]} — {e}")
                time.sleep(0.4)
            notify_discord_subsidy(registered_subsidy, NOTION_DATABASE_ID_SUBSIDY)
            print(f"  補助金: {len(registered_subsidy)}件登録完了")

    print("\n=== 全処理完了 ===")


if __name__ == "__main__":
    main()
