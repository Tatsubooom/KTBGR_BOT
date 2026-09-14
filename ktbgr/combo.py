"""複数のキーワードを続けて検出したときのコンボ表示。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .matcher import Match

MAX_MESSAGE_LEN = 2000  # Discord のメッセージ上限


@dataclass
class Hit:
    text: str  # 検出元の発言 (文字起こし)
    match: Match


@dataclass
class ComboState:
    """VC で同じ人が続けて語録を言ったときのコンボ (メッセージを編集して伸ばしていく)。"""

    hits: list[Hit] = field(default_factory=list)
    message: object | None = None  # 送信済みの discord.Message
    last_time: float = 0.0


def combo_header(count: int) -> str:
    if count >= 10:
        return f"🌈 **{count} COMBO!!!!** 語録の化身"
    if count >= 7:
        return f"💥 **{count} COMBO!!!**"
    if count >= 5:
        return f"⚡ **{count} COMBO!!!**"
    if count >= 3:
        return f"🔥 **{count} COMBO!!**"
    return f"✨ **{count} COMBO!**"


def _label(match: Match) -> str:
    return match.keyword.label or f"**{match.keyword.name}**"


def build_message(hits: list[Hit], user) -> str | None:
    """反応メッセージを組み立てる。

    - 1件だけならそのキーワードの reply をそのまま使う
    - 複数件なら「N COMBO!」ヘッダー + 各キーワードの label を検出順に並べる
      (発言が1つだけならヘッダーに発言を載せ、複数の発言にまたがる場合は発言ごとに載せる)
    """
    if not hits:
        return None
    if len(hits) == 1:
        keyword, text = hits[0].match.keyword, hits[0].text
        if not keyword.reply:
            return None
        return keyword.reply.format(user=user.mention, name=user.display_name, keyword=keyword.name, text=text)

    single_text = len({h.text for h in hits}) == 1
    header = f"{combo_header(len(hits))} {user.display_name}"
    lines = [f"{header}「{hits[0].text}」" if single_text else header]
    previous_text = None
    for i, hit in enumerate(hits, start=1):
        line = f"`{i:>2}` {_label(hit.match)}"
        if not single_text and hit.text != previous_text:
            line += f" ← 「{hit.text}」"
        previous_text = hit.text
        lines.append(line)

    message = ""
    for i, line in enumerate(lines):
        if len(message) + len(line) + 1 > MAX_MESSAGE_LEN - 20:
            message += f"…ほか {len(lines) - i}件"
            break
        message += line + "\n"
    return message.rstrip()[:MAX_MESSAGE_LEN]


def hits_in_order(text: str, matches: list[Match]) -> list[Hit]:
    """1つの発言内の一致を、発言に出てきた順の Hit にする。"""
    return [Hit(text, m) for m in sorted(matches, key=lambda m: m.start)]
