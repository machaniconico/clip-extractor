"""Generation mode selection for clip-extractor.

Users can toggle normal clips, Shorts, and chapter text independently. When a
video output is enabled, the clip-side prompt feeds the single
detect_highlights call; a chapters-only run uses the chapter prompt.
"""

from dataclasses import dataclass


@dataclass
class GenerationModes:
    """Which outputs to produce, and which prompts to use for each mode."""

    enable_clips: bool = True
    enable_shorts: bool = False
    enable_chapters: bool = True
    clip_prompt: str = ""
    chapter_prompt: str = ""

    def validate(self) -> None:
        """Ensure at least one output is enabled."""
        if (
            not self.enable_clips
            and not self.enable_shorts
            and not self.enable_chapters
        ):
            raise ValueError(
                "切り抜き・ショート・タイムスタンプの少なくとも1つは有効にしてください "
                "(at least one of clip/short/chapter generation must be enabled)"
            )

    @property
    def active_prompt(self) -> str:
        """Prompt passed to detect_highlights.

        Precedence rule: when normal clip or Shorts generation is enabled, the
        clip prompt is used. Only a chapters-only run uses the chapter prompt.
        """
        self.validate()
        return (
            self.clip_prompt
            if self.enable_clips or self.enable_shorts
            else self.chapter_prompt
        )
