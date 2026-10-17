"""Название, ссылки и надпись о программе — в одном месте.

Надпись показывается только в русификаторах, которые создаются «для друзей»
(архив с установщиком): в главном меню игры, в окне установщика и в
«Прочти.txt». В собственную русификацию она не добавляется.
"""

from __future__ import annotations

from .translation import AUTHOR_CONTACT, AUTHOR_TELEGRAM

APP_NAME = "Русификатор игр"
REPO = "sgfsd/game-russificator"
GITHUB_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{GITHUB_URL}/releases/latest"
TELEGRAM = AUTHOR_TELEGRAM
TELEGRAM_URL = AUTHOR_CONTACT

#: надпись в игре — коротко, одной строкой, без рекламы
CREDIT = f"Автоперевод: {APP_NAME} · github.com/{REPO}"
#: то же латиницей — для шрифтов без кириллицы (последний рубеж)
CREDIT_LATIN = f"Auto-translation: Game Russificator · github.com/{REPO}"
