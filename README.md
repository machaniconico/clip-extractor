# Clip Extractor

YouTube 配信アーカイブ（または手元の動画ファイル）から、AI でハイライトを検出して**切り抜き動画・縦型ショート・サムネ・概要欄タイムスタンプ**を自動生成するツールです。Premiere Pro 用の XML 書き出しと、ブラウザで使える Web UI の両方に対応します。

---

## 主な機能

- **ハイライト自動検出** — 文字起こしを AI（Claude / OpenAI / Gemini）に渡し、盛り上がり区間を抽出
- **切り抜き生成** — 検出区間を ffmpeg で切り出し（combined / individual の2モード）
- **縦型ショート変換（9:16）** — `crop` / `blur` / `pad` の3モード＋冒頭タイトル焼き込み（書記素クラスタ対応の折返しで絵文字・結合文字も崩れない）
- **ワード単位カラオケ字幕** — ショート専用。ASS の `\k` タイミングで読み上げに同期して色が動く字幕を焼き込み
- **サムネイル候補の自動生成** — 代表フレームを抽出し、タイトルを焼き込んだ候補画像を生成
- **音声の盛り上がり融合** — 音量（dBFS）カーブ＋スパイク検出で「盛り上がりスコア」を作り、AI のハイライト順位に融合して再ランク（失敗時は元の順位を維持する fail-open）
- **概要欄タイムスタンプ生成** — チャプター（タイムスタンプ）テキストを生成。YouTube の概要欄へ自動追記も可能
- **Premiere Pro 連携** — combined / individual の XML を書き出し
- **2フェーズ Web UI** — 「検出」と「レンダリング」を分離。検出後に各クリップの **イン/アウト点・タイトルをプレビューしながら編集** してから書き出せる（再文字起こし不要）
- **外部連携（任意）** — YouTube 概要欄への自動追記、Google Drive へのアップロード

---

## 処理の流れ

```
入力（YouTube URL / 動画ファイル）
  └─ ダウンロード（yt-dlp）           ※URL のとき
  └─ 文字起こし（faster-whisper）
  └─ ハイライト検出（Claude / OpenAI / Gemini）
        └─ 音声の盛り上がり融合で再ランク（--audio-fusion）
  └─ 切り抜き生成（ffmpeg）
        ├─ 縦型ショート（--shorts）＋タイトル焼き込み
        ├─ カラオケ字幕（--karaoke）
        └─ サムネ候補（--thumbnails）
  └─ 概要欄タイムスタンプ生成（+ 任意で YouTube へ追記）
  └─ Premiere Pro 用 XML 書き出し
```

---

## 必要環境

- **Python 3.10+**
- **ffmpeg / ffprobe** — 切り抜き・ショート・サムネ生成に必須（PATH に通しておくこと）
- **ハイライト検出に使う AI**（いずれか1つ）
  - **Claude**（CLI 既定）: `claude` CLI が必要 — `npm install -g @anthropic-ai/claude-code`
  - **OpenAI**: `OPENAI_API_KEY`
  - **Gemini**: `GEMINI_API_KEY`（または Web UI から保存。取得方法は次節）
- GPU があれば faster-whisper が高速化されます（CPU でも動作可）

### Gemini API キーの取得（無料・約2分）

> 🔰 **PC 操作に慣れていない方へ**: 同梱の **`SETUP_GUIDE.html`** をダブルクリックしてブラウザで開いてください。Gemini API キーと credentials.json の両方の取得手順を、図解つき・専門用語の解説つきで説明しています。

Gemini は**無料枠あり・クレジットカード登録不要**で、3 つの中で一番手軽に始められます。

