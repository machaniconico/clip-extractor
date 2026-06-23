"""Unit tests for modes.py GenerationModes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modes import GenerationModes


def test_both_enabled_uses_clip_prompt():
    m = GenerationModes(
        enable_clips=True, enable_chapters=True,
        clip_prompt="clip side", chapter_prompt="chapter side",
    )
    assert m.active_prompt == "clip side", (
        f"both enabled should pick clip prompt, got {m.active_prompt!r}"
    )


def test_clips_only_uses_clip_prompt():
    m = GenerationModes(
        enable_clips=True, enable_chapters=False,
        clip_prompt="clip only", chapter_prompt="unused",
    )
    assert m.active_prompt == "clip only", (
        f"clips-only should pick clip prompt, got {m.active_prompt!r}"
    )


def test_chapters_only_uses_chapter_prompt():
    m = GenerationModes(
        enable_clips=False, enable_chapters=True,
        clip_prompt="unused", chapter_prompt="chapter only",
    )
    assert m.active_prompt == "chapter only", (
        f"chapters-only should pick chapter prompt, got {m.active_prompt!r}"
    )


def test_both_disabled_raises_validate():
    m = GenerationModes(enable_clips=False, enable_chapters=False)
    try:
        m.validate()
    except ValueError:
        return
    raise AssertionError("validate() must raise ValueError when both disabled")


def test_both_disabled_raises_on_active_prompt():
    m = GenerationModes(enable_clips=False, enable_chapters=False,
                        clip_prompt="a", chapter_prompt="b")
    try:
        _ = m.active_prompt
    except ValueError:
        return
    raise AssertionError("active_prompt must raise ValueError when both disabled")


def test_default_both_enabled():
    m = GenerationModes()
    assert m.enable_clips is True
    assert m.enable_chapters is True
    assert m.clip_prompt == ""
    assert m.chapter_prompt == ""
    # default state must validate cleanly
    m.validate()


def test_empty_prompts_are_valid():
    m = GenerationModes(enable_clips=True, enable_chapters=True,
                        clip_prompt="", chapter_prompt="")
    # Empty strings are allowed — detect_highlights treats falsy prompt as
    # "no custom prompt", same as before.
    assert m.active_prompt == ""


def run_all():
    test_both_enabled_uses_clip_prompt()
    test_clips_only_uses_clip_prompt()
    test_chapters_only_uses_chapter_prompt()
    test_both_disabled_raises_validate()
    test_both_disabled_raises_on_active_prompt()
    test_default_both_enabled()
    test_empty_prompts_are_valid()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
