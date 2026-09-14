"""VC内の会話を文字起こしし、特定の単語に反応する Discord BOT。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import voice_recv

from ktbgr.config import DEVICES, load_settings
from ktbgr.listener import SpeechSink
from ktbgr.matcher import KeywordMatcher, Match
from ktbgr.transcriber import Transcriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ktbgr")


class KTBGRBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.settings = load_settings()
        self.matcher = KeywordMatcher.from_file(self.settings.keywords_file, self.settings.match_threshold)
        self.transcriber = Transcriber(
            model=self.settings.model,
            device=self.settings.device,
            compute_type=self.settings.compute_type,
            language=self.settings.language,
            beam_size=self.settings.beam_size,
        )
        self._update_hotwords()

    def _update_hotwords(self) -> None:
        # キーワードを Whisper に事前に伝えておくと、その単語として認識されやすくなる
        self.transcriber.hotwords = " ".join(k.name for k in self.matcher.keywords) or None

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

        async def on_detect(user, text: str, match: Match) -> None:
            keyword = match.keyword
            if keyword.reply:
                message = keyword.reply.format(user=user.mention, name=user.display_name, keyword=keyword.name, text=text)
                await self._response_channel(vc).send(message)
            if keyword.sound and vc.is_connected() and not vc.is_playing():
                if Path(keyword.sound).is_file():
                    vc.play(discord.FFmpegPCMAudio(keyword.sound))
                else:
                    log.warning("効果音ファイルが見つかりません: %s", keyword.sound)

        async def on_transcript(user, text: str) -> None:
            await self._response_channel(vc).send(f"**{user.display_name}**: {text}")

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
            f"🎙️ {voice.channel.mention} で聞き取りを開始しました (device: `{bot.transcriber.device}`)"
        )

    @tree.command(name="leave", description="VCから退出します")
    async def leave(interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc is None:
            await interaction.response.send_message("VCに参加していません。", ephemeral=True)
            return
        await vc.disconnect()
        await interaction.response.send_message("👋 退出しました")

    @tree.command(name="device", description="文字起こしに使うデバイス (GPU/CPU) を切り替えます")
    @app_commands.describe(device="auto: GPUがあればGPU / cuda: GPU / cpu: CPU")
    @app_commands.choices(device=[app_commands.Choice(name=d, value=d) for d in DEVICES])
    async def device(interaction: discord.Interaction, device: app_commands.Choice[str]):
        await interaction.response.defer(thinking=True)
        try:
            await asyncio.to_thread(bot.transcriber.load, device.value, bot.settings.compute_type)
        except Exception as e:
            await interaction.followup.send(f"⚠️ 切り替えに失敗しました: {e}\n(現在: `{bot.transcriber.device}`)")
            return
        await interaction.followup.send(
            f"✅ `{bot.transcriber.device}` ({bot.transcriber.compute_type}) に切り替えました"
        )

    @tree.command(name="keywords", description="反応するキーワードの一覧を表示します")
    async def keywords(interaction: discord.Interaction):
        lines = [
            f"- **{k.name}**" + (f" (別表記: {', '.join(k.aliases)})" if k.aliases else "")
            for k in bot.matcher.keywords
        ]
        await interaction.response.send_message("\n".join(lines) or "キーワードが登録されていません。", ephemeral=True)

    @tree.command(name="reload", description="keywords.json を再読み込みします")
    async def reload(interaction: discord.Interaction):
        try:
            bot.matcher = KeywordMatcher.from_file(bot.settings.keywords_file, bot.settings.match_threshold)
        except Exception as e:
            await interaction.response.send_message(f"⚠️ 読み込みに失敗しました: {e}", ephemeral=True)
            return
        bot._update_hotwords()
        await interaction.response.send_message(f"🔄 {len(bot.matcher.keywords)}件のキーワードを読み込みました", ephemeral=True)

    @tree.command(name="status", description="BOTの状態を表示します")
    async def status(interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        s = bot.settings
        await interaction.response.send_message(
            f"VC: {vc.channel.mention if vc else '未接続'}\n"
            f"モデル: `{s.model}` / デバイス: `{bot.transcriber.device}` ({bot.transcriber.compute_type})\n"
            f"キーワード: {len(bot.matcher.keywords)}件 / 一致しきい値: {s.match_threshold}",
            ephemeral=True,
        )


def main() -> None:
    bot = KTBGRBot()
    bot.run(bot.settings.token, log_handler=None)


if __name__ == "__main__":
    main()
