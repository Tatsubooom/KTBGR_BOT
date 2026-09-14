# KTBGR_BOT

Discord の VC 内の会話をリアルタイムで文字起こしし、特定の単語 (曖昧一致) に反応する BOT です。

- **discord.py** + [discord-ext-voice-recv](https://github.com/zacker150/discord-ext-voice-recv) で VC 音声を受信 (Discord の VC E2E 暗号化 = DAVE に対応)
- **faster-whisper** で文字起こし。**GPU (CUDA) / CPU を選択可能**
- **曖昧一致**: 表記ゆれ (漢字/ひらがな/カタカナ) や 1〜2 文字の聞き間違いがあっても反応
- **リアルタイム重視**: 話し終わりを待たず、話している途中の音声も逐次文字起こししてキーワードを早期検出

## 仕組み

```
VC音声 (話者ごと 48kHz stereo)
  └─ 16kHz mono に変換 → 音量で発話/無音を判定
       ├─ 話している途中: PARTIAL_INTERVAL_MS ごとに途中経過を文字起こし ─┐
       └─ 無音が SILENCE_MS 続く: 発話を確定して文字起こし ──────────────┤
                                                                      ▼
                          faster-whisper (GPU/CPU) → キーワード曖昧一致 → チャットに反応 / 効果音
```

- 文字起こしが追いつかない場合、古い途中経過は捨てて最新の音声だけを処理するため遅延が溜まりません
- 同じ発話の途中経過と確定結果で二重に反応しないようにしています (+ `COOLDOWN_SEC`)

### 曖昧一致の方法

文字起こし結果とキーワードを次の 3 つの表記に正規化し、編集距離ベースの類似度 (0〜100) の最大値が `MATCH_THRESHOLD` 以上なら反応します。

| 表記 | 例: 「寝落ち」 | 拾えるゆれ |
|---|---|---|
| surface (かな統一) | 寝落ち | カタカナ⇔ひらがな、全角/半角 |
| reading (読み) | ねおち | 漢字⇔かな、誤変換 (眠落ち など) |
| romaji | neochi | 音の近い聞き間違い |

2 文字以下の短いキーワードは誤爆しやすいため、完全一致のみで反応します。

## セットアップ

### 1. Discord BOT の作成

1. [Discord Developer Portal](https://discord.com/developers/applications) でアプリを作成し、Bot のトークンを取得
2. OAuth2 → URL Generator で `bot` と `applications.commands` を選択し、権限は
   `View Channels` / `Send Messages` / `Connect` / `Speak` を付けてサーバーに招待

### 2. インストール

Python 3.10〜3.13 を推奨します。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# CPU で使う場合
pip install -r requirements.txt

# GPU (NVIDIA / CUDA 12) で使う場合
pip install -r requirements-gpu.txt
```

効果音 (`sound`) を使う場合のみ [FFmpeg](https://ffmpeg.org/) を PATH に入れてください。

### 3. 設定

```bash
cp .env.example .env   # Windows: copy .env.example .env
```

`.env` の `DISCORD_TOKEN` を設定します。その他の項目は `.env.example` のコメントを参照してください。

### 4. 起動

```bash
python bot.py                  # WHISPER_DEVICE の設定に従う (デフォルト auto)
python bot.py --device cuda    # GPU を使う
python bot.py --device cpu     # CPU を使う
python bot.py --device cpu --model base
```

初回起動時は Whisper モデルが自動でダウンロードされます。

## GPU / CPU の選択

| 方法 | 例 |
|---|---|
| `.env` | `WHISPER_DEVICE=cuda` (`auto` / `cuda` / `cpu`) |
| 起動引数 | `python bot.py --device cpu` |
| 実行中に切り替え | `/device cpu` |

- `auto`: GPU が使えれば GPU、なければ CPU
- `WHISPER_COMPUTE_TYPE=default` の場合、GPU は `float16`、CPU は `int8` を使います

### おすすめ設定

| 環境 | WHISPER_MODEL | PARTIAL_INTERVAL_MS |
|---|---|---|
| GPU (VRAM 6GB〜) | `large-v3-turbo` または `medium` | 700〜1000 |
| GPU (VRAM 2〜4GB) | `small` | 700〜1000 |
| CPU | `base` または `small` | 1500〜2000 (重い場合は 0 で無効化) |

## キーワード設定 (`keywords.json`)

```json
{
  "keywords": [
    {
      "name": "お疲れ様",
      "aliases": ["おつかれ", "おつ"],
      "threshold": 80,
      "reply": "🍵 {name}さん、お疲れ様です！",
      "sound": "sounds/otsukare.mp3"
    }
  ]
}
```

| キー | 必須 | 説明 |
|---|---|---|
| `name` | ○ | キーワード |
| `aliases` | | 別表記・言い換え |
| `threshold` | | このキーワードだけの一致しきい値 (未指定なら `MATCH_THRESHOLD`) |
| `reply` | | 反応メッセージ。`{user}` (メンション) `{name}` (表示名) `{keyword}` `{text}` (文字起こし) が使えます |
| `sound` | | VC で再生する音声ファイル (要 FFmpeg) |

編集後は `/reload` で再起動なしに反映できます。キーワードは Whisper にもヒントとして渡されるため、固有名詞も認識されやすくなります。

## コマンド

| コマンド | 説明 |
|---|---|
| `/join` | 自分のいる VC に参加して聞き取り開始 |
| `/leave` | VC から退出 (VC に誰もいなくなった場合も自動退出) |
| `/device <auto\|cuda\|cpu>` | 文字起こしデバイスを切り替え |
| `/keywords` | キーワード一覧 |
| `/reload` | `keywords.json` を再読み込み |
| `/status` | 接続先・デバイス・モデルなどを表示 |

## 調整のヒント

- **反応が遅い** → `SILENCE_MS` / `PARTIAL_INTERVAL_MS` を下げる、モデルを小さくする、GPU を使う
- **誤反応が多い** → `MATCH_THRESHOLD` を上げる (85〜90)、キーワード単位で `threshold` を設定
- **反応しない** → `MATCH_THRESHOLD` を下げる (70〜75)、`aliases` に聞き間違えやすい表記を追加
- **物音や呼吸音で文字起こしされる** → `ENERGY_THRESHOLD` / `MIN_SPEECH_MS` を上げる

## テスト

```bash
pip install pytest
pytest
```

## 注意

VC の会話を文字起こしするため、利用するサーバーのメンバーには事前に BOT の存在と目的を周知してください。