1. [aistudio.google.com/apikey](https://aistudio.google.com/apikey)（Google AI Studio）を開いて Google アカウントでログイン
   - 会社・学校の Workspace アカウントは組織設定でブロックされていることがあるため**個人 Gmail 推奨**
2. 初回は利用規約に同意（デフォルトプロジェクトと初期キーが自動作成されることもあり、それを使って OK）
3. **[+ APIキーを作成]**（Create API key）→ プロジェクトを聞かれたら新規作成で OK
4. 生成されたキー（`AIza...`）をコピーし、環境変数 `GEMINI_API_KEY` に設定するか、Web UI の Settings タブに貼り付けて保存

知っておくと安心:

- **無料で使えるモデル**: `gemini-2.5-flash`（既定）/ `gemini-2.5-flash-lite` / `gemini-2.5-pro` の3つとも無料枠で利用可。ただし **Pro は無料枠のレート上限が flash 系よりかなり厳しい**（429 が出やすい）ため、普段使いは既定の flash が無難
- **`429` エラー** = 無料枠のレート制限。数分待てば復活します
- **昔作ったキーが弾かれる場合**: 2026年のセキュリティ移行で「制限なし」の古いキーは 2026/6/19 から順次拒否されます。AI Studio でキーを**新規作成し直す**のが最速（新キーは自動で適切に制限済み）
- キーはパスワードと同じ扱いで（公開リポジトリに書かない・人に見せない）

> ⚠️ この API キーと、YouTube/Drive 連携で使う `credentials.json`（`CREDENTIALS_SETUP.txt` 参照）は**別物**です。

---

## インストール

```bash
# 依存パッケージ
pip install -r requirements.txt
```

Windows では同梱の `setup.bat` でも環境構築できます。

> 開発時は仮想環境を推奨します。テストは `.venv` 前提で `.venv/bin/python -m pytest` で実行してください（後述）。

---

## 使い方

### Web UI（おすすめ）

ブラウザ上で検出 → クリップを編集 → 書き出し、までを操作できます。

```bash
python web_app.py
# または launcher 経由（自動でブラウザを開く）
python launcher.py
```

Windows では `Clip Extractor.bat` をダブルクリックでも起動できます。起動後 `http://localhost:7860` を開きます。

### OBS Studio と同時起動（Windows）

Settings / 設定タブの **「Clip Extractor起動時にOBS Studioも起動」** をONにし、画面下の **「デフォルトに設定」** で保存すると、次回から通常のClip Extractor起動時にOBSも一緒に開きます。

- **OBS実行ファイルのパス**には `obs64.exe` のフルパスを貼り付けられます
- パスを空欄にすると、PATHと標準インストール先から自動検出します
- OBS が既に起動している場合は二重起動しません
- OBS が見つからない・起動に失敗した場合も Clip Extractor は通常どおり起動します

設定とは別に、`Clip Extractor with OBS.bat` をダブルクリックするとチェック状態に関係なく2つを一度に起動できます。`setup.bat` が作成するデスクトップショートカットは通常の **`Clip Extractor`** 1つだけです。同時起動を常に使いたい場合は上記の設定をONにしてください。

これは2つのアプリを同時に開く機能です。配信終了後の自動処理を使う場合は、Web UI の **OBS連携** タブで従来どおり **「OBS連携 開始」** を押してください。

コマンドから使う場合は `python launcher.py --with-obs` でも起動できます。

- AI プロバイダ（Claude / OpenAI / Gemini）とモデルを画面で選択
- Gemini の API キーは画面から保存可能（`.gemini_key` に保存され、`GEMINI_API_KEY` 環境変数より優先）。取得手順は Settings タブの「📘 Gemini APIキーの取得手順」アコーディオンにも掲載
- 「検出」後に各クリップのイン/アウト点・タイトルを編集してから「レンダリング」（再文字起こしは走りません）

### コマンドライン（main.py）

```bash
# YouTube URL から
python main.py https://youtube.com/watch?v=xxxxx

# 手元の動画から、縦型ショートも生成
python main.py ./archive.mp4 --shorts

# クリップ数とモードを指定
python main.py ./archive.mp4 --mode individual --clips 3

# 検出プロンプトを指定
python main.py ./archive.mp4 --prompt "面白いシーンだけ選んで"

# ショート + カラオケ字幕 + サムネ + 音声融合まで一気に
python main.py ./archive.mp4 --shorts --karaoke --thumbnails --audio-fusion
```

> CLI のハイライト検出は既定で `claude` CLI を使います（API キー不要）。OpenAI / Gemini を CLI から使いたい場合は Web UI の利用を推奨します。

---

## 主なオプション（CLI）

| オプション | 説明 | 既定値 |
|---|---|---|
| `input` | YouTube URL または動画ファイルパス | — |
| `-o, --output` | 出力ディレクトリ | 自動生成 |
| `-n, --clips` | 切り抜き本数 | 5 |
| `-m, --mode` | `combined` / `individual` | combined |
| `-s, --shorts` | 9:16 縦型ショートも生成 | off |
| `--shorts-mode` | ショート変換 `crop` / `blur` / `pad` | crop |
| `--shorts-crop` | 横クロップ位置 `center` / `left` / `right` | center |
| `--no-shorts-title` | ショート冒頭のタイトル焼き込みを無効化 | off |
| `--thumbnails` | サムネイル候補画像を生成 | off |
| `--audio-fusion` | 音声の盛り上がりを順位に融合 | off |
| `--audio-alpha` | 音声重み（0.0–1.0） | 0.35 |
| `--karaoke` | ショートにワード単位カラオケ字幕を焼き込み | off |
| `-p, --prompt` | ハイライト検出の追加プロンプト | "" |
| `--min-duration` / `--max-duration` | クリップの最短/最長秒数 | 30 / 90 |
| `--whisper-model` | Whisper モデルサイズ | large-v3 |
| `--language` | 言語コード | ja |
| `--font-config` | フォント設定 JSON のパス | — |
| `--no-clips` | 切り抜きを生成せずタイムスタンプのみ | off |
| `--no-chapters` | タイムスタンプ生成を無効化 | off |
| `--chapter-prompt` | タイムスタンプ専用プロンプト（`--no-clips` 時） | "" |

### 外部連携（任意）

| オプション | 説明 |
|---|---|
| `--auto-append-youtube` | 生成したタイムスタンプを YouTube 概要欄へ自動追記（URL 入力 + `credentials.json` 必須） |
| `--youtube-setup` / `--youtube-status` / `--youtube-revoke` | YouTube OAuth の認証 / 状態確認 / 解除 |
| `--drive-setup` / `--drive-status` / `--drive-revoke` | Google Drive OAuth の認証 / 状態確認 / 解除 |

YouTube / Drive 連携のセットアップ手順は `CREDENTIALS_SETUP.txt` を参照してください（初心者向けの図解版は `SETUP_GUIDE.html`）。

---

## 出力物

- 切り抜き動画（横）と、`--shorts` 指定時は縦型ショート（9:16）
- `--karaoke` 指定時はカラオケ字幕を焼き込んだショート
- `--thumbnails` 指定時はサムネイル候補画像
- 概要欄用タイムスタンプ（テキスト）
- Premiere Pro 用 XML（combined / individual）

---

## 開発

```bash
# 全テスト（.venv 前提）
.venv/bin/python -m pytest -q
```

主要モジュール:

| ファイル | 役割 |
|---|---|
| `main.py` | CLI エントリポイント |
| `web_app.py` / `launcher.py` | Gradio Web UI |
| `downloader.py` | yt-dlp ダウンロード |
| `transcriber.py` | faster-whisper 文字起こし（ワード単位タイムスタンプ対応） |
| `highlighter.py` | AI ハイライト検出（Claude / OpenAI / Gemini） |
| `audio_energy.py` | 音声の盛り上がりスコア化・順位融合 |
| `clipper.py` | 切り抜き・ショート変換・サムネ生成（ffmpeg） |
| `subtitles.py` | SRT / カラオケ ASS 字幕生成 |
| `chapters.py` | 概要欄タイムスタンプ生成 |
| `premiere_xml.py` | Premiere Pro 用 XML 書き出し |
| `youtube_api.py` / `drive_upload.py` / `_google_auth.py` | YouTube / Drive 連携 |
