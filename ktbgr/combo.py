"""複数のキーワードを続けて検出したときのコンボ表示。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .matcher import Match

MAX_MESSAGE_LEN = 2000  # Discord のメッセージ上限


@dataclass
class Hit:
    text: str  # 検出元の発言 (文字起こし)。VC では発話の確定後に確定版の文字起こしに差し替える
    match: Match
    key: tuple[int, int] | None = None  # VC の発話キー (ユーザーID, 発話ID)
    context: tuple[str, ...] = ()  # 照合につなげた直前の発言 (VC で1つの語録が複数の発話に分かれた場合)

    @property
    def source(self) -> str:
        """原因になった発言。直前の発言とつなげて検知した場合はそれも含める。"""
        return "".join(f"「{t}」" for t in (*self.context, self.text))

    @property
    def plain_source(self) -> str:
        return " ".join((*self.context, self.text))


@dataclass
class ComboState:
    """VC で同じ人が続けて語録を言ったときのコンボ (メッセージを編集して伸ばしていく)。"""

    hits: list[Hit] = field(default_factory=list)
    message: object | None = None  # 送信済みの discord.Message
    last_time: float = 0.0


def _label(match: Match) -> str:
    return match.keyword.label or match.keyword.name


def build_message(hits: list[Hit], user, show_source: bool = False) -> str | None:
    """反応メッセージを組み立てる。

    - 1件だけならそのキーワードの reply をそのまま使う
    - 複数件なら「N コンボ」の見出し + 各キーワードの label を検出順に並べる
      (発言が1つだけなら見出しの次の行に発言を載せ、複数の発言にまたがる場合は行ごとに載せる)
    - show_source: reply に発言 ({text}) が含まれていなくても、原因になった発言を付記する (VC 用)
    """
    if not hits:
        return None
    if len(hits) == 1:
        hit = hits[0]
        keyword = hit.match.keyword
        if not keyword.reply:
            return None
        message = keyword.reply.format(
            user=user.mention, name=user.display_name, keyword=keyword.name, text=hit.plain_source
        )
        if show_source and "{text}" not in keyword.reply:
            message += f"\n発言: {hit.source}"
        return message[:MAX_MESSAGE_LEN]

    single_source = len({h.source for h in hits}) == 1
    lines = [f"{user.display_name} {len(hits)}コンボ"]
    if single_source:
        lines.append(hits[0].source)
    previous_source = None
    for i, hit in enumerate(hits, start=1):
        line = f"{i}. {_label(hit.match)}"
        if not single_source and hit.source != previous_source:
            line += f"　{hit.source}"
        previous_source = hit.source
        lines.append(line)

    message = ""
    for i, line in enumerate(lines):
        if len(message) + len(line) + 1 > MAX_MESSAGE_LEN - 20:
            message += f"ほか {len(lines) - i}件"
            break
        message += line + "\n"
    return message.rstrip()[:MAX_MESSAGE_LEN]


def hits_in_order(
    text: str, matches: list[Match], key: tuple[int, int] | None = None, context: list[str] | tuple[str, ...] = ()
) -> list[Hit]:
    """1つの発言内の一致を、発言に出てきた順の Hit にする。"""
    return [Hit(text, m, key, tuple(context)) for m in sorted(matches, key=lambda m: m.start)]
