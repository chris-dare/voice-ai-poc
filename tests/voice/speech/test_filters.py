from voice_ai.voice.speech.filters import StatefulThinkStripper, _looks_like_raw_json


def test_strips_fragmented_think_block() -> None:
    stripper = StatefulThinkStripper()
    chunks = ["Hello ", "<thi", "nk>private", " reasoning</th", "ink>", " world."]

    output = "".join(stripper.feed(chunk) for chunk in chunks) + stripper.finish()

    assert output == "Hello  world."
    assert "private" not in output


def test_strips_thinking_variant_and_unclosed_content() -> None:
    stripper = StatefulThinkStripper()

    assert stripper.feed("Safe.<thinking>never speak this") == "Safe."
    assert stripper.finish() == ""


def test_preserves_partial_non_tag_text_at_end() -> None:
    stripper = StatefulThinkStripper()

    assert stripper.feed("The comparison is 2 < 3") == "The comparison is 2 < 3"
    assert stripper.finish() == ""


def test_raw_json_detection_only_blocks_complete_json() -> None:
    assert _looks_like_raw_json('{"balance": "GHS 42.50"}')
    assert not _looks_like_raw_json("Your balance is GHS 42.50.")
    assert not _looks_like_raw_json("{not valid")
