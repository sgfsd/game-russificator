"""Командная строка русификатора.

Примеры:
    russificator detect "C:/Games/MyGame"
    russificator run "C:/Games/MyGame"                                  # машинный перевод
    russificator run "C:/Games/MyGame" --mode local --local-model gemma4-e4b
    russificator run "C:/Games/MyGame" --mode cloud --provider deepseek --api-key KEY
    russificator models --provider gemini --api-key KEY                 # список моделей
    russificator restore "C:/Games/MyGame"
    russificator play "C:/Games/MyGame"          # игра + живой перевод (Unity)
    russificator export "C:/Games/MyGame" --out "D:/Русификаторы"   # архив для друзей
    russificator install "D:/Игра — русификатор.zip" "C:/Games/MyGame"
    russificator games                           # установленные игры (Steam, GOG, Epic…)
    russificator order
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

import russificator
from russificator.translation.cloud import PROVIDERS
from russificator.translation.local_llm import PRESETS


def _add_translator_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=["machine", "local", "cloud"], default="machine",
                   help="machine — машинный офлайн (слабые ПК); local — нейросеть на ПК; "
                        "cloud — облачная нейросеть по ключу")
    p.add_argument("--local-model", default="", choices=[""] + [x.id for x in PRESETS],
                   help="модель для --mode local (по умолчанию подбирается по железу)")
    p.add_argument("--local-gguf", default="", help="свой .gguf-файл для --mode local")
    p.add_argument("--cpu", action="store_true", help="--mode local: не использовать видеокарту")
    p.add_argument("--provider", default="deepseek", choices=list(PROVIDERS), help="провайдер для --mode cloud")
    p.add_argument("--api-key", default=os.environ.get("RUSSIFICATOR_API_KEY", ""),
                   help="API-ключ (или переменная окружения RUSSIFICATOR_API_KEY)")
    p.add_argument("--base-url", default="", help="свой адрес API (…/v1)")
    p.add_argument("--model", default="", help="имя модели провайдера")
    p.add_argument("--parallel", type=int, default=4, help="одновременных запросов к API")
    p.add_argument("--font", default="", help="свой шрифт .ttf/.otf с кириллицей")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="russificator",
                                     description="Универсальный авторусификатор игр (Unity, Ren'Py, RPG Maker)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("detect", help="определить движок игры")
    p.add_argument("game_dir")

    p = sub.add_parser("run", help="русифицировать игру")
    p.add_argument("game_dir")
    _add_translator_args(p)

    p = sub.add_parser("restore", help="откатить русификацию")
    p.add_argument("game_dir")

    p = sub.add_parser("play", help="запустить игру с живым переводом (Unity; способ перевода — из настроек)")
    p.add_argument("game_dir")

    p = sub.add_parser("models", help="актуальные модели облачного провайдера")
    p.add_argument("--provider", default="deepseek", choices=list(PROVIDERS))
    p.add_argument("--api-key", default=os.environ.get("RUSSIFICATOR_API_KEY", ""))
    p.add_argument("--base-url", default="")

    p = sub.add_parser("export", help="создать архив-русификатор для друзей (из русифицированной игры)")
    p.add_argument("game_dir")
    p.add_argument("--out", default=".", help="куда сохранить архив")
    p.add_argument("--no-credit", action="store_true", help="без надписи о программе в игре")

    p = sub.add_parser("install", help="установить архив-русификатор в игру")
    p.add_argument("package", help="архив .zip (или распакованная папка)")
    p.add_argument("game_dir")

    sub.add_parser("games", help="установленные игры на компьютере")

    sub.add_parser("order", help="заказать качественный перевод у автора")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")
    for name in ("urllib3",):
        logging.getLogger(name).setLevel(logging.WARNING)

    if args.cmd == "detect":
        try:
            plugin, det = russificator.detect_engine(args.game_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"Движок не распознан: {exc}")
            return 2
        print(f"Движок: {det.engine_name or plugin.title} (уверенность {det.confidence:.0%})")
        if det.details:
            print(f"Признаки: {det.details}")
        for n in det.notes:
            print(f"Примечание: {n}")
        return 0

    if args.cmd == "run":
        return _run(args)

    if args.cmd == "restore":
        from russificator.core.restore import restore_backups
        count, notes = restore_backups(Path(args.game_dir))
        print("\n".join(notes))
        return 0 if count else 1

    if args.cmd == "play":
        from russificator import play
        return play.play(Path(args.game_dir))

    if args.cmd == "models":
        from russificator.translation.cloud import list_models
        try:
            for m in list_models(args.provider, args.api_key, args.base_url):
                print(m)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось получить список моделей: {exc}")
            return 2
        return 0

    if args.cmd == "export":
        from russificator.core.package import PackageError, export_package
        try:
            res = export_package(Path(args.game_dir), Path(args.out), credit=not args.no_credit,
                                 status=lambda m, f=None: print(m))
        except PackageError as exc:
            print(f"Ошибка: {exc}")
            return 2
        print(f"Готово: {res['path']} ({res['size'] // 1024} КБ)", *res.get("notes", []), sep="\n")
        return 0

    if args.cmd == "install":
        from russificator.core.package import PackageError, check_game, install_package, open_package
        try:
            pkg = open_package(Path(args.package))
        except PackageError as exc:
            print(f"Ошибка: {exc}")
            return 2
        try:
            chk = check_game(pkg, Path(args.game_dir))
            print(chk.message)
            if not chk.ok:
                return 2
            res = install_package(pkg, Path(args.game_dir), status=lambda m, f=None: print(m))
            print("Готово.", *res.get("notes", []), sep="\n")
            return 0
        except PackageError as exc:
            print(f"Ошибка: {exc}")
            return 1
        finally:
            pkg.close()

    if args.cmd == "games":
        from russificator.library import discover
        for g in discover():
            print(f"{g.title:40.40}  {g.to_dict()['engine_title']:18.18}  {g.path}")
        return 0

    if args.cmd == "order":
        print("Нужен качественный ручной перевод? Напишите автору в Telegram: "
              f"@{russificator.ORDER_TELEGRAM} — {russificator.ORDER_CONTACT}")
        return 0
    return 0


def _run(args) -> int:
    from russificator.core.pipeline import Pipeline, PipelineOptions
    from russificator.translation import TranslatorError
    options = {
        "local_model": args.local_model, "local_gpu": not args.cpu, "local_custom_model": args.local_gguf,
        "cloud_provider": args.provider, "api_key": args.api_key, "cloud_base_url": args.base_url,
        "cloud_model": args.model, "cloud_parallel": args.parallel,
    }
    try:
        translator = russificator.create_translator(args.mode, options, Path(args.game_dir).name)
    except TranslatorError as exc:
        print(f"Ошибка: {exc}")
        return 2

    state = {"last": ""}

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "stage":
            print(f"\n== {ev['title']}")
        elif t in ("progress", "status"):
            msg = ev.get("message", "")
            if msg != state["last"]:
                state["last"] = msg
                sys.stdout.write("\r" + msg[:100].ljust(100))
                sys.stdout.flush()
        elif t == "log" and ev.get("level") != "info":
            print(f"\n{ev['message']}")

    cancel = threading.Event()
    pipeline = Pipeline(translator, PipelineOptions(font_path=Path(args.font) if args.font else None),
                        on_event=on_event, cancel=cancel)
    try:
        result = pipeline.run(args.game_dir)
    except KeyboardInterrupt:
        cancel.set()
        print("\nОстановлено.")
        return 130
    print("\n\n" + result.summary())
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
