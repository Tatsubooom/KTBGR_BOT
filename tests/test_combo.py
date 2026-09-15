from types import SimpleNamespace

from ktbgr.combo import Hit, build_message, hits_in_order
from ktbgr.matcher import Keyword, KeywordMatcher

USER = SimpleNamespace(mention="<@1>", display_name="テスター")


def test_multiple_keywords_in_one_utterance():
    matcher = KeywordMatcher(
        [Keyword("やりますねぇ"), Keyword("頭にきますよ"), Keyword("当たり前だよなぁ？"), Keyword("ラーメン")], 70
    )
    found = matcher.find("頭にきますよ、当たり前だよなぁ？やりますねぇ")
    assert {m.keyword.name for m in found} == {"やりますねぇ", "頭にきますよ", "当たり前だよなぁ？"}


def test_overlapping_matches_are_counted_once():
    # 「めちゃくちゃだよ」は「あーもうめちゃくちゃだよ」と同じ箇所に一致するので1件として数える
    matcher = KeywordMatcher([Keyword("めちゃくちゃだよ"), Keyword("あーもうめちゃくちゃだよ")], 70)
    text = "あーもうめちゃくちゃだよ"
    assert len(matcher.find(text, allow_overlap=True)) == 2
    assert [m.keyword.name for m in matcher.find(text)] == ["あーもうめちゃくちゃだよ"]


def test_short_romaji_does_not_match_across_mora():
    matcher = KeywordMatcher([Keyword("おつ")], 70)
    assert matcher.find("バイト疲れたー") == []


def test_single_match_uses_reply():
    matcher = KeywordMatcher([Keyword("ラーメン", reply="{name}「{text}」")], 70)
    text = "ラーメン食べたい"
    assert build_message(hits_in_order(text, matcher.find(text)), USER) == "テスター「ラーメン食べたい」"


def test_combo_message_in_order_of_appearance():
    matcher = KeywordMatcher([Keyword("寝落ち"), Keyword("ラーメン", label="ラーメン（食べ物）")], 70)
    text = "ラーメン食べてから寝落ちした"
    message = build_message(hits_in_order(text, matcher.find(text)), USER)
    assert message.splitlines() == [
        "テスター 2コンボ",
        "「ラーメン食べてから寝落ちした」",
        "1. ラーメン（食べ物）",
        "2. 寝落ち",
    ]


def test_combo_across_utterances_shows_each_text():
    matcher = KeywordMatcher([Keyword("頭にきますよ"), Keyword("やりますねぇ"), Keyword("やったぜ")], 70)
    hits: list[Hit] = []
    for text in ["頭に来ますよ", "やりますね", "やったぜ"]:
        hits += hits_in_order(text, matcher.find(text))
    assert build_message(hits, USER).splitlines() == [
        "テスター 3コンボ",
        "1. 頭にきますよ　「頭に来ますよ」",
        "2. やりますねぇ　「やりますね」",
        "3. やったぜ　「やったぜ」",
    ]


def test_voice_message_shows_source_utterance():
    matcher = KeywordMatcher([Keyword("おはよう", reply="おはようございます、{name}さん")], 70)
    hits = hits_in_order("おはよう", matcher.find("おはよう"), key=(1, 1))
    # テキストチャット: reply のまま
    assert build_message(hits, USER) == "おはようございます、テスターさん"
    # VC: reply に発言が含まれていなければ付記する
    assert build_message(hits, USER, show_source=True) == "おはようございます、テスターさん\n発言: 「おはよう」"


def test_voice_source_includes_only_context_used():
    matcher = KeywordMatcher([Keyword("ガチャン！ゴン！", reply="{name}「{text}」")], 70)
    hits = hits_in_order("ゴン", matcher.find("がちゃん ゴン"), key=(1, 2), context=["がちゃん"])
    assert build_message(hits, USER, show_source=True) == "テスター「がちゃん ゴン」"
    assert hits[0].source == "「がちゃん」「ゴン」"


def test_voice_message_is_updated_with_final_transcript():
    import asyncio

    import bot as botmod
    from ktbgr.combo import ComboState

    sent = []

    class FakeMessage:
        async def edit(self, content, **kwargs):
            sent.append(content)

    class FakeChannel:
        id = 10

    matcher = KeywordMatcher([Keyword("ケツの穴がない！", reply="{name}「{text}」")], 70)
    state = ComboState(hits=hits_in_order("穴がない", matcher.find("ケツの穴がない"), key=(1, 5)), message=FakeMessage())
    fake_bot = SimpleNamespace(
        _voice_combos={(10, 1): state}, _combo_lock=asyncio.Lock(), _response_channel=lambda vc: FakeChannel()
    )
    fake_bot._send_or_edit_combo = lambda c, s, u: botmod.KTBGRBot._send_or_edit_combo(fake_bot, c, s, u)
    user = SimpleNamespace(id=1, mention="<@1>", display_name="テスター")

    # 途中経過 (穴がない) で反応していたメッセージが、確定版の文字起こしに差し替わる
    asyncio.run(botmod.KTBGRBot.update_voice_transcript(fake_bot, None, user, "ケツの穴がない", (1, 5)))
    assert sent == ["テスター「ケツの穴がない」"]
    # 別の発話の確定では変わらない
    asyncio.run(botmod.KTBGRBot.update_voice_transcript(fake_bot, None, user, "別の発言", (1, 6)))
    assert len(sent) == 1


def test_no_emoji_or_markdown_in_generated_messages():
    import json
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent
    entries = []
    for path in (root / "data" / "inmu_goroku.json", root / "data" / "hikamani_goroku.json", root / "keywords.json"):
        entries += json.loads(path.read_text(encoding="utf-8"))["keywords"]
    decorated = re.compile(r"[\U0001F300-\U0001FAFF☀-➿️→]|\*\*")
    for entry in entries:
        for key in ("reply", "label"):
            # 語録のタイトルや発言者名自体に含まれる記号 (「うんちして♡」「YOU THE ROCK★」など) は原文なので対象外
            template = (entry.get(key) or "").replace(entry.get("title", "\0"), "").replace(entry.get("speaker", "\0"), "")
            assert not decorated.search(template), entry
