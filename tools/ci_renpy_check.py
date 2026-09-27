"""Проверка русификации Ren'Py настоящим движком (шаг CI, нужен интернет и Linux с Xvfb).

Для каждой версии Ren'Py:
  1. скачать SDK, взять демо-игру «The Question», скомпилировать её в .rpyc и убрать
     исходники .rpy — как в выпущенных играх;
  2. русифицировать программой (пайплайн целиком, переводчик-заглушка «Ру: …»);
  3. собрать архив «для друзей» с надписью о программе и установить его в чистую копию;
  4. ``renpy.sh <игра> lint`` — проверка скриптов и экранов;
  5. запустить игру под Xvfb: главное меню (надпись о программе), начало игры (реплики через
     наш фильтр, подгонка шрифта), нажатие Alt+T (переключение оригинала), выход.
     Любая ошибка Ren'Py — это traceback.txt/errors.txt, проверка падает.

    python tools/ci_renpy_check.py 8.3.7 7.4.11
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DRIVER = r'''
## Проверка CI: ведёт игру сама и пишет, что видела.
init 3000 python:
    import io as _ci_io
    import os as _ci_os

    def _ci_write(name, text):
        with _ci_io.open(_ci_os.path.join(config.basedir, name), "a", encoding="utf-8") as f:
            f.write(u"%s\n" % (text,))

    _ci_prev = config.say_menu_text_filter

    def _ci_filter(s):
        r = _ci_prev(s) if _ci_prev is not None else s
        _ci_write("ci_said.txt", r)
        return r

    config.say_menu_text_filter = _ci_filter

    def _ci_press_alt_t():
        import pygame_sdl2 as pg
        pg.event.post(pg.event.Event(pg.KEYDOWN, key=pg.K_t, scancode=0, mod=pg.KMOD_LALT, unicode=u"t",
                                     repeat=False))
        pg.event.post(pg.event.Event(pg.KEYUP, key=pg.K_t, scancode=0, mod=pg.KMOD_LALT))

    def _ci_state(tag):
        on = globals().get("_ru_on")
        credit = renpy.get_screen("russificator_credit") is not None
        _ci_write("ci_state.txt", u"%s ru_on=%s credit=%s" % (tag, on[0] if on else None, credit))

    preferences.afm_enable = True
    preferences.afm_time = 1
    preferences.text_cps = 0

    if hasattr(config, "always_shown_screens"):
        config.always_shown_screens.append("ci_driver")
    else:
        config.overlay_screens.append("ci_driver")

screen ci_driver():
    zorder 2000
    if main_menu:
        timer 2.5 action [Function(_ci_state, "menu"), Start()]
    else:
        timer 3.0 action Function(_ci_state, "game")
        timer 4.0 action Function(_ci_press_alt_t)
        timer 5.5 action Function(_ci_state, "after_alt_t")
        timer 9.0 action Quit(confirm=False)
'''


def fetch_sdk(version: str, dest: Path) -> Path:
    url = f"https://www.renpy.org/dl/{version}/renpy-{version}-sdk.tar.bz2"
    print(f"[{version}] скачиваю {url}", flush=True)
    data = urllib.request.urlopen(url, timeout=300).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tf:
        tf.extractall(dest)
    sdk = next(p for p in dest.iterdir() if p.is_dir() and p.name.startswith("renpy-"))
    return sdk


def renpy(sdk: Path, game: Path, *args: str, timeout: int = 300, xvfb: bool = False) -> subprocess.CompletedProcess:
    cmd = [str(sdk / "renpy.sh"), str(game), *args]
    if xvfb:
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24"] + cmd
    env = dict(os.environ, SDL_AUDIODRIVER="dummy")
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, -9, exc.stdout or "", (exc.stderr or "") + "\nTIMEOUT")


def errors_of(game: Path) -> str:
    out = []
    for name in ("traceback.txt", "errors.txt"):
        f = game / name
        if f.is_file():
            out.append(f"--- {name}\n" + f.read_text(encoding="utf-8", errors="replace")[-4000:])
    return "\n".join(out)


def check(version: str, work: Path) -> bool:
    from russificator.core.package import export_package, install_package, open_package
    from russificator.core.pipeline import Pipeline, PipelineOptions
    from russificator.translation.base import Translator

    class Fake(Translator):
        cache_id = "ci:fake"
        title = "ci"

        def translate(self, entries, glossary):
            return {e.id: "Ру: " + e.source for e in entries}

    root = work / version
    root.mkdir(parents=True)
    sdk = fetch_sdk(version, root)
    game = root / "TheQuestion"
    shutil.copytree(sdk / "the_question", game)
    r = renpy(sdk, game, "compile")
    print(f"[{version}] compile: код {r.returncode}", flush=True)
    rpyc = list((game / "game").rglob("*.rpyc"))
    if not rpyc:
        print(r.stdout[-2000:], r.stderr[-2000:])
        return False
    for f in (game / "game").rglob("*.rpy"):
        f.unlink()                                  # как в выпущенной игре: только .rpyc
    for d in ("cache", "saves"):
        shutil.rmtree(game / "game" / d, ignore_errors=True)
    friend = root / "Friend" / "TheQuestion"
    shutil.copytree(game, friend)

    res = Pipeline(Fake(), PipelineOptions(use_memory=False)).run(game)
    print(f"[{version}] русификация: {res.summary() if hasattr(res, 'summary') else res.success}", flush=True)
    if not res.success:
        return False
    z = export_package(game, root / "out", credit=True)
    pkg = open_package(Path(z["path"]))
    try:
        install_package(pkg, friend)
    finally:
        pkg.close()
    (friend / "game" / "zz_ci_driver.rpy").write_text(DRIVER, encoding="utf-8")

    ok = True
    r = renpy(sdk, friend, "lint", timeout=300)
    lint = (r.stdout or "") + (r.stderr or "")
    lint_errors = [ln for ln in lint.splitlines() if "zz_russificator" in ln or "russificator" in ln.lower()]
    print(f"[{version}] lint: код {r.returncode}; строки про русификатор: {lint_errors or 'нет'}", flush=True)
    err = errors_of(friend)
    if r.returncode != 0 or err or lint_errors:
        print(lint[-3000:])
        print(err)
        ok = False
    for name in ("traceback.txt", "errors.txt"):
        (friend / name).unlink(missing_ok=True)

    started = time.monotonic()
    r = renpy(sdk, friend, timeout=120, xvfb=True)
    print(f"[{version}] запуск: код {r.returncode} за {time.monotonic() - started:.0f} с", flush=True)
    err = errors_of(friend)
    said = (friend / "ci_said.txt").read_text(encoding="utf-8") if (friend / "ci_said.txt").is_file() else ""
    state = (friend / "ci_state.txt").read_text(encoding="utf-8") if (friend / "ci_state.txt").is_file() else ""
    print(f"[{version}] состояние:\n{state}")
    print(f"[{version}] реплики (первые):\n" + "\n".join(said.splitlines()[:8]))
    if err:
        print(err)
        ok = False
    if "Ру:" not in said:
        print(f"[{version}] ПРОВАЛ: реплики не переведены")
        print((r.stdout or "")[-2000:], (r.stderr or "")[-2000:])
        log = friend / "log.txt"
        if log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace")[-3000:])
        ok = False
    if "menu" in state and "credit=True" not in state.splitlines()[0]:
        print(f"[{version}] ПРОВАЛ: надписи о программе нет в главном меню")
        ok = False
    if "after_alt_t ru_on=False" not in state:
        print(f"[{version}] ПРОВАЛ: Alt+T не переключил перевод")
        ok = False
    print(f"[{version}] ИТОГ: {'OK' if ok else 'FAIL'}", flush=True)
    return ok


def main() -> int:
    versions = sys.argv[1:] or ["8.3.7"]
    work = Path(tempfile.mkdtemp(prefix="renpy-ci-"))
    os.environ.setdefault("RUSSIFICATOR_HOME", str(work / "home"))
    results = {}
    for v in versions:
        try:
            results[v] = check(v, work)
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            results[v] = False
    print("ИТОГ:", ", ".join(f"Ren'Py {k}={'OK' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
