"""Окно установщика русификатора (Tk) и режим командной строки.

    Установить.exe                      окно: найти игру, установить / удалить
    Установить.exe --game <папка>       окно с уже выбранной папкой игры
    Установить.exe --game <папка> --install [--quiet]   установить сразу (после
                                        перезапуска с правами администратора)
    Установить.exe --game <папка> --uninstall --quiet   удалить без окна
    Установить.exe --selftest           проверка сборки
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Any, Callable, List, Optional

log = logging.getLogger("russificator.installer")

C = {
    "bg": "#0d0f14", "bg2": "#11141b", "panel": "#161a23", "panel2": "#1c2130", "panel3": "#232939",
    "border": "#262c3b", "border2": "#333b4f", "text": "#e8eaf0", "muted": "#8d95a8", "faint": "#5d6579",
    "accent": "#8b7bff", "accent_hi": "#a092ff", "accent2": "#ff6b9a", "ok": "#3ddc97", "warn": "#ffb547",
    "danger": "#ff5d6c", "tg": "#2aabee",
}


def _base_dir() -> Path:
    """Папка распакованного архива: рядом с exe (или текущая папка при запуске из исходников)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _prepare_env() -> Path:
    """Временные файлы установщика — в %TEMP%, а не рядом с архивом."""
    home = Path(tempfile.gettempdir()) / "RussificatorSetup"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["RUSSIFICATOR_HOME"] = str(home)
    handler = logging.FileHandler(home / "setup.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return home


def _resource(name: str) -> Optional[Path]:
    p = Path(__file__).resolve().parents[1] / "resources" / name
    return p if p.exists() else None


def _is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _relaunch_as_admin(args: List[str]) -> bool:
    """Перезапустить установщик с правами администратора (UAC). True — запущен."""
    if sys.platform != "win32":
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, args
    else:
        exe, params = sys.executable, ["-m", "russificator.installer", *args]
    line = subprocess.list2cmdline(params)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, line, str(_base_dir()), 1)
    return rc > 32


def _launch(path: Path) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.Popen([str(path)], cwd=str(path.parent))
        else:
            subprocess.Popen([str(path)], cwd=str(path.parent))
        return True
    except OSError:
        return False


def _game_exe(pkg, game_dir: Path) -> Optional[Path]:
    exe = str(pkg.game.get("exe") or "")
    if exe and (game_dir / exe).is_file():
        return game_dir / exe
    from ..library import main_exe
    return main_exe(game_dir)


# ============================================================================ окно

