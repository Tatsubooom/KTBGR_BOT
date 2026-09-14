# KTBGR_BOT

Discord の VC 内の会話をリアルタイムで文字起こしし、特定の単語 (曖昧一致) に反応する BOT です。

- VC 音声の受信は discord.py と [discord-ext-voice-recv](https://github.com/zacker150/discord-ext-voice-recv) (Discord の VC E2E 暗号化 DAVE に対応)
- 文字起こしは faster-whisper。GPU (CUDA) と CPU を選択できます
- 表記ゆれ (漢字/ひらがな/カタカナ) や 1〜2 文字の聞き間違いがあっても反応します
- 話し終わりを待たず、話している途中の音声も文字起こししてキーワードを検出します
- テキストチャンネルの発言にも同じキーワードで反応します
- 1回の発言で複数のキーワードを検出した場合は、コンボとして1つのメッセージにまとめます

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
   `View Channels` / `Send Messages` / `Read Message History` / `Connect` / `Speak` を付けてサーバーに招待
3. テキストチャットに反応させる場合は、Bot ページの Privileged Gateway Intents で MESSAGE CONTENT INTENT を有効化
   (使わない場合は `.env` で `TEXT_CHAT=false`。有効化せずに `TEXT_CHAT=true` のまま起動するとログインに失敗します)

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
      "reply": "{name}さん、お疲れ様です",
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
| `label` | | コンボ表示での表記 (未指定なら `name`) |
| `enabled` | | `false` にすると検出しない |
| `hotword` | | `false` にすると Whisper への認識ヒントに含めない |

編集後は `/reload` で再起動なしに反映できます。キーワードは Whisper にもヒントとして渡されるため、固有名詞も認識されやすくなります (合計 200 文字まで)。

キーワードファイルは `.env` の `KEYWORDS_FILE` にカンマ区切りで複数指定できます (デフォルト: `keywords.json,data/inmu_goroku.json`)。

### データの更新

```bash
python scripts/fetch_inmu_goroku.py --refresh
```

取得結果は `.cache/` にキャッシュされ、`--refresh` なしの場合はキャッシュからデータだけ再生成します (しきい値のルールを変えたときなど)。

## テキストチャット

`TEXT_CHAT=true` (デフォルト) の場合、BOT が見えるテキストチャンネルの発言にも反応し、その発言への返信として `reply` を送ります。`/join` しなくても動作します。

- 反応するチャンネルを絞るには `TEXT_CHANNEL_IDS=123,456` のように指定
- 同じ人・同じキーワードの連続反応は `COOLDOWN_SEC` で抑制 (VC とは別に管理)
- 他の BOT の発言には反応しません
- BOT がそのサーバーの VC に接続中なら、`sound` の効果音も VC で再生します

## コンボ

1回の発言で複数のキーワードを検出すると、1つのメッセージにまとめて送ります。発言内で同じ箇所に複数のキーワードが一致した場合は、一番近いものだけを数えます。

```
テスター 3コンボ
「頭にきますよ、当たり前だよなぁ？やりますねぇ」
1. 頭にきますよ（野獣先輩）
2. 当たり前だよなぁ？（MUR）
3. やりますねぇ！（野獣先輩）
```

- テキストチャット: 1つのメッセージ内の検出をまとめます
- VC: 同じ人が `COMBO_WINDOW_SEC` (デフォルト 8 秒) 以内に続けて検出されると、前の反応メッセージを編集してコンボを続けます。発言が複数にまたがる場合は、行ごとに発言を表示します (0 で無効)

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

| 症状 | 対処 |
|---|---|
| 反応が遅い | `SILENCE_MS` / `PARTIAL_INTERVAL_MS` を下げる、モデルを小さくする、GPU を使う |
| 誤反応が多い | `MATCH_THRESHOLD` を上げる (80〜90)、キーワード単位で `threshold` を設定 |
| 反応しない | `MATCH_THRESHOLD` を下げる (60〜65)、`aliases` に聞き間違えやすい表記を追加 |
| 物音や呼吸音で文字起こしされる | `ENERGY_THRESHOLD` / `MIN_SPEECH_MS` を上げる |

## テスト

```bash
pip install pytest
pytest
```

## 注意

VC の会話を文字起こしするため、利用するサーバーのメンバーには事前に BOT の存在と目的を周知してください。
