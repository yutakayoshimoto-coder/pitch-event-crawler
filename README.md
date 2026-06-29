目的：AIピッチイベントとスタートアップ補助金を定期収集しDiscordへ通知する
背景：社外依頼者向けに精度最優先でAI/ピッチ/補助金情報を自動提供
内容：信頼ソース固定（LLMなし・新規APIキーなし） → Notion DB → Discord通知

# pitch-event-crawler

AIプロダクトを中核とするスタートアップが応募できる選抜型ピッチイベント・アクセラレータ情報と、創業・スタートアップ・AI/DX向け補助金情報を自動収集します。

## スコープ

### ピッチイベント
- 対象：AI/AIプロダクトを事業の中核とするスタートアップが登壇・応募できる選抜型のピッチイベント、アクセラレータ、デモデイ、ピッチコンテスト
- 対象外：開催済みイベント、募集終了案件

### スタートアップ補助金
- 対象：創業・新規事業・スタートアップ向け＋AI/DX系の補助金（受付中のみ）
- 対象外：施設整備/農業/介護/エネルギー等、スタートアップ色がないもの

### 共通
- 方式：案A（LLMなし・信頼ソース固定）。Brave Search、Claude APIは不使用。新規APIキー不要。

## 採用データソース

### ピッチイベント

