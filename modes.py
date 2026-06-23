"""Generation mode selection for clip-extractor.

Users can toggle clip extraction and chapter text generation independently.
When both modes are enabled, the clip-side prompt wins and feeds the single
detect_highlights call; when only one is enabled, that mode's prompt is used.
"""

from dataclasses import dataclass


@dataclass
class GenerationModes:
    """Which outputs to produce, and which prompts to use for each mode."""

    enable_clips: bool = True
    enable_chapters: bool = True
    clip_prompt: str = ""
    chapter_prompt: str = ""

    def validate(self) -> None:
        """Ensure at least one mode is enabled."""
        if not self.enable_clips and not self.enable_chapters:
            raise ValueError(
                "切り抜きまたは概要欄のどちらかは有効にしてください "
                "(at least one of clip/chapter generation must be enabled)"
            )

    @property
    def active_prompt(self) -> str:
        """Prompt passed to detect_highlights.

        Precedence rule: when clip generation is enabled, its prompt is used —
        even if chapter generation is also enabled. Only a chapters-only run
        uses the chapter prompt.
        """
        self.validate()
        return self.clip_prompt if self.enable_clips else self.chapter_prompt
