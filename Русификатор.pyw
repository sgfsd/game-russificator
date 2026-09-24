# -*- coding: utf-8 -*-
"""Русификатор игр — запуск из исходников (Windows, без консоли).

Проще всего — start.bat: он сам создаёт окружение .venv с компонентами и
запускает этот файл. Если файл открыли другим Python, он перезапустится через
.venv. Готовая сборка без Python — Russificator.exe из раздела Releases.
"""

import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _message(title: str, text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        print(f"{title}: {text}", file=sys.stderr)


def _relaunch_in_venv() -> bool:
    """Открыли не тем Python (без компонентов) — перезапуститься через .venv рядом, если он есть."""
    venv = os.path.join(_ROOT, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(venv):
        return False
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(venv):
        return False
    import subprocess
    subprocess.Popen([venv, os.path.abspath(__file__)] + sys.argv[1:], cwd=_ROOT)
    return True


def main() -> None:
    try:
        from russificator.ui.app import run
    except ImportError as exc:
        if _relaunch_in_venv():
            return
        _message("Русификатор игр",
                 f"Не хватает компонента: {exc.name or exc}.\n\n"
                 "Запустите start.bat в папке программы — он сам установит всё нужное.\n\n"
                 "Или скачайте готовую сборку Russificator.exe (Python не нужен).")
        return
    try:
        run(sys.argv[1:])
    except Exception:  # noqa: BLE001
        log = os.path.join(_ROOT, "russificator_crash.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(traceback.format_exc() + "\n")
        _message("Русификатор игр — ошибка", "Программа завершилась с ошибкой. Подробности:\n" + log)


if __name__ == "__main__":
    main()