| ソース | 取得方式 | robots.txt | 備考 |
|--------|----------|-----------|------|
| [Open Network Lab](https://onlab.jp/feed/) | RSS (feedparser) | 管理画面のみDisallow | 「Series T – Post AGI from Kyoto」等のAIピッチを掲載確認 |
| [TechWave](https://techwave.jp/feed/) | RSS (feedparser) | wp-adminのみDisallow | スタートアップ・技術系メディア。IVS LaunchPad等の関連記事あり |
| [01Booster](https://www.01booster.co.jp/feed/) | RSS (feedparser) | wp-adminのみDisallow | スタートアップ支援・アクセラレータ運営 |
| [IVS News](https://www.ivs.events/news) | HTML (BeautifulSoup) | Allow: / （全許可） | IVS LaunchPad主催者公式ニュース |

### スタートアップ補助金

| ソース | 取得方式 | 認証 | 備考 |
|--------|----------|------|------|
| [Jグランツ API](https://api.jgrants-portal.go.jp/exp/v1/public/subsidies) | REST API (JSON) | 不要 | デジタル庁公式。acceptance=1で受付中のみ取得 |
| [NEDO スタートアップ支援公募](https://www.nedo.go.jp/koubo/2025_list_10.html) | HTML (BeautifulSoup) | 不要 | ディープテック系を補完。robots.txt 404=制限なし |

## jGrants API 確定仕様（2026-06-29 検証済み）

エンドポイント: `GET https://api.jgrants-portal.go.jp/exp/v1/public/subsidies`

### 必須パラメータ（全4件）

| パラメータ | 型 | 値の例 | 説明 |
|---|---|---|---|
| `keyword` | string | `スタートアップ` | 2文字以上、スペース不可 |
| `sort` | string | `acceptance_end_datetime` | `created_date` / `acceptance_start_datetime` / `acceptance_end_datetime` |
| `order` | string | `ASC` | `ASC` / `DESC` |
| `acceptance` | string | `1` | `0`=全件 / `1`=受付中のみ |

本実装で使用するキーワード: `スタートアップ` / `AI` / `創業` / `DX` / `新規事業`（5回叩いてID重複排除）

### レスポンス主要フィールド（一覧）

| フィールド | 説明 |
|---|---|
| `id` | 補助金ID（詳細URL構成に使用） |
| `title` | 補助金名 |
| `institution_name` | 実施機関名 |
| `subsidy_max_limit` | 補助上限額（整数・円） |
| `acceptance_start_datetime` | 募集開始日時（ISO 8601） |
| `acceptance_end_datetime` | 募集終了日時（ISO 8601）→ 応募締切に使用 |
| `target_area_search` | 対象地域 |

詳細エンドポイント `GET /exp/v1/public/subsidies/id/{id}` を叩くと `subsidy_rate`（補助率）/ `front_subsidy_detail_page_url` も取得可能。

## Notion スキーマ（確定版）

### ピッチイベント DB（NOTION_DATABASE_ID_PITCH）

| プロパティ名 | 型 | 選択肢 |
|---|---|---|
| イベント名 | Title | - |
| 主催 | Rich text | - |
| 種別 | Select | ピッチ / アクセラ / デモデイ / コンテスト / **補助金** |
| 対象ステージ | Select | シード / アーリー / 成長期 / 不問 |
| 応募締切 | Date | - |
| 開催日 | Date | - |
| 開催形式 | Select | オンライン / オフライン / ハイブリッド / 不明 |
| イベントURL | URL | 重複キー |
| データソース | Select | onlab / techwave / 01booster / ivs / jgrants / nedo / その他 |
| ステータス | Select | 新着 / 確認済 / 応募済 / 締切済 |
| 通知日 | Date | - |

### スタートアップ補助金 DB（NOTION_DATABASE_ID_SUBSIDY）

ピッチDBと同じ共通プロパティに加えて、以下3つを追加してください：

| 追加プロパティ名 | 型 | 備考 |
|---|---|---|
| 補助上限額 | Rich text | 例: 「50万円」「2億円」 |
| 補助率 | Rich text | 例: 「1/2」「2分の1」 |
| 実施機関 | Rich text | jGrantsのinstitution_name |

種別の選択肢には「補助金」のみ使用。

## 環境変数（GitHub Secrets）

| Secret名 | 説明 | 共用元 |
|---|---|---|
| `NOTION_TOKEN` | Notion APIトークン | kkj-crawlerと共用 |
| `NOTION_DATABASE_ID_PITCH` | ピッチイベント用NotionデータベースID | 新規（CEOがDB作成後に設定） |
| `NOTION_DATABASE_ID_SUBSIDY` | スタートアップ補助金用NotionデータベースID | 新規（未設定なら補助金処理スキップ） |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL | kkj-crawlerと共用 |

## Discord通知仕様

### ピッチイベント
- タイトル：`🚀 AIピッチイベント 新着情報 (YYYY-MM-DD)`
- 色：0x00B4D8（水色）
- 締切不明の場合：「応募締切：要確認（詳細は元ページ）」と明記

### スタートアップ補助金
- タイトル：`🏛️ スタートアップ補助金 新着情報 (YYYY-MM-DD)`
- 色：0xF4A261（オレンジ）
- 補助上限額を表示
- 別Embedで送信（ピッチと視覚的に区別）

## dry-run（ローカル動作確認）

env未設定の状態で実行すると、実送信せずフィルタ通過候補を標準出力に表示します。

```bash
cd pitch-event-crawler
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## 実行スケジュール

毎週月曜・木曜 09:00 JST に自動実行（GitHub Actions）

## CEOがやるフェーズ2手順

1. GitHubに新規リポジトリ `retra-pitch-event-crawler` を作成し、このディレクトリの4ファイルをpush（.venv は除く）
2. Notion MCPで **ピッチイベントDB** を作成（上記スキーマ参照）
3. Notion MCPで **スタートアップ補助金DB** を作成（上記スキーマ参照、追加3プロパティあり）
4. GitHub Secrets に以下を設定: `NOTION_TOKEN` / `NOTION_DATABASE_ID_PITCH` / `NOTION_DATABASE_ID_SUBSIDY` / `DISCORD_WEBHOOK_URL`
5. Actions タブ → `Pitch Event Crawler` → `Run workflow` で手動実行して動作確認

## 運用メモ

- ソースを追加したい場合は、ピッチ側は `source_funcs` 相当のリストに、補助金側は補助金ループに関数を追加するだけ。1つのソースが例外でも他は継続する設計。
- `SUBSIDY_PASS_KEYWORDS` / `SUBSIDY_EXCLUDE_KEYWORDS` で補助金フィルタ精度を調整。
- `PITCH_EXCLUDE_KEYWORDS` はタイトルのみで判定（本文中の「結果発表」等で募集記事を誤除外しないため）。
- NEDOのページURLが変わった場合は `NEDO_KOUBO_URL` 定数を変更。
- jGrantsの受付中は`acceptance=1`で担保済み。フィルタは補助金名のみで行うため精度は中程度。ノイズが多い場合は`SUBSIDY_EXCLUDE_KEYWORDS`に追加する。
