"""Плагин движка Unity.

Перевод подставляется в рантайме через BepInEx + XUnity.AutoTranslator
(см. ``xunity.py``) — так переводится любой текст игры, включая собранный
кодом на лету, в Mono- и IL2CPP-сборках, без правки файлов игры.

Извлечение (``extract.py``) собирает всё, что видно статически, чтобы
перевести это заранее, пачками и с контекстом: компоненты текста в сценах
и ассетах, поля скриптов, строки из кода. Остальное доводится «живым»
переводом: ярлык «Играть на русском» в папке игры запускает игру вместе с
сервером перевода (или пока открыта программа).

Страницы встроенного браузера (ZFBrowser) рисует Chromium, а не Unity, —
их XUnity не видит, поэтому они переводятся прямо в пакете ресурсов.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from ...core.plugin_api import Detection, EnginePlugin, ExportPlan
from ...core.universal import EntryStatus, ExtractionResult, TranslationProject
from . import detect as _detect
from . import extract as _extract
from . import tmp_cyrillic as _tmp
from . import xunity, zfbrowser

PAGE_PREFIX = "zfb:"

log = logging.getLogger("russificator.unity")


class UnityPlugin(EnginePlugin):
    engine_id = "unity"
    title = "Unity"
    runtime_translation = True   # перевод работает и без заранее найденных строк
    supports_live = True         # можно доводить перевод «на лету»

    def detect(self, game_dir: Path) -> Detection:
        conf, details, notes = _detect.detect(Path(game_dir))
        return Detection(confidence=conf, engine_name=self.title, details=details, notes=notes)

    def extract(self, game_dir: Path, project: TranslationProject) -> ExtractionResult:
        game = _detect.inspect(Path(game_dir))
        if game is None:
            return ExtractionResult([], warnings=["Папка *_Data не найдена."])
        project.meta["unity"] = {"backend": game.backend, "arch": game.arch, "version": game.version,
                                 "exe": str(game.exe) if game.exe else ""}
        if game.exe:
            project.meta["game_title"] = game.exe.stem
        entries, warnings, col = _extract.extract_all(game, self.status)
        project.meta["unity_font_files"] = [str(p) for p in col.font_files]
        project.meta["unity_ui"] = col.ui
        project.meta["unity_prefixes"] = col.prefixes
        for e in entries:
            project.add_entry(e)
        if not entries:
            warnings.append("Заранее текст найти не удалось — вся игра будет переводиться на лету.")
        return ExtractionResult(entries, warnings=warnings)

    def inject(self, game_dir: Path, project: TranslationProject) -> None:
        game = _detect.inspect(Path(game_dir))
        if game is None or game.exe is None:
            raise RuntimeError("Не найден .exe игры — перевод Unity-игр поддерживается для Windows-версий.")
        backup = project.backup
        self._il2cpp = game.backend == "il2cpp"
        for note in xunity.install(game, backup, self.status, self.cancel):
            project.warnings.append(note)
        done = [e for e in project.entries
                if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)]
        pairs = {e.source: e.translation for e in done if not e.target_key.startswith(PAGE_PREFIX)}
        pages = [e for e in done if e.target_key.startswith(PAGE_PREFIX)]

        # шрифты: дорисовать кириллицу во все TMP-шрифты игры и проверить результат
        self.status("Проверка шрифтов на кириллицу…", None)
        font_files = [Path(p) for p in project.meta.get("unity_font_files", []) if Path(p).is_file()]
        problems: List[str] = []
        reports: List[_tmp.FontReport] = []
        if font_files:
            gen = _extract._typetree_generator(game, [])
            reports, problems = _tmp.fix_game(font_files, gen, list(pairs.values()), backup, self.status)
            project.meta["unity_fonts"] = [{"file": r.file, "name": r.name, "added": r.added, "error": r.error}
                                           for r in reports]
        fixed = sum(1 for r in reports if r.added)
        if fixed:
            names = ", ".join(sorted({r.name for r in reports if r.added})[:8])
            project.warnings.append(f"Шрифтам игры без кириллицы дорисованы русские буквы ({fixed}): {names}.")
        fallback = None
        if any(r.error for r in reports) or any("шрифты не исправлены" in p for p in problems):
            try:  # запасной вариант — готовый шрифт XUnity (есть для Unity 2018+)
                fallback = xunity.install_tmp_font(game, backup, self.status, self.cancel)
            except Exception as exc:  # noqa: BLE001
                log.warning("TMP-шрифт XUnity: %s", exc)
        project.warnings.extend(problems)
        partial = xunity.partial_translations_fit(pairs)
        usage = self._code_usage(game)
        compat = usage.appends > 0 and usage.length_reveals == 0
        imgui = usage.imgui_calls >= 3
        project.meta["unity_getter_compat"] = compat
        project.meta["unity_imgui"] = imgui
        xunity.write_config(game, backup, live=True, tmp_font=fallback, partial=partial, getter_compat=compat,
                            imgui=imgui)
        if xunity.plugin_installed(game):
            from ...core import launcher
            xunity.write_plugin_config(game, backup, launcher.serve_command(game.root))
        rules = xunity.prefix_rules(project.meta.get("unity_prefixes") or [], pairs)
        n = xunity.write_translations(game, backup, pairs, rules)
        project.meta["unity_static_translations"] = n
        project.meta["unity_backend"] = game.backend
        resized = xunity.write_resizer(game, backup, xunity.resize_rules(project.meta.get("unity_ui") or [], pairs))
        project.meta["unity_resized"] = resized
        if pages:
            self.status("Перевод страниц встроенного браузера…", None)
            project.meta["unity_pages"] = inject_pages(game, backup, pages)
        # плагин сам запускает доводку при любом запуске игры; без плагина — ярлык «Играть на русском»
        self._plugin = xunity.plugin_installed(game)
        self._shortcut = None if self._plugin else self._make_shortcut(game, backup)

    @staticmethod
    def _code_usage(game):
        """Как код игры работает с текстом (печатная машинка, IMGUI) — по IL-коду сборок Mono."""
        from . import il_scan
        total = il_scan.TextUsage()
        if game.backend != "mono":
            return total
        for dll in _extract.game_assemblies(game):
            usage = il_scan.text_usage(dll)
            total.appends += usage.appends
            total.length_reveals += usage.length_reveals
            total.imgui_calls += usage.imgui_calls
        return total

    def _make_shortcut(self, game, backup):
        from ...core import launcher
        lnk = game.root / launcher.SHORTCUT_NAME
        if lnk.exists():
            backup.save_original(lnk)
        else:
            backup.track_new(lnk)
        ok = launcher.create_shortcut(lnk, game.root, game.exe, "Запуск игры с живым переводом (русификатор)")
        return lnk if ok else None

    def install_font(self, game_dir: Path, project: TranslationProject, font_path) -> bool:
        return True  # шрифты — через XUnity (fallback TMP) и системный fallback Unity, см. inject

    def export_plan(self, game_dir: Path, project: TranslationProject, credit: Optional[str]) -> ExportPlan:
        return _export_plan(self, game_dir, project, credit)

    def post_inject_instructions(self) -> List[str]:
        if getattr(self, "_plugin", False) or not getattr(self, "_il2cpp", False):
            first = ("Запускайте игру как обычно (Steam, ярлык, exe): живой перевод текста, который игра "
                     "собирает на лету, включится сам. Не перемещайте и не удаляйте папку русификатора — "
                     "игра запускает его оттуда. Переведённое запоминается.")
        elif getattr(self, "_shortcut", None):
            first = ("Запускайте игру ярлыком «Играть на русском» в папке игры: вместе с игрой включится "
                     "живой перевод текста, который игра собирает на лету. Переведённое запоминается.")
        else:
            first = ("Текст, который игра собирает на лету, переводится, пока русификатор открыт "
                     "(«Живой перевод»). Переведённое запоминается — потом программа не нужна.")
        tips = [
            first,
            "Первый запуск может быть чуть дольше — загружается модуль перевода.",
            "В игре Alt+T переключает оригинал/перевод.",
        ]
        if getattr(self, "_il2cpp", False):
            tips.insert(1, "IL2CPP-игра: первый запуск с BepInEx занимает 1–5 минут и требует интернет — это один раз.")
        return tips


def _export_plan(plugin: "UnityPlugin", game_dir: Path, project: TranslationProject,
                 credit: Optional[str]) -> ExportPlan:
    """Unity: копируются BepInEx, XUnity, словари и плагин; изменённые ассеты (шрифты, страницы
    браузера) — патчами. У друга нет программы, поэтому обращение к её серверу выключается,
    а если его версия игры другая и патч шрифта не встанет — выручит готовый TMP-шрифт XUnity."""
    from ...core import launcher
    root = Path(game_dir).resolve()
    game = _detect.inspect(root)
    plan = ExportPlan(method="files", optional_patches=True)
    plan.exclude = [launcher.SHORTCUT_NAME, "BepInEx/cache/*", "BepInEx/interop/*", "BepInEx/unity-libs/*",
                    "BepInEx/LogOutput.log*", "BepInEx/ErrorLog*", "BepInEx/DumpedAssemblies/*",
                    "BepInEx/Translation/*/Text/_Preprocessors*.log", "*.tmp", "*.rutmp"]
    fonts_patched = any(f.get("added") for f in project.meta.get("unity_fonts") or [])
    fallback = None
    if game is not None and fonts_patched:
        name = xunity.tmp_font_bundle(game)
        if name and (root / name).is_file():
            fallback = name                    # уже лежит в игре (создан при русификации) — скопируется
        elif name:
            try:
                f = xunity.tmp_font_file(game, plugin.status, plugin.cancel)
            except Exception as exc:  # noqa: BLE001
                log.warning("TMP-шрифт XUnity для архива не получен: %s", exc)
                f = None
            if f is not None:
                plan.extra[name] = f
                fallback = name
    ini = root / "BepInEx" / "config" / "AutoTranslatorConfig.ini"
    if ini.is_file():
        text = ini.read_text(encoding="utf-8", errors="replace")
        plan.replace["BepInEx/config/AutoTranslatorConfig.ini"] = \
            xunity.config_for_friends(text, fallback).encode("utf-8")
    if (root / xunity.PLUGIN_CONFIG).is_file() or (game is not None and xunity.plugin_installed(game)):
        plan.replace[xunity.PLUGIN_CONFIG] = xunity.plugin_config_text(None, True, credit or "").encode("utf-8")
    plan.notes.append("Первый запуск игры после установки может быть на 10–30 секунд дольше — "
                      "загружается модуль перевода (BepInEx). Alt+T в игре переключает перевод и оригинал.")
    if game is not None and game.backend == "il2cpp":
        plan.notes.append("Это IL2CPP-игра: при самом первом запуске BepInEx 1–5 минут готовит файлы и "
                          "скачивает библиотеки Unity — нужен интернет. Это происходит один раз.")
    return plan


def inject_pages(game, backup, pages) -> int:
    """Подставить переводы фраз в HTML-страницы пакетов ZFBrowser. Возвращает число страниц."""
    by_pack: Dict[str, Dict[str, Dict[int, str]]] = {}
    for e in pages:
        pack, rest = e.target_key[len(PAGE_PREFIX):].split("|", 1)
        name, idx = rest.rsplit("|", 1)
        by_pack.setdefault(pack, {}).setdefault(name, {})[int(idx)] = e.translation
    changed = 0
    for pack_name, per_page in by_pack.items():
        path = game.data / "Resources" / pack_name
        if not zfbrowser.is_pack(path):
            continue
        header, files = zfbrowser.read_pack(path)
        out = []
        for name, blob in files:
            tr = per_page.get(name)
            decoded = zfbrowser.decode_page_enc(blob) if tr else None
            if decoded is not None:
                page, enc = decoded
                new = zfbrowser.apply_page(page, tr)
                if new != page:
                    blob = new.encode(enc)
                    changed += 1
            out.append((name, blob))
        backup.write_bytes(path, zfbrowser.write_pack(header, out))
    return changed


from ...core.registry import register  # noqa: E402
UnityPlugin = register(UnityPlugin)
