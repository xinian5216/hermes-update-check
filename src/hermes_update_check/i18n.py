"""Tiny two-language (zh/en) translation helper.

The project ships with Chinese and English text; every user-facing string is
written as ``tr.t("中文", "English")`` so both languages stay in sync in the
source file instead of drifting apart in a translation table.
"""

from __future__ import annotations

SUPPORTED_LANGUAGES = ("zh", "en")


class Translator:
    """Pick one of the two strings based on the configured language."""

    def __init__(self, lang: str = "zh") -> None:
        self.lang = lang if lang in SUPPORTED_LANGUAGES else "zh"

    @property
    def is_zh(self) -> bool:
        return self.lang == "zh"

    def t(self, zh: str, en: str) -> str:
        """Return the configured-language variant of a message."""
        return zh if self.is_zh else en

    def set_lang(self, lang: str) -> None:
        if lang in SUPPORTED_LANGUAGES:
            self.lang = lang

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Translator(lang={self.lang!r})"


#: Module level default translator; the CLI swaps the language once config is loaded.
DEFAULT = Translator("zh")
