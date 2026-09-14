"""VC内の会話を文字起こしし、特定の単語に反応する Discord BOT。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import voice_recv

from ktbgr.combo import ComboState, build_message, hits_in_order
from ktbgr.config import DEVICES, load_settings
from ktbgr.listener import SpeechSink
from ktbgr.matcher import KeywordMatcher, Match
from ktbgr.transcriber import Transcriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ktbgr")

HOTWORDS_MAX_CHARS = 200
# 発言内容をそのまま載せるので @everyone やロールメンションは無効化する
ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


class KTBGRBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        self.settings = load_settings()
        # テキストチャットの内容を読むには Developer Portal で MESSAGE CONTENT INTENT を有効にする必要がある
        intents.message_content = self.settings.text_chat
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._text_last_fired: dict[tuple[int, str], float] = {}
        self._voice_combos: dict[tuple[int, int], ComboState] = {}
        self._combo_lock = asyncio.Lock()
        self.matcher = self.load_matcher()
        self.transcriber = Transcriber(
            model=self.settings.model,
            device=self.settings.device,
            compute_type=self.settings.compute_type,
            language=self.settings.language,
            beam_size=self.settings.beam_size,
        )
        self._update_hotwords()

    def load_matcher(self) -> KeywordMatcher:
        return KeywordMatcher.from_files(self.settings.keywords_files, self.settings.match_threshold)

    def _update_hotwords(self) -> None:
        # キーワードを Whisper に事前に伝えておくと、その単語として認識されやすくなる。
        # 長すぎるとかえって認識が崩れるので上限を設ける
        words, total = [], 0
        for k in self.matcher.keywords:
            if not k.hotword:
                continue
            if total + len(k.name) > HOTWORDS_MAX_CHARS:
                break
            words.append(k.name)
            total += len(k.name) + 1
        self.transcriber.hotwords = " ".join(words) or None

    async def setup_hook(self) -> None:
        register_commands(self)
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("ログイン: %s (device=%s)", self.user, self.transcriber.device)

    async def on_voice_state_update(self, member, before, after) -> None:
        # BOT以外が誰もいなくなったら自動退出
        vc = member.guild.voice_client
        if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
            await vc.disconnect()

    async def on_message(self, message: discord.Message) -> None:
        s = self.settings
        if not s.text_chat or message.author.bot or not message.content:
            return
        if s.text_channel_ids and message.channel.id not in s.text_channel_ids:
            return

        matches = await asyncio.to_thread(self.matcher.find, message.content)
        now = time.monotonic()
        vc = message.guild.voice_client if message.guild else None
        fresh = []
        for match in matches:
            key = (message.author.id, match.keyword.name)
            if now - self._text_last_fired.get(key, 0.0) < s.cooldown_sec:
                continue
            self._text_last_fired[key] = now
            fresh.append(match)
        if fresh:
            log.info(
                "検出(テキスト): %s <- %s「%s」%s",
                [m.keyword.name for m in fresh], message.author, message.content,
                [(round(m.score), m.variant) for m in fresh],
            )
            # テキストは1メッセージ内の検出をコンボにまとめて返信
            content = build_message(hits_in_order(message.content, fresh), message.author)
            if content:
                await message.reply(content, mention_author=False, allowed_mentions=ALLOWED_MENTIONS)
            self.play_sound(fresh, vc)

    async def react_voice(self, vc: discord.VoiceClient, user, text: str, matches: list[Match]) -> None:
        """VC での検出。同じ人が COMBO_WINDOW_SEC 以内に続けて語録を言ったら、前のメッセージを編集してコンボを伸ばす。"""
        channel = self._response_channel(vc)
        key = (channel.id, user.id)
        async with self._combo_lock:
            now = time.monotonic()
            state = self._voice_combos.get(key)
            window = self.settings.combo_window_sec
            if state is None or window <= 0 or now - state.last_time > window:
                state = self._voice_combos[key] = ComboState()
            state.hits += hits_in_order(text, matches)
            state.last_time = now

            content = build_message(state.hits, user)
            if content:
                if state.message is not None:
                    try:
                        await state.message.edit(content=content, allowed_mentions=ALLOWED_MENTIONS)
                    except discord.HTTPException:
                        state.message = None  # 消されていたら送り直す
                if state.message is None:
                    state.message = await channel.send(content, allowed_mentions=ALLOWED_MENTIONS)
        self.play_sound(matches, vc)

    def play_sound(self, matches: list[Match], vc: discord.VoiceClient | None) -> None:
        sound = next((m.keyword.sound for m in matches if m.keyword.sound), None)
        if sound and vc is not None and vc.is_connected() and not vc.is_playing():
            if Path(sound).is_file():
                vc.play(discord.FFmpegPCMAudio(sound))
            else:
                log.warning("効果音ファイルが見つかりません: %s", sound)

    def _response_channel(self, vc: discord.VoiceClient) -> discord.abc.Messageable:
        if self.settings.response_channel_id:
            channel = self.get_channel(self.settings.response_channel_id)
            if channel is not None:
                return channel
        return vc.channel  # ボイスチャンネル内蔵のテキストチャット

    async def start_listening(self, channel: discord.VoiceChannel) -> voice_recv.VoiceRecvClient:
        vc = channel.guild.voice_client
        if vc is None:
            vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        elif vc.channel != channel:
            await vc.move_to(channel)

        if vc.is_listening():
            vc.stop_listening()

        async def on_detect(user, text: str, matches: list[Match]) -> None:
            await self.react_voice(vc, user, text, matches)

        async def on_transcript(user, text: str) -> None:
            await self._response_channel(vc).send(f"{user.display_name}: {text}", allowed_mentions=ALLOWED_MENTIONS)

        sink = SpeechSink(
            settings=self.settings,
            transcriber=self.transcriber,
            get_matcher=lambda: self.matcher,
            loop=asyncio.get_running_loop(),
            on_detect=on_detect,
            on_transcript=on_transcript if self.settings.post_transcripts else None,
        )
        vc.listen(sink)
        return vc


def register_commands(bot: KTBGRBot) -> None:
    tree = bot.tree

    @tree.command(name="join", description="あなたのいるVCに参加して文字起こしを開始します")
    async def join(interaction: discord.Interaction):
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await interaction.response.send_message("先にVCに参加してください。", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await bot.start_listening(voice.channel)
        await interaction.followup.send(
            f"{voice.channel.mention} で聞き取りを開始しました (デバイス: {bot.transcriber.device})"
        )

    @tree.command(name="leave", description="VCから退出します")
    async def leave(interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None:
            await interaction.response.send_message("VCに参加していません。", ephemeral=True)
            return
        await vc.disconnect()
        await interaction.response.send_message("VCから退出しました")

    @tree.command(name="device", description="文字起こしに使うデバイス (GPU/CPU) を切り替えます")
    @app_commands.describe(device="auto: GPUがあればGPU / cuda: GPU / cpu: CPU")
    @app_commands.choices(device=[app_commands.Choice(name=d, value=d) for d in DEVICES])
    async def device(interaction: discord.Interaction, device: app_commands.Choice[str]):
        await interaction.response.defer(thinking=True)
        try:
            await asyncio.to_thread(bot.transcriber.load, device.value, bot.settings.compute_type)
        except Exception as e:
            await interaction.followup.send(f"切り替えに失敗しました: {e}\n現在のデバイス: {bot.transcriber.device}")
            return
        await interaction.followup.send(
            f"デバイスを {bot.transcriber.device} ({bot.transcriber.compute_type}) に切り替えました"
        )

    @tree.command(name="keywords", description="反応するキーワードの一覧を表示します")
    async def keywords(interaction: discord.Interaction):
        keywords = bot.matcher.keywords
        header = f"有効なキーワード: {len(keywords)}件 (無効 {len(bot.matcher.all_keywords) - len(keywords)}件)\n"
        body = ""
        for i, k in enumerate(keywords):
            line = f"- {discord.utils.escape_markdown(k.name)}\n"
            if len(header) + len(body) + len(line) > 1900:  # Discord のメッセージ上限 2000 文字
                body += f"ほか {len(keywords) - i}件"
                break
            body += line
        await interaction.response.send_message(header + body if keywords else "キーワードが登録されていません。", ephemeral=True)

    @tree.command(name="reload", description="キーワードファイルを再読み込みします")
    async def reload(interaction: discord.Interaction):
        try:
            bot.matcher = await asyncio.to_thread(bot.load_matcher)
        except Exception as e:
            await interaction.response.send_message(f"読み込みに失敗しました: {e}", ephemeral=True)
            return
        bot._update_hotwords()
        await interaction.response.send_message(f"キーワードを{len(bot.matcher.keywords)}件読み込みました", ephemeral=True)

    @tree.command(name="status", description="BOTの状態を表示します")
    async def status(interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        s = bot.settings
        await interaction.response.send_message(
            f"VC: {vc.channel.mention if vc else '未接続'}\n"
            f"モデル: {s.model} / デバイス: {bot.transcriber.device} ({bot.transcriber.compute_type})\n"
            f"キーワード: {len(bot.matcher.keywords)}件 / 一致しきい値: {s.match_threshold}",
            ephemeral=True,
        )


def main() -> None:
    bot = KTBGRBot()
    bot.run(bot.settings.token, log_handler=None)


if __name__ == "__main__":
    main()
