"""Точка входа установщика русификатора (Установить.exe в архивах «для друзей»)."""

import sys


def main() -> None:
    from russificator.installer.app import main as run
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
