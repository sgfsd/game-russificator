"""Точка входа программы (окно). Используется сборкой exe и командой `russificator-gui`."""

import sys


def main() -> None:
    from russificator.ui.app import run
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