class App:
    def __init__(self, root, base: Path, preset_game: Optional[str], auto_install: bool):
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        self.root = root
        self.base = base
        self.pkg = None
        self.game: Optional[Path] = None
        self.check = None
        self.busy = False
        self.auto_install = auto_install
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)

        families = set(tkfont.families(root))
        fam = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "DejaVu Sans") if f in families),
                   "TkDefaultFont")
        semi = next((f for f in ("Segoe UI Semibold", "Segoe UI Variable Display Semib", fam) if f in families), fam)
        self.f_small = tkfont.Font(family=fam, size=9)
        self.f_text = tkfont.Font(family=fam, size=10)
        self.f_bold = tkfont.Font(family=semi, size=10, weight="bold" if semi == fam else "normal")
        self.f_title = tkfont.Font(family=semi, size=17, weight="bold" if semi == fam else "normal")
        self.f_big = tkfont.Font(family=semi, size=14, weight="bold" if semi == fam else "normal")

        root.title("Русификатор — установка")
        root.configure(bg=C["bg"])
        root.resizable(False, False)
        w, h = self.px(640), self.px(580)
        root.geometry(f"{w}x{h}+{max(0, (root.winfo_screenwidth() - w) // 2)}+"
                      f"{max(0, (root.winfo_screenheight() - h) // 3)}")
        ico = _resource("icon.ico")
        if ico is not None and sys.platform == "win32":
            try:
                root.iconbitmap(default=str(ico))
            except Exception:  # noqa: BLE001
                pass
        self.logo = None
        logo = _resource("installer/logo96.png" if self.s >= 1.5 else "installer/logo48.png")
        if logo is not None:
            try:
                self.logo = tk.PhotoImage(file=str(logo))
            except Exception:  # noqa: BLE001
                self.logo = None

        self._build()
        root.after(40, self._pump)
        threading.Thread(target=self._load, args=(preset_game,), daemon=True).start()

    def px(self, n: float) -> int:
        return int(round(n * self.s))

    # ------------------------------------------------------------------ разметка

    def _build(self) -> None:
        tk, px = self.tk, self.px
        root = self.root
        # --- шапка
        head = tk.Frame(root, bg=C["panel"], highlightthickness=0)
        head.pack(fill="x")
        stripe = tk.Canvas(head, height=px(4), bg=C["panel"], highlightthickness=0, bd=0)
        stripe.pack(fill="x")
        stripe.bind("<Configure>", lambda e: self._gradient(stripe, e.width, px(4)))
        inner = tk.Frame(head, bg=C["panel"])
        inner.pack(fill="x", padx=px(26), pady=(px(18), px(18)))
        if self.logo is not None:
            tk.Label(inner, image=self.logo, bg=C["panel"]).pack(side="left", padx=(0, px(16)))
        txt = tk.Frame(inner, bg=C["panel"])
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text="РУСИФИКАТОР", font=self.f_small, fg=C["accent_hi"], bg=C["panel"]).pack(anchor="w")
        self.title_lbl = tk.Label(txt, text="Загрузка…", font=self.f_title, fg=C["text"], bg=C["panel"],
                                  anchor="w", justify="left", wraplength=px(470))
        self.title_lbl.pack(anchor="w", fill="x")
        self.meta_lbl = tk.Label(txt, text="", font=self.f_small, fg=C["muted"], bg=C["panel"], anchor="w",
                                 justify="left", wraplength=px(470))
        self.meta_lbl.pack(anchor="w", fill="x", pady=(px(2), 0))

        # --- подвал (раньше тела — чтобы тело заняло остаток)
        foot = tk.Frame(root, bg=C["bg2"])
        foot.pack(side="bottom", fill="x")
        tk.Frame(foot, bg=C["border"], height=1).pack(fill="x")
        fin = tk.Frame(foot, bg=C["bg2"])
        fin.pack(fill="x", padx=px(26), pady=px(14))
        from .. import branding
        tk.Label(fin, text=f"Автоперевод: {branding.APP_NAME} — бесплатно, в пару кликов",
                 font=self.f_small, fg=C["muted"], bg=C["bg2"]).pack(anchor="w")
        links = tk.Frame(fin, bg=C["bg2"])
        links.pack(anchor="w", pady=(px(4), 0))
        self._link(links, f"Ручной перевод на заказ — Telegram @{branding.TELEGRAM}", branding.TELEGRAM_URL,
                   C["tg"]).pack(side="left")
        tk.Label(links, text="  ·  ", font=self.f_small, fg=C["faint"], bg=C["bg2"]).pack(side="left")
        self._link(links, "Скачать программу", branding.RELEASES_URL, C["accent_hi"]).pack(side="left")

        # --- тело
        self.body = tk.Frame(root, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=px(26), pady=(px(20), px(12)))
        self._build_main()
        self._build_done()
        self.done.pack_forget()

    def _gradient(self, canvas, width: int, height: int) -> None:
        canvas.delete("all")
        a, b = (0x8b, 0x7b, 0xff), (0xff, 0x6b, 0x9a)
        steps = max(1, width // 4)
        for i in range(steps):
            t = i / steps
            col = "#%02x%02x%02x" % tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))
            canvas.create_rectangle(i * 4, 0, i * 4 + 4, height, fill=col, width=0)

    def _link(self, parent, text: str, url: str, color: str):
        lbl = self.tk.Label(parent, text=text, font=self.f_small, fg=color, bg=parent["bg"], cursor="hand2")
        lbl.bind("<Button-1>", lambda e: webbrowser.open(url))
        lbl.bind("<Enter>", lambda e: lbl.configure(font=self._underlined(self.f_small)))
        lbl.bind("<Leave>", lambda e: lbl.configure(font=self.f_small))
        return lbl

    def _underlined(self, font):
        f = font.copy()
        f.configure(underline=True)
        return f

    def _build_main(self) -> None:
        tk, px = self.tk, self.px
        self.main = tk.Frame(self.body, bg=C["bg"])
        self.main.pack(fill="both", expand=True)
        tk.Label(self.main, text="Папка игры", font=self.f_bold, fg=C["text"], bg=C["bg"]).pack(anchor="w")
        box = tk.Frame(self.main, bg=C["panel"], highlightbackground=C["border"], highlightthickness=1)
        box.pack(fill="x", pady=(px(8), 0))
        row = tk.Frame(box, bg=C["panel"])
        row.pack(fill="x", padx=px(14), pady=px(12))
        self.dot = tk.Canvas(row, width=px(12), height=px(12), bg=C["panel"], highlightthickness=0)
        self.dot.pack(side="left", padx=(0, px(10)))
        self.path_lbl = tk.Label(row, text="Ищу игру на компьютере…", font=self.f_text, fg=C["muted"],
                                 bg=C["panel"], anchor="w", justify="left", wraplength=px(400))
        self.path_lbl.pack(side="left", fill="x", expand=True)
        self.pick_btn = Button(self, row, "Изменить…", self.pick_folder, kind="ghost", bg=C["panel"])
        self.pick_btn.pack(side="right", padx=(px(10), 0))
        self.status_lbl = tk.Label(self.main, text="", font=self.f_small, fg=C["muted"], bg=C["bg"], anchor="w",
                                   justify="left", wraplength=px(580))
        self.status_lbl.pack(anchor="w", fill="x", pady=(px(8), 0))

        # что сделает установщик — коротко и по делу
        self.facts = tk.Frame(self.main, bg=C["bg"])
        self.facts.pack(fill="x", pady=(px(18), 0))
        self.fact_lbls = []
        for text in ("Переведёт текст игры на русский", "Подключит шрифт с русскими буквами, если его нет в игре",
                     "Сохранит оригиналы файлов — удалить можно этим же установщиком"):
            row = tk.Frame(self.facts, bg=C["bg"])
            row.pack(fill="x", pady=px(3))
            mark = tk.Canvas(row, width=px(16), height=px(16), bg=C["bg"], highlightthickness=0)
            mark.create_line(px(3), px(8), px(7), px(12), px(13), px(4), fill=C["ok"], width=px(2),
                             capstyle="round", joinstyle="round")
            mark.pack(side="left", padx=(0, px(10)))
            lbl = tk.Label(row, text=text, font=self.f_text, fg=C["muted"], bg=C["bg"], anchor="w",
                           justify="left", wraplength=px(550))
            lbl.pack(side="left", fill="x")
            self.fact_lbls.append(lbl)

        self.actions = tk.Frame(self.main, bg=C["bg"])
        self.actions.pack(fill="x", pady=(px(22), 0))
        self.install_btn = Button(self, self.actions, "Установить русификатор", self.install, kind="primary",
                                  big=True)
        self.install_btn.pack(side="left")
        self.remove_btn = Button(self, self.actions, "Удалить", self.uninstall, kind="ghost")
        self.remove_btn.pack(side="left", padx=(px(10), 0))

        self.prog = tk.Frame(self.main, bg=C["bg"])
        self.bar = tk.Canvas(self.prog, height=px(8), bg=C["bg"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(px(18), px(6)))
        self.bar.bind("<Configure>", lambda e: self._draw_bar())
        self.prog_lbl = tk.Label(self.prog, text="", font=self.f_small, fg=C["muted"], bg=C["bg"], anchor="w")
        self.prog_lbl.pack(anchor="w", fill="x")
        self._frac: Optional[float] = 0.0
        self._phase = 0

        self.note_lbl = tk.Label(self.main, text="", font=self.f_small, fg=C["faint"], bg=C["bg"], anchor="w",
                                 justify="left", wraplength=px(580))
        self.note_lbl.pack(anchor="w", fill="x", side="bottom")

    def _build_done(self) -> None:
        tk, px = self.tk, self.px
        self.done = tk.Frame(self.body, bg=C["bg"])
        self.done.pack(fill="both", expand=True)
        self.done_icon = tk.Canvas(self.done, width=px(64), height=px(64), bg=C["bg"], highlightthickness=0)
        self.done_icon.pack(pady=(px(6), px(12)))
        self.done_title = tk.Label(self.done, text="", font=self.f_big, fg=C["text"], bg=C["bg"])
        self.done_title.pack()
        self.done_text = tk.Label(self.done, text="", font=self.f_text, fg=C["muted"], bg=C["bg"],
                                  justify="center", wraplength=px(540))
        self.done_text.pack(pady=(px(8), px(18)))
        btns = tk.Frame(self.done, bg=C["bg"])
        btns.pack()
        self.play_btn = Button(self, btns, "Играть", self.play, kind="primary", big=True)
        self.play_btn.pack(side="left")
        Button(self, btns, "Закрыть", self.root.destroy, kind="ghost").pack(side="left", padx=(px(10), 0))

    def _draw_icon(self, ok: bool) -> None:
        c, px = self.done_icon, self.px
        c.delete("all")
        col = C["ok"] if ok else C["danger"]
        c.create_oval(px(2), px(2), px(62), px(62), fill=C["panel"], outline=col, width=px(3))
        if ok:
            c.create_line(px(19), px(33), px(28), px(42), px(46), px(23), fill=col, width=px(5),
                          capstyle="round", joinstyle="round")
        else:
            c.create_line(px(22), px(22), px(42), px(42), fill=col, width=px(5), capstyle="round")
            c.create_line(px(42), px(22), px(22), px(42), fill=col, width=px(5), capstyle="round")

    def _draw_bar(self) -> None:
        c = self.bar
        c.delete("all")
        w, h = max(1, c.winfo_width()), self.px(8)
        c.create_rectangle(0, 0, w, h, fill=C["panel3"], width=0)
        if self._frac is None:
            seg = w // 3
            x = (self._phase * 8) % (w + seg) - seg
            c.create_rectangle(max(0, x), 0, min(w, x + seg), h, fill=C["accent"], width=0)
        else:
            c.create_rectangle(0, 0, int(w * max(0.0, min(1.0, self._frac))), h, fill=C["accent"], width=0)

    def _set_dot(self, color: str) -> None:
        self.dot.delete("all")
        self.dot.create_oval(self.px(1), self.px(1), self.px(11), self.px(11), fill=color, width=0)

    # ------------------------------------------------------------------ фоновые задачи

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                getattr(self, "_on_" + kind)(payload)
        except queue.Empty:
            pass
        if self.busy and self._frac is None:
            self._phase += 1
            self._draw_bar()
        self.root.after(40, self._pump)

    def _post(self, kind: str, payload: Any = None) -> None:
        self.events.put((kind, payload))

    def _load(self, preset: Optional[str]) -> None:
        from ..core.package import PackageError, check_game, find_game, open_package
        try:
            pkg = open_package(self.base)
        except PackageError as exc:
            self._post("fatal", f"{exc}\n\nСначала распакуйте архив целиком (правый клик → «Извлечь всё»), "
                                "затем запустите «Установить.exe» из распакованной папки.")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("архив не открыт")
            self._post("fatal", f"Архив русификатора не читается: {exc}")
            return
        self._post("package", pkg)
        game = None
        if preset:
            game = Path(preset)
        else:
            cands = find_game(pkg, near=self.base)
            game = next((c for c in cands if check_game(pkg, c).ok), None)
        if game is None:
            self._post("game", (None, None))
            return
        self._post("game", (game, check_game(pkg, game)))

    def _on_fatal(self, text: str) -> None:
        self.title_lbl.configure(text="Не получилось открыть русификатор")
        self._show_done(False, "Архив не найден", text, play=False)

    def _on_package(self, pkg) -> None:
        self.pkg = pkg
        g = pkg.game
        st = pkg.info.get("stats") or {}
        self.root.title(f"Русификатор — {pkg.title}")
        self.title_lbl.configure(text=pkg.title)
        meta = [str(g.get("engine_title") or pkg.engine)]
        if st.get("total"):
            total = int(st.get("total", 0) or 0)
            word = "строки" if total % 10 == 1 and total % 100 != 11 else "строк"
            meta.append(f"переведено {_n(st.get('translated', 0))} из {_n(total)} {word}")
        if pkg.info.get("translator"):
            meta.append(str(pkg.info["translator"]))
        self.meta_lbl.configure(text=" · ".join(meta))
        if st.get("translated"):
            self.fact_lbls[0].configure(text=f"Переведёт текст игры на русский (строк: {_n(st['translated'])})")
        notes = [n for n in pkg.info.get("notes") or [] if n]
        if notes:
            self.note_lbl.configure(text="\n".join("• " + n for n in notes[:3]))

    def _on_game(self, payload) -> None:
        game, check = payload
        self.game, self.check = game, check
        self._refresh()
        if self.auto_install and game is not None and check is not None and check.ok:
            self.auto_install = False
            self.install()

    def _refresh(self) -> None:
        from ..core.package import installed_info
        from ..core.backup import GameBackup
        if self.game is None:
            self._set_dot(C["warn"])
            self.path_lbl.configure(text="Игра не найдена автоматически", fg=C["text"])
            self.status_lbl.configure(text="Нажмите «Изменить…» и выберите папку игры — ту, где лежит её .exe.",
                                      fg=C["warn"])
            self.install_btn.set_enabled(False)
            self.remove_btn.hide()
            self.pick_btn.set_text("Выбрать папку…")
            return
        self.pick_btn.set_text("Изменить…")
        self.path_lbl.configure(text=str(self.game), fg=C["text"])
        ok = bool(self.check and self.check.ok)
        exact = bool(self.check and self.check.exact)
        self._set_dot(C["ok"] if exact else C["warn"] if ok else C["danger"])
        msg = self.check.message if self.check else ""
        info = installed_info(self.game) if ok else None
        russified = ok and GameBackup(self.game).exists
        if info:
            msg += f"\nРусификатор уже установлен ({info.get('title', '')}). Можно переустановить или удалить."
        elif russified:
            msg += "\nИгра уже русифицирована программой — установка заменит этот перевод."
        self.status_lbl.configure(text=msg, fg=C["muted"] if exact else C["warn"] if ok else C["danger"])
        self.install_btn.set_enabled(ok and not self.busy)
        self.install_btn.set_text("Переустановить" if info else "Установить русификатор")
        if russified:
            self.remove_btn.show()
            self.remove_btn.set_enabled(not self.busy)
        else:
            self.remove_btn.hide()

    # ------------------------------------------------------------------ действия

    def pick_folder(self) -> None:
        if self.busy or self.pkg is None:
            return
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=self.root, title="Папка игры (где лежит её .exe)",
                                    initialdir=str(self.game or Path.home()))
        if not d:
            return
        from ..core.package import check_game
        game = Path(d)
        self.game, self.check = game, check_game(self.pkg, game)
        self._refresh()

    def _start(self, text: str) -> None:
        self.busy = True
        self._frac = None
        self.prog.pack(fill="x", after=self.actions)
        self.prog_lbl.configure(text=text)
        self.install_btn.set_enabled(False)
        self.remove_btn.set_enabled(False)
        self.pick_btn.set_enabled(False)

    def _stop(self) -> None:
        self.busy = False
        self.prog.pack_forget()
        self.pick_btn.set_enabled(True)
        self._refresh()

    def install(self) -> None:
        if self.busy or self.pkg is None or self.game is None:
            return
        from tkinter import messagebox
        if self.check is not None and self.check.ok and not self.check.exact:
            if not messagebox.askyesno("Русификатор", self.check.message + "\n\nУстановить всё равно?",
                                       parent=self.root):
                return
        self._start("Установка…")
        pkg, game = self.pkg, self.game

        def work():
            from ..core.package import PackageError, install_package
            try:
                res = install_package(pkg, game, status=lambda m, f=None: self._post("progress", (m, f)))
                self._post("installed", res)
            except PackageError as exc:
                self._post("failed", (str(exc), exc.code))
            except Exception as exc:  # noqa: BLE001
                log.exception("установка упала")
                self._post("failed", (f"Непредвиденная ошибка: {exc}", ""))
        threading.Thread(target=work, daemon=True).start()

    def uninstall(self) -> None:
        if self.busy or self.game is None:
            return
        from tkinter import messagebox
        if not messagebox.askyesno("Удалить русификатор?",
                                   "Игре вернутся оригинальные файлы, всё добавленное русификатором будет удалено.",
                                   parent=self.root):
            return
        self._start("Удаление…")
        game = self.game

        def work():
            from ..core.package import uninstall
            try:
                count, notes = uninstall(game)
                self._post("removed", notes)
            except Exception as exc:  # noqa: BLE001
                log.exception("удаление упало")
                self._post("failed", (f"Удалить не получилось: {exc}", ""))
        threading.Thread(target=work, daemon=True).start()

    def play(self) -> None:
        if self.pkg is None or self.game is None:
            return
        exe = _game_exe(self.pkg, self.game)
        if exe is None or not _launch(exe):
            from tkinter import messagebox
            messagebox.showinfo("Русификатор", "Запустите игру как обычно — через Steam, ярлык или её .exe.",
                                parent=self.root)
            return
        self.root.after(600, self.root.destroy)

    # ------------------------------------------------------------------ события задач

    def _on_progress(self, payload) -> None:
        msg, frac = payload
        self._frac = frac
        self.prog_lbl.configure(text=msg)
        self._draw_bar()

    def _on_installed(self, res) -> None:
        self.busy = False
        notes = [n for n in res.get("notes") or [] if n]
        text = "Запускайте игру как обычно — через Steam, ярлык или её .exe.\nAlt+T в игре переключает перевод и оригинал."
        if notes:
            text += "\n\n" + "\n".join(notes[:3])
        self._show_done(True, "Готово! Игра на русском.", text, play=True)

    def _on_removed(self, notes) -> None:
        self.busy = False
        self._show_done(True, "Русификатор удалён", "Игра возвращена к оригиналу.", play=False)

    def _on_failed(self, payload) -> None:
        text, code = payload
        self._stop()
        from tkinter import messagebox
        if code == "access" and not _is_admin() and sys.platform == "win32":
            if messagebox.askyesno("Нужны права администратора",
                                   text + "\n\nПерезапустить установщик от имени администратора?", parent=self.root):
                args = ["--game", str(self.game), "--install"]
                if _relaunch_as_admin(args):
                    self.root.destroy()
                    return
            return
        messagebox.showerror("Не получилось", text, parent=self.root)

    def _show_done(self, ok: bool, title: str, text: str, play: bool) -> None:
        self.main.pack_forget()
        self.done.pack(fill="both", expand=True)
        self._draw_icon(ok)
        self.done_title.configure(text=title)
        self.done_text.configure(text=text)
        if play:
            self.play_btn.show()
        else:
            self.play_btn.hide()


