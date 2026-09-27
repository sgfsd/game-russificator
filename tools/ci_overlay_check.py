"""Проверка живого перевода на настоящей Windows (шаг CI, не блокирует сборку).

Рисует картинку с английским текстом и распознаёт её обоими способами
(pythonnet и PowerShell), затем проверяет окна оверлея: прозрачное окно,
трей, захват экрана. Всё печатается в журнал сборки — по нему видно, что
работает на Windows «вживую», а что нет.

    python tools/ci_overlay_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PHRASE = "Press any key to continue"


def frame():
    from PIL import Image, ImageDraw
    from russificator.overlay.render import font
    img = Image.new("RGB", (900, 220), (18, 20, 28))
    d = ImageDraw.Draw(img)
    d.text((40, 40), PHRASE, font=font(40, bold=False), fill=(240, 240, 240))
    d.text((40, 120), "Welcome back, traveler!", font=font(34, bold=False), fill=(250, 220, 120))
    return img.width, img.height, img.convert("RGBA").tobytes("raw", "BGRA")


def check_ocr() -> bool:
    from russificator.overlay import ocr
    w, h, data = frame()
    ok_any = False
    for name, make in (("pythonnet", lambda: ocr.DotNetOcr("en")),
                       ("powershell", lambda: ocr.PowerShellOcr("en", Path(tempfile.gettempdir())))):
        started = time.monotonic()
        try:
            engine = make()
        except ocr.OcrUnavailable as exc:
            print(f"[ocr:{name}] недоступно ({exc.code}): {exc}")
            continue
        except Exception:  # noqa: BLE001
            print(f"[ocr:{name}] ошибка запуска:\n{traceback.format_exc()}")
            continue
        try:
            lines = engine.recognize(w, h, data)
            text = " | ".join(f"{ln.text} @({ln.x},{ln.y},{ln.w}x{ln.h})" for ln in lines)
            good = any(PHRASE.lower() in ln.text.lower() for ln in lines)
            ok_any |= good
            print(f"[ocr:{name}] {'OK' if good else 'НЕ ТО'} за {time.monotonic() - started:.2f} с, "
                  f"язык {engine.lang}, предел {engine.max_dim}: {text or '(пусто)'}")
        except Exception:  # noqa: BLE001
            print(f"[ocr:{name}] ошибка распознавания:\n{traceback.format_exc()}")
        finally:
            engine.close()
    try:
        best = ocr.create("en", Path(tempfile.gettempdir()))
        print(f"[ocr] create() выбрал: {best.name}")
        best.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[ocr] create(): {exc}")
    return ok_any


def check_windows() -> bool:
    from russificator.overlay import render, win32
    win32.set_dpi_aware()
    fg = win32.foreground()
    print(f"[win32] окно переднего плана: {fg}")
    shot = win32.capture(0, 0, 64, 64)
    print(f"[win32] захват экрана 64x64: {'OK' if shot and len(shot) == 64 * 64 * 4 else shot}")
    icon = Path(__file__).resolve().parents[1] / "russificator" / "resources" / "icon.ico"
    events = []
    gui = win32.Gui(str(icon), events.append, events.append, events.append, lambda: [("quit", "Выход", False)])
    gui.start()
    ok = bool(gui.host and gui.overlay)
    print(f"[win32] окна оверлея созданы: {ok}, исключено из захвата: {gui.excluded_from_capture}")
    if ok:
        img = render.render((400, 120), [render.Item(rect=(10, 10, 300, 30), text="Нажмите любую клавишу",
                                                     line_h=30)])
        gui.show_frame(100, 100, 400, 120, render.to_bgra_premultiplied(img))
        time.sleep(0.5)
        print(f"[win32] оверлей показан: {gui.visible}")
        gui.set_hotkeys([win32.Hotkey(1, win32.MOD_ALT, 0x54, "toggle")])
        gui.balloon("Проверка", "Живой перевод")
        time.sleep(0.3)
        print(f"[win32] горячие клавиши: {[h.name for h in gui._hotkeys.values()]}")
        gui.hide()
        gui.stop()
    print(f"[win32] окно программы (поиск по exe): {win32.find_program_window('Русификатор игр')}")
    return ok


def check_live() -> bool:
    """Сквозная проверка службы: полноэкранное окно «игры» с английским текстом → служба сама
    решает, что это игра, снимает кадр, распознаёт, переводит и показывает плашку."""
    import subprocess
    import threading
    from russificator.overlay import ocr, service, win32
    pyw = Path(sys.executable).with_name("pythonw.exe")
    code = ("import tkinter as tk\n"
            "r = tk.Tk(); r.configure(bg='black'); r.attributes('-fullscreen', True)\n"
            "tk.Label(r, text='Press any key to continue', fg='white', bg='black', font=('Arial', 30))"
            ".place(relx=0.5, rely=0.8, anchor='center')\n"
            "r.after(300, lambda: (r.lift(), r.focus_force())); r.after(40000, r.destroy); r.mainloop()\n")
    game = subprocess.Popen([str(pyw if pyw.is_file() else sys.executable), "-c", code])
    try:
        time.sleep(4)
        fg = win32.foreground()
        print(f"[live] окно переднего плана: {fg.exe if fg else None} на весь экран: {fg.fullscreen if fg else None}")
        svc = service.LiveService(quiet=True)
        icon = Path(__file__).resolve().parents[1] / "russificator" / "resources" / "icon.ico"
        svc.gui = win32.Gui(str(icon), svc._on_hotkey, svc._on_menu, svc._on_region, svc._menu_items)
        svc.gui.start()
        svc.ocr = ocr.create("en", Path(tempfile.gettempdir()))

        class Tr:
            cache_id = "ci:live"

            def translate(self, entries, glossary):
                return {e.id: "Нажмите любую клавишу" for e in entries}

            def close(self):
                pass
        svc.translator, svc._reload_translator = Tr(), False
        threading.Thread(target=svc._translate_loop, daemon=True).start()
        started = time.monotonic()
        while time.monotonic() - started < 20:
            svc._tick()
            if svc.count and svc.gui.visible:
                break
            time.sleep(0.4)
        st = svc.status()
        print(f"[live] игра: {st['game']}, распознавание: {st['ocr']['name']}, переведено: {st['count']}, "
              f"плашка на экране: {svc.gui.visible}, за {time.monotonic() - started:.1f} с, "
              f"последние: {st['recent'][:2]}")
        ok = bool(st["game"]) and st["count"] > 0 and svc.gui.visible
        svc.stop_event.set()
        svc.gui.stop()
        svc.ocr.close()
        return ok
    finally:
        game.kill()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):     # консоль CI в cp1252 — русские сообщения не должны ронять проверку
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    if sys.platform != "win32":
        print("только для Windows")
        return 0
    results = {}
    os.environ.setdefault("RUSSIFICATOR_HOME", tempfile.mkdtemp(prefix="ci-live-"))
    for name, fn in (("ocr", check_ocr), ("windows", check_windows), ("live", check_live)):
        try:
            results[name] = fn()
        except Exception:  # noqa: BLE001
            print(f"[{name}] упало:\n{traceback.format_exc()}")
            results[name] = False
    print("ИТОГ:", ", ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
