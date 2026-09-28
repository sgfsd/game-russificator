"""Пометка Windows «файл из интернета» (Mark of the Web) на файлах самой программы.

Браузер помечает скачанный архив (скрытый поток ``Zone.Identifier``), а Проводник при
распаковке переносит пометку на каждый файл. .NET Framework отказывается загружать
помеченные сборки — pythonnet падает с «Failed to resolve Python.Runtime.Loader.Initialize»,
и без него не открывается окно программы (pywebview) и не работает распознавание текста.

Собранная программа при запуске снимает пометку со своих файлов — то же самое, что
«Свойства → Разблокировать» у архива. Если снять не удалось (нет прав на запись в папку
программы), пользователь получает понятное объяснение вместо трассировки.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger("russificator.unblock")

STREAM = ":Zone.Identifier"

#: сборки .NET, без которых программа не работает: по ним видно, помечены ли файлы
PROBES = ("pythonnet/runtime/Python.Runtime.dll", "webview/lib/Microsoft.Web.WebView2.Core.dll",
          "webview/lib/Microsoft.Web.WebView2.WinForms.dll")

MESSAGE = (
    "Windows заблокировала файлы программы, потому что архив скачан из интернета, "
    "а снять блокировку в папке «{folder}» программа не смогла (нет прав на запись).\n\n"
    "Как исправить (один раз):\n"
    "1. Нажмите правой кнопкой на скачанный архив Russificator-win64.zip → «Свойства».\n"
    "2. Внизу поставьте галочку «Разблокировать» → «ОК».\n"
    "3. Распакуйте архив заново — в обычную папку (Загрузки, Документы, рабочий стол), не в Program Files."
)


def marked(path: Path) -> bool:
    """На файле есть пометка «из интернета»."""
    try:
        return os.path.exists(str(path) + STREAM)
    except (OSError, ValueError):
        return False


def unblock_tree(root: Path, recursive: bool = True) -> Tuple[int, int]:
    """Снять пометку со всех файлов папки. Возвращает (снято, не удалось)."""
    removed = failed = 0
    root = Path(root)
    walker: Iterable = os.walk(root) if recursive else [(str(root), [], [p.name for p in root.iterdir()
                                                                        if p.is_file()])]
    for folder, _dirs, files in walker:
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + STREAM)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                failed += 1
    return removed, failed


def bundle_dir() -> Optional[Path]:
    """Папка с файлами собранной программы (``_internal``) или None при запуске из исходников."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(sys.executable).parent


def ensure_unblocked(base: Optional[Path] = None) -> Optional[str]:
    """Снять пометку «из интернета» с файлов программы, если она есть.

    Возвращает объяснение для пользователя, если сборки .NET так и остались заблокированными."""
    if sys.platform != "win32":
        return None
    base = Path(base) if base is not None else bundle_dir()
    if base is None or not base.is_dir():
        return None
    probes = [base / p for p in PROBES]
    if not any(marked(p) for p in probes):
        return None
    removed, failed = unblock_tree(base)
    exe_dir = Path(sys.executable).parent
    if exe_dir != base and exe_dir.is_dir():
        r2, f2 = unblock_tree(exe_dir, recursive=False)
        removed, failed = removed + r2, failed + f2
    log.info("пометка «из интернета» снята с файлов программы: %d (не удалось: %d)", removed, failed)
    still: List[Path] = [p for p in probes if marked(p)]
    if still:
        log.warning("файлы программы остались заблокированы: %s", ", ".join(str(p) for p in still))
        return MESSAGE.format(folder=exe_dir)
    return None