def _n(v: Any) -> str:
    try:
        return f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


class Button:
    """Плоская кнопка в стиле программы (tk.Label с наведением и нажатием)."""

    def __init__(self, app: App, parent, text: str, command: Callable[[], None], kind: str = "primary",
                 big: bool = False, bg: Optional[str] = None):
        tk = app.tk
        self.app = app
        self.kind = kind
        self.command = command
        self.enabled = True
        self.visible = True
        self.parent_bg = bg or parent["bg"]
        pad = (app.px(22), app.px(11)) if big else (app.px(14), app.px(7))
        self.w = tk.Label(parent, text=text, font=app.f_bold, cursor="hand2", padx=pad[0], pady=pad[1],
                          highlightthickness=1)
        self._pack_args: tuple = ((), {})
        self._paint(False)
        self.w.bind("<Enter>", lambda e: self._paint(True))
        self.w.bind("<Leave>", lambda e: self._paint(False))
        self.w.bind("<Button-1>", lambda e: self.enabled and self.command())

    def _paint(self, hover: bool) -> None:
        if self.kind == "primary":
            bg = C["accent_hi"] if hover and self.enabled else C["accent"]
            fg, border = "#ffffff", bg
            if not self.enabled:
                bg, fg, border = C["panel3"], C["faint"], C["panel3"]
        else:
            bg = C["panel2"] if hover and self.enabled else self.parent_bg
            fg, border = (C["text"], C["border2"]) if self.enabled else (C["faint"], C["border"])
        self.w.configure(bg=bg, fg=fg, highlightbackground=border, highlightcolor=border,
                         cursor="hand2" if self.enabled else "arrow")

    def pack(self, *args, **kwargs) -> "Button":
        self._pack_args = (args, kwargs)
        self.w.pack(*args, **kwargs)
        return self

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self._paint(False)

    def set_text(self, text: str) -> None:
        self.w.configure(text=text)

    def hide(self) -> None:
        if self.visible:
            self.w.pack_forget()
            self.visible = False

    def show(self) -> None:
        if not self.visible:
            args, kwargs = self._pack_args
            self.w.pack(*args, **kwargs)
            self.visible = True


