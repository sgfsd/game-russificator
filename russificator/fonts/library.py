"""Кириллический шрифт по умолчанию.

PT Sans (ParaType, лицензия SIL OFL 1.1) лежит прямо в программе
(``resources/fonts``, вместе с текстом лицензии) — ничего скачивать не
нужно, ссылки не протухнут. Пользователь может указать свой .ttf/.otf.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

FONTS = Path(__file__).resolve().parent.parent / "resources" / "fonts"
DEFAULT_REGULAR = "PT_Sans-Web-Regular.ttf"
DEFAULT_BOLD = "PT_Sans-Web-Bold.ttf"


def ensure_font(bold: bool = False) -> Optional[Path]:
    """Путь к встроенному кириллическому шрифту."""
    p = FONTS / (DEFAULT_BOLD if bold else DEFAULT_REGULAR)
    return p if p.is_file() else None


def license_file() -> Optional[Path]:
    p = FONTS / "OFL.txt"
    return p if p.is_file() else None
