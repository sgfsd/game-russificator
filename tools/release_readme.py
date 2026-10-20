"""«Прочти.txt» для архива самой программы (Russificator-win64.zip).

Сборка в GitHub Actions кладёт его рядом с Russificator.exe:
    python tools/release_readme.py dist/Russificator
"""

from __future__ import annotations

import sys
from pathlib import Path

import russificator
from russificator import branding
from russificator.core.package import README_NAME, contacts_lines


def release_readme() -> str:
    lines = [
        f"{branding.APP_NAME} {russificator.__version__}",
        "=" * 60,
        "Бесплатная программа: переводит игры на Unity, Ren'Py и RPG Maker на русский язык,",
        "а любые другие — живым переводом поверх окна игры.",
        "",
        "КАК ЗАПУСТИТЬ",
        "1. Распакуйте архив целиком в любую папку (не запускайте прямо из архива).",
        "2. Запустите Russificator.exe.",
        "",
        "Если Windows показывает «Система Windows защитила ваш компьютер» — нажмите",
        "«Подробнее» → «Выполнить в любом случае». Программа не подписана сертификатом,",
        "это обычное предупреждение для бесплатных программ.",
        "",
        "ОБНОВЛЕНИЯ И ИНСТРУКЦИЯ",
        f"    {branding.GITHUB_URL}",
        "",
        "ВОПРОСЫ И ОШИБКИ",
    ] + contacts_lines()
    return "\n".join(lines) + "\n"


def write(folder: Path) -> Path:
    path = folder / README_NAME
    path.write_bytes(("﻿" + release_readme()).replace("\n", "\r\n").encode("utf-8"))
    return path


if __name__ == "__main__":
    write(Path(sys.argv[1]))      # без print: консоль сборки Windows может не знать кириллицу