# ============================================================================ запуск

def selftest() -> int:
    lines = []
    ok = True
    for mod in ("russificator.core.package", "russificator.core.delta", "russificator.library",
                "russificator.engines.renpy.plugin", "russificator.engines.rpgmaker.plugin",
                "russificator.engines.unity.plugin", "tkinter"):
        try:
            __import__(mod)
            lines.append(f"ok   {mod}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"FAIL {mod}: {exc}")
    from ..fonts.library import ensure_font
    for name, good in (("font", ensure_font() is not None), ("logo", _resource("installer/logo48.png") is not None)):
        ok &= good
        lines.append(("ok   " if good else "FAIL ") + name)
    lines.append("RESULT " + ("OK" if ok else "FAIL"))
    out = Path(os.environ.get("RUSSIFICATOR_HOME", tempfile.gettempdir())) / "selftest.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    if sys.stdout is not None:
        print("\n".join(lines))
    return 0 if ok else 1


def _cli(args, base: Path) -> int:
    from ..core.package import PackageError, check_game, install_package, open_package, uninstall
    game = Path(args.game)
    if args.uninstall:
        count, notes = uninstall(game)
        print("\n".join(notes))
        return 0
    pkg = open_package(Path(args.package) if args.package else base)
    try:
        chk = check_game(pkg, game)
        if not chk.ok:
            print(chk.message, file=sys.stderr)
            return 2
        res = install_package(pkg, game, status=lambda m, f=None: print(m))
        print("Готово.", *res.get("notes", []), sep="\n")
        return 0
    except PackageError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        pkg.close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="Установить", description="Установка русификатора игры")
    p.add_argument("--package", default="", help="папка распакованного архива (по умолчанию — рядом с exe)")
    p.add_argument("--game", default="", help="папка игры")
    p.add_argument("--install", action="store_true", help="сразу установить")
    p.add_argument("--uninstall", action="store_true", help="удалить русификатор")
    p.add_argument("--quiet", action="store_true", help="без окна")
    p.add_argument("--selftest", action="store_true")
    args, _ = p.parse_known_args(argv)
    _prepare_env()
    if args.selftest:
        return selftest()
    base = Path(args.package) if args.package else _base_dir()
    if args.quiet and args.game:
        return _cli(args, base)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    try:
        import tkinter as tk
    except ImportError:
        print("Окно недоступно: нет tkinter. Используйте --game <папка> --install --quiet", file=sys.stderr)
        return 3
    root = tk.Tk()
    try:
        App(root, base, args.game or None, auto_install=bool(args.install and args.game))
        root.mainloop()
    except Exception:  # noqa: BLE001
        log.exception("окно установщика упало")
        _message(traceback.format_exc())
        return 1
    return 0


def _message(text: str) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text[-1500:], "Русификатор — ошибка", 0x10)
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
