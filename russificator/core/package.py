"""Русификатор «для друзей»: архив с установщиком и его установка.

Кнопка «Создать файл русификатора» собирает из уже русифицированной игры
архив ``<Игра> — русификатор.zip``::

    <Игра> — русификатор/
        Установить.exe          установщик (он же удаляет русификацию)
        Прочти.txt              что это, как установить, контакты
        Лицензии/               BepInEx, XUnity.AutoTranslator, PT Sans
        russificator-data/
            package.json        что за игра, чем переведено, что делать при установке
            translations.json   перевод строк (оригинал → перевод, адрес строки)
            files/…             файлы, которые русификатор добавляет в игру
            patches/…           бинарные патчи изменённых файлов игры

Файлов самой игры в архиве нет: только то, что добавил русификатор, патчи
(«что поменять») и сам перевод. Как именно ставить, решает движок
(:class:`~russificator.core.plugin_api.ExportPlan`): Ren'Py и Unity —
копированием файлов и патчами, RPG Maker — повторным внедрением перевода в
копию игры друга (подходит к любой её версии).

Установка пишет всё через :class:`GameBackup` — удалить русификацию можно и
установщиком, и полной программой («Откатить»).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import branding, paths
from .backup import BACKUP_DIR, GameBackup
from .universal import EntryStatus, TranslationProject

log = logging.getLogger("russificator.package")

FORMAT = 1
DATA_DIR = "russificator-data"
MANIFEST = "package.json"
TRANSLATIONS = "translations.json"
INSTALLER_NAME = "Установить.exe"
README_NAME = "Прочти.txt"
LICENSES_DIR = "Лицензии"
#: изменённый файл, для которого патч не получился, кладётся целиком, если он не больше этого
FULL_FILE_LIMIT = 48 << 20

StatusFn = Callable[[str, Optional[float]], None]


def _noop(message: str, fraction: Optional[float] = None) -> None:
    pass


class PackageError(Exception):
    """Архив не создать или не установить. ``code`` — для интерфейса (например, "access")."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


# ====================================================================== создание

def translator_title(translator_id: str) -> str:
    """Человеческое описание способа перевода по id из проекта ("CloudTranslator:…")."""
    kind, _, rest = (translator_id or "").partition(":")
    model = rest.split(":")[-1] if rest else ""
    if kind.startswith("Machine"):
        return "машинный перевод"
    if kind.startswith("Local"):
        name = model[:-5] if model.lower().endswith(".gguf") else model
        return "нейросеть на ПК" + (f" ({name})" if name else "")
    if kind.startswith("Cloud"):
        return "облачная нейросеть" + (f" ({model})" if model else "")
    return "автоматический перевод"


def can_export(game_dir: Path) -> Tuple[bool, str]:
    """Можно ли создать архив: игра русифицирована и проект перевода на месте."""
    from .pipeline import project_dir
    game_dir = Path(game_dir)
    backup = GameBackup(game_dir)
    if not backup.exists:
        return False, "Сначала русифицируйте игру — архив делается из готовой русификации."
    info = backup.info or {}
    if info.get("package"):
        return False, "Эта игра русифицирована установщиком из чужого архива — пересоздайте русификацию программой."
    if not (project_dir(game_dir) / TranslationProject.PROJECT_FILE).is_file():
        return False, "Не найден проект перевода этой игры — русифицируйте её заново."
    return True, ""


def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s).strip(" .")
    return re.sub(r"\s{2,}", " ", s)[:80] or "Игра"


def _walk(game_dir: Path, rel: str) -> List[str]:
    p = game_dir / rel
    if p.is_dir():
        return sorted(f.relative_to(game_dir).as_posix() for f in p.rglob("*") if f.is_file())
    return [rel] if p.is_file() else []


def _excluded(rel: str, patterns: List[str]) -> bool:
    """Путь подходит под шаблон: целиком (``*`` захватывает и вложенные папки), папка под
    шаблон «папка/*», а шаблон без «/» сверяется с именем файла."""
    rel_l = rel.lower().strip("/")
    name = rel_l.rsplit("/", 1)[-1]
    for pat in patterns:
        pat_l = pat.lower()
        if fnmatch.fnmatchcase(rel_l, pat_l) or fnmatch.fnmatchcase(rel_l + "/", pat_l):
            return True
        if "/" not in pat_l and fnmatch.fnmatchcase(name, pat_l):
            return True
    return False


def export_package(game_dir: Path, out_dir: Path, credit: bool = True, status: StatusFn = _noop,
                   cancel=None) -> Dict[str, Any]:
    """Собрать архив-русификатор. Возвращает {"path", "size", "notes"}."""
    import russificator
    from ..fonts.library import license_file
    from .delta import make_patch, sha1_file
    from .pipeline import project_dir
    from .registry import detect_engine

    game_dir = Path(game_dir).resolve()
    ok, why = can_export(game_dir)
    if not ok:
        raise PackageError(why)
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageError(f"Папка для архива недоступна: {exc}", "access") from exc

    status("Подготовка…", None)
    project = TranslationProject.load(project_dir(game_dir))
    plugin, det = detect_engine(game_dir)
    if plugin.engine_id != project.engine:
        raise PackageError("Движок игры не совпадает с проектом перевода — русифицируйте игру заново.")
    plugin.status = lambda m, f=None: status(m, f)
    plugin.cancel = cancel
    backup = GameBackup(game_dir)
    credit_text = branding.CREDIT if credit else None
    plan = plugin.export_plan(game_dir, project, credit_text)
    notes: List[str] = []

    title = _safe_name(str(project.meta.get("game_title") or game_dir.name))
    folder_name = f"{title} — русификатор"
    stage_root = Path(tempfile.mkdtemp(prefix="export-", dir=str(paths.sub("tmp"))))
    try:
        top = stage_root / folder_name
        data = top / DATA_DIR
        (data / "files").mkdir(parents=True)

        # --- перевод строк (нужен для «reinject» и для импорта в программу)
        done = [e for e in project.entries
                if e.translation and e.status in (EntryStatus.TRANSLATED, EntryStatus.APPROVED)]
        tr_rows = [{"k": e.target_key, "s": e.source, "t": e.translation, "st": e.status.value} for e in done]
        (data / TRANSLATIONS).write_text(json.dumps(tr_rows, ensure_ascii=False, separators=(",", ":")),
                                         encoding="utf-8")

        files: List[Dict[str, Any]] = []
        created: List[str] = []
        patches: List[Dict[str, Any]] = []
        full: List[Dict[str, Any]] = []

        def put(rel: str, src: Optional[Path] = None, content: Optional[bytes] = None) -> None:
            dest = data / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if content is not None:
                dest.write_bytes(content)
            else:
                shutil.copyfile(src, dest)
            files.append({"path": rel, "size": dest.stat().st_size})

        if plan.method == "files":
            status("Сбор файлов русификации…", None)
            for rel in backup.created:
                if _excluded(rel, plan.exclude) or not (game_dir / rel).exists():
                    continue
                created.append(rel)
                for f in _walk(game_dir, rel):
                    if _excluded(f, plan.exclude) or f in plan.replace:
                        continue
                    put(f, game_dir / f)
            modified = [r for r in backup.modified if not _excluded(r, plan.exclude)]
            for n, rel in enumerate(modified):
                if cancel is not None and cancel.is_set():
                    raise PackageError("Отменено.")
                if rel in plan.replace:
                    continue
                cur, orig = game_dir / rel, backup.root / rel
                if not cur.is_file() or not orig.is_file():
                    continue
                status(f"Патч: {Path(rel).name}", n / max(1, len(modified)))
                patch_rel = f"patches/{len(patches)}.rud"
                (data / "patches").mkdir(exist_ok=True)
                info = make_patch(orig, cur, data / patch_rel)
                if info is not None:
                    patches.append({"path": rel, "patch": patch_rel, "orig_sha1": info["orig_sha1"],
                                    "orig_size": info["orig_size"], "new_sha1": info["new_sha1"],
                                    "new_size": info["new_size"], "required": not plan.optional_patches})
                elif cur.stat().st_size <= FULL_FILE_LIMIT:
                    file_rel = f"full/{len(full)}.bin"
                    (data / "full").mkdir(exist_ok=True)
                    shutil.copyfile(cur, data / file_rel)
                    full.append({"path": rel, "file": file_rel, "orig_sha1": sha1_file(orig),
                                 "orig_size": orig.stat().st_size, "required": not plan.optional_patches})
                else:
                    notes.append(f"Файл {rel} слишком сильно изменён и велик для архива — у друга он "
                                 "останется оригинальным.")
        for rel, content in plan.replace.items():
            if plan.method == "files" and rel not in backup.modified:
                put(rel, content=content)
            elif plan.method == "files":
                file_rel = f"full/{len(full)}.bin"
                (data / "full").mkdir(exist_ok=True)
                (data / file_rel).write_bytes(content)
                # конфиг «для друзей» пишется поверх любого — сверять оригинал не нужно
                full.append({"path": rel, "file": file_rel, "orig_sha1": "", "orig_size": 0, "required": False})
        for rel, src in plan.extra.items():
            if not any(f["path"] == rel for f in files):
                put(rel, src)
                if plan.method == "files" and not (game_dir / rel).exists():
                    created.append(rel)

        st = project.stats()
        exe = _game_exe_name(game_dir, project)
        manifest = {
            "format": FORMAT,
            "app": branding.APP_NAME, "app_version": russificator.__version__,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "game": {"title": title, "folder": game_dir.name, "exe": exe, "engine": plugin.engine_id,
                     "engine_title": det.engine_name or plugin.title, "details": det.details,
                     "version": project.meta.get("rpgmaker_version") or
                     (project.meta.get("unity") or {}).get("version") or ""},
            "method": plan.method,
            "translator": translator_title(str(project.meta.get("translator", ""))),
            "stats": {"total": st["total"], "translated": st["translated"] + st["approved"]},
            "credit": bool(credit),
            "created_paths": created,
            "files": files, "patches": patches, "full": full,
            "notes": plan.notes + notes,
        }
        (data / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

        # --- установщик, «Прочти.txt», лицензии
        installer = installer_exe()
        if installer is not None:
            shutil.copyfile(installer, top / INSTALLER_NAME)
        else:
            notes.append("Установщик (Установить.exe) есть только в собранной программе — этот архив "
                         "можно установить через «Русификатор игр» (Мои игры → Установить из архива).")
        (top / README_NAME).write_bytes(("﻿" + readme_text(manifest, installer is not None))
                                        .replace("\n", "\r\n").encode("utf-8"))
        lic = top / LICENSES_DIR
        lic.mkdir()
        for src in licenses(plugin.engine_id):
            shutil.copyfile(src, lic / src.name)
        ofl = license_file()
        if ofl is not None:
            shutil.copyfile(ofl, lic / "PT-Sans-OFL-1.1.txt")

        # --- архив
        status("Упаковка архива…", None)
        target = _unique(out_dir / f"{folder_name}.zip")
        tmp_zip = target.with_suffix(".zip.part")
        all_files = sorted(p for p in stage_root.rglob("*") if p.is_file())
        total = sum(p.stat().st_size for p in all_files) or 1
        done_bytes = 0
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in all_files:
                if cancel is not None and cancel.is_set():
                    raise PackageError("Отменено.")
                arc = p.relative_to(stage_root).as_posix()
                z.write(p, arc, compress_type=zipfile.ZIP_STORED if p.suffix.lower() in (".exe", ".zip", ".png", ".jpg")
                        else zipfile.ZIP_DEFLATED)
                done_bytes += p.stat().st_size
                status("Упаковка архива…", done_bytes / total)
        os.replace(tmp_zip, target)
        return {"path": str(target), "size": target.stat().st_size, "notes": notes,
                "name": target.name, "installer": installer is not None}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    for i in range(2, 100):
        p = path.with_name(f"{stem} ({i}){path.suffix}")
        if not p.exists():
            return p
    return path.with_name(f"{stem} ({int(time.time())}){path.suffix}")


def _game_exe_name(game_dir: Path, project: TranslationProject) -> str:
    exe = (project.meta.get("unity") or {}).get("exe") or ""
    if exe:
        return Path(exe).name
    from ..library import main_exe
    found = main_exe(game_dir)
    return found.name if found else ""


def installer_exe() -> Optional[Path]:
    """Установщик, собранный вместе с программой (resources/installer/RussificatorSetup.exe)."""
    p = Path(__file__).resolve().parents[1] / "resources" / "installer" / "RussificatorSetup.exe"
    return p if p.is_file() else None


def licenses(engine: str) -> List[Path]:
    base = Path(__file__).resolve().parents[1] / "resources" / "licenses"
    names = ["GameRussificator-MIT.txt"]
    if engine == "unity":
        names += ["BepInEx-LGPL-2.1.txt", "XUnity.AutoTranslator-MIT.txt"]
    return [base / n for n in names if (base / n).is_file()]


def readme_text(m: Dict[str, Any], has_installer: bool) -> str:
    g = m["game"]
    st = m.get("stats") or {}
    lines = [
        f"Русификатор для игры «{g['title']}»",
        "=" * 60,
        f"Создан программой «{branding.APP_NAME}» {m.get('app_version', '')} — {m.get('created', '')}.",
        f"Движок: {g.get('engine_title') or g.get('engine')}. "
        f"Переведено строк: {st.get('translated', 0)} из {st.get('total', 0)} ({m.get('translator', '')}).",
        "",
        "КАК УСТАНОВИТЬ",
    ]
    if has_installer:
        lines += [
            "1. Распакуйте архив в любую папку (например, на рабочий стол).",
            "2. Запустите «Установить.exe» и нажмите «Установить русификатор».",
            "   Установщик сам найдёт игру (Steam, GOG, Epic и др.) или попросит показать её папку —",
            "   ту, где лежит .exe игры.",
            "3. Играйте! Удалить русификатор — тем же установщиком, кнопка «Удалить».",
            "",
            "Если Windows показывает «Система Windows защитила ваш компьютер» — нажмите",
            "«Подробнее» → «Выполнить в любом случае». Установщик не подписан сертификатом,",
            "это обычное предупреждение для бесплатных программ.",
        ]
    else:
        lines += [
            "Установите через программу «Русификатор игр»: «Мои игры» → «Установить из архива»",
            "и выберите этот архив.",
        ]
    if m.get("method") == "files" and not m.get("patches") and not m.get("full"):
        lines += [
            "",
            "Вручную (без установщика): скопируйте содержимое папки russificator-data\\files",
            "в папку игры с заменой файлов. Чтобы удалить — удалите скопированное.",
        ]
    extra = [n for n in m.get("notes") or [] if n]
    if extra:
        lines += ["", "ОСОБЕННОСТИ"] + [f"• {n}" for n in extra]
    lines += [
        "",
        "ОБНОВЛЕНИЕ ИГРЫ",
        "После обновления игры или «Проверки целостности» в Steam перевод может слететь —",
        "просто запустите установщик ещё раз.",
        "",
        "О ПЕРЕВОДЕ",
        "Перевод сделан автоматически программой «Русификатор игр» — бесплатно, в пару кликов,",
        "для игр на Unity, Ren'Py и RPG Maker. Скачать программу:",
        f"    {branding.RELEASES_URL}",
        "",
        "Хотите качественный ручной перевод этой или другой игры — с редактурой, адаптацией",
        f"юмора и имён, перерисовкой надписей? Пишите автору в Telegram: @{branding.TELEGRAM}",
        f"    {branding.TELEGRAM_URL}",
    ]
    return "\n".join(lines) + "\n"


# ====================================================================== чтение

@dataclass
class Package:
    data: Path                       # папка russificator-data
    info: Dict[str, Any]
    _temp: Optional[Path] = field(default=None, repr=False)

    @property
    def game(self) -> Dict[str, Any]:
        return self.info.get("game") or {}

    @property
    def title(self) -> str:
        return str(self.game.get("title") or "игра")

    @property
    def engine(self) -> str:
        return str(self.game.get("engine") or "")

    def translations(self) -> List[Dict[str, str]]:
        try:
            return json.loads((self.data / TRANSLATIONS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def close(self) -> None:
        if self._temp is not None:
            shutil.rmtree(self._temp, ignore_errors=True)
            self._temp = None


def open_package(path: Path) -> Package:
    """Открыть русификатор: zip-архив, распакованную папку или саму папку russificator-data."""
    path = Path(path)
    temp = None
    if path.is_file() and zipfile.is_zipfile(path):
        temp = Path(tempfile.mkdtemp(prefix="pkg-", dir=str(paths.sub("tmp"))))
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if f"{DATA_DIR}/" in n.replace("\\", "/")]
            if not names:
                shutil.rmtree(temp, ignore_errors=True)
                raise PackageError("Это не архив русификатора: внутри нет папки russificator-data.")
            for n in names:
                _safe_extract(z, n, temp)
        found = next(iter(temp.rglob(f"{DATA_DIR}/{MANIFEST}")), None)
        data = found.parent if found else None
    else:
        data = None
        for cand in (path, path / DATA_DIR, path.parent / DATA_DIR):
            if (cand / MANIFEST).is_file():
                data = cand
                break
    if data is None:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise PackageError("Не найден package.json — архив русификатора повреждён или неполный.")
    try:
        info = json.loads((data / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise PackageError(f"package.json не читается: {exc}") from exc
    try:
        newer = int(info.get("format", 0)) > FORMAT
    except (TypeError, ValueError):
        newer = True
    if newer or not isinstance(info.get("game"), dict):
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        raise PackageError("Архив создан более новой версией программы — обновите установщик/программу."
                           if newer else "package.json повреждён — пересоздайте архив.")
    return Package(data=data, info=info, _temp=temp)


def _safe_extract(z: zipfile.ZipFile, name: str, dest: Path) -> None:
    target = (dest / name).resolve()
    if dest.resolve() not in target.parents and target != dest.resolve():
        raise PackageError("В архиве есть недопустимые пути.")
    if name.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with z.open(name) as src, open(target, "wb") as out:
        shutil.copyfileobj(src, out)


# ====================================================================== установка

@dataclass
class GameCheck:
    ok: bool
    exact: bool                      # похоже именно на ту игру (движок + exe)
    message: str
    engine_title: str = ""


def check_game(pkg: Package, game_dir: Path) -> GameCheck:
    """Подходит ли папка: движок тот же, и (желательно) тот же exe."""
    from .plugin_api import EngineNotDetectedError
    from .registry import detect_engine
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return GameCheck(False, False, "Папка не найдена.")
    try:
        plugin, det = detect_engine(game_dir)
    except EngineNotDetectedError:
        return GameCheck(False, False, "В этой папке не найдена игра. Выберите корень игры — папку, где лежит её .exe.")
    except Exception as exc:  # noqa: BLE001
        return GameCheck(False, False, f"Не удалось проверить папку: {exc}")
    title = det.engine_name or plugin.title
    if plugin.engine_id != pkg.engine:
        return GameCheck(False, False, f"Это игра на другом движке ({title}), а русификатор сделан для "
                                       f"«{pkg.title}» ({pkg.game.get('engine_title') or pkg.engine}).", title)
    exe = str(pkg.game.get("exe") or "")
    if exe and not (game_dir / exe).is_file():
        return GameCheck(True, False, f"Не найден {exe} — возможно, это другая игра на том же движке. "
                                      "Установить всё равно можно.", title)
    return GameCheck(True, True, f"Игра найдена: {title}.", title)


def find_game(pkg: Package, near: Optional[Path] = None) -> List[Path]:
    """Где может быть игра: рядом с архивом (если его распаковали в папку игры) и в библиотеках."""
    out: List[Path] = []
    exe = str(pkg.game.get("exe") or "")
    if near is not None:
        p = Path(near).resolve()
        for cand in [p, *list(p.parents)[:4]]:
            if exe and (cand / exe).is_file():
                out.append(cand)
                break
    try:
        from ..library import find_game as lib_find
        for p in lib_find(folder_name=str(pkg.game.get("folder") or ""), exe_name=exe, title=pkg.title):
            if p not in out:
                out.append(p)
    except Exception as exc:  # noqa: BLE001
        log.warning("поиск игры не удался: %s", exc)
    return out


def installed_info(game_dir: Path) -> Optional[Dict[str, Any]]:
    """Сведения о русификаторе, установленном из архива (или None)."""
    b = GameBackup(Path(game_dir))
    if not b.exists:
        return None
    return dict(b.info.get("package") or {}) or None


def _writable(d: Path) -> bool:
    probe = d / f".russificator_write_test_{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def install_package(pkg: Package, game_dir: Path, status: StatusFn = _noop) -> Dict[str, Any]:
    """Установить русификатор в игру. Бросает PackageError; при сбое игра возвращается к оригиналу."""
    from .restore import restore_backups
    game_dir = Path(game_dir).resolve()
    if not game_dir.is_dir():
        raise PackageError("Папка игры не найдена.")
    if not _writable(game_dir):
        raise PackageError("Нет прав на запись в папку игры. Запустите установщик от имени администратора.",
                           "access")
    if GameBackup(game_dir).exists:
        status("Удаляю прошлую русификацию…", None)
        restore_backups(game_dir)
    notes: List[str] = []
    try:
        if pkg.info.get("method") == "reinject":
            translated = _install_reinject(pkg, game_dir, status, notes)
        else:
            translated = _install_files(pkg, game_dir, status, notes)
    except PackageError:
        restore_backups(game_dir)
        raise
    except PermissionError as exc:
        restore_backups(game_dir)
        raise PackageError(f"Нет доступа к файлу игры: {exc.filename or exc}. Закройте игру и попробуйте "
                           "снова (или запустите установщик от имени администратора).", "access") from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("установка русификатора упала")
        restore_backups(game_dir)
        raise PackageError(f"Установка не удалась: {exc}. Игра возвращена к оригиналу.") from exc
    backup = GameBackup(game_dir)
    backup.snapshot({"package": {"title": pkg.title, "created": pkg.info.get("created", ""),
                                 "app_version": pkg.info.get("app_version", ""),
                                 "translator": pkg.info.get("translator", "")},
                     "installed": time.strftime("%Y-%m-%d %H:%M"), "method": pkg.info.get("method", "")})
    status("Готово", 1.0)
    return {"ok": True, "notes": notes, "translated": translated,
            "total": (pkg.info.get("stats") or {}).get("total", 0)}


def _install_files(pkg: Package, game_dir: Path, status: StatusFn, notes: List[str]) -> int:
    from .delta import PatchError, apply_patch, sha1_file
    backup = GameBackup(game_dir)
    files = pkg.info.get("files") or []
    src_root = pkg.data / "files"
    # папки, которые русификатор создаёт целиком, — откат удалит их со всем содержимым
    for rel in sorted(pkg.info.get("created_paths") or [], key=len):
        dest = game_dir / rel
        if not dest.exists() and (src_root / rel).is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            backup.track_new(dest)
    for n, f in enumerate(files):
        rel = f["path"]
        _check_rel(rel)
        status(f"Копирование: {Path(rel).name}", 0.6 * n / max(1, len(files)))
        backup.copy_new(src_root / rel, game_dir / rel)
    patches = pkg.info.get("patches") or []
    skipped = 0
    for n, p in enumerate(patches):
        rel = p["path"]
        _check_rel(rel)
        target = game_dir / rel
        status(f"Патч: {Path(rel).name}", 0.6 + 0.35 * n / max(1, len(patches)))
        reason = ""
        if not target.is_file():
            reason = "файла нет"
        elif target.stat().st_size != p["orig_size"] or sha1_file(target) != p["orig_sha1"]:
            reason = "другая версия игры"
        if reason:
            if p.get("required"):
                raise PackageError(f"Файл {rel}: {reason}. Этот русификатор сделан для другой версии игры.")
            skipped += 1
            continue
        backup.save_original(target)
        try:
            apply_patch(target, pkg.data / p["patch"], target, check_orig=False)
        except PatchError as exc:
            if p.get("required"):
                raise PackageError(f"Файл {rel}: {exc}") from exc
            skipped += 1
    for f in pkg.info.get("full") or []:
        rel = f["path"]
        _check_rel(rel)
        target = game_dir / rel
        if f.get("orig_sha1") and target.is_file() and sha1_file(target) != f["orig_sha1"]:
            if f.get("required"):
                raise PackageError(f"Файл {rel} другой версии — этот русификатор сделан для другой версии игры.")
            skipped += 1
            continue
        backup.copy_new(pkg.data / f["file"], target)
    if skipped:
        notes.append(f"Ваша версия игры отличается от той, для которой сделан русификатор: {skipped} файл(ов) "
                     "оставлены как есть. Текст переведён, но часть шрифтов может выглядеть иначе.")
    return int((pkg.info.get("stats") or {}).get("translated", 0))


def _check_rel(rel: str) -> None:
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or (parts and parts[0].lower() == BACKUP_DIR):
        raise PackageError(f"Недопустимый путь в архиве: {rel}")


def _install_reinject(pkg: Package, game_dir: Path, status: StatusFn, notes: List[str]) -> int:
    """Извлечь текст из копии игры и внедрить перевод из архива (тем же кодом, что в программе)."""
    from ..fonts.library import ensure_font
    from .registry import detect_engine
    plugin, _det = detect_engine(game_dir)
    if plugin.engine_id != pkg.engine:
        raise PackageError("Движок игры не совпадает с русификатором.")
    plugin.status = lambda m, f=None: status(m, None)
    work = Path(tempfile.mkdtemp(prefix="inst-", dir=str(paths.sub("tmp"))))
    try:
        project = TranslationProject(game_dir, plugin.engine_id, work)
        project.backup = GameBackup(game_dir)
        if pkg.info.get("credit"):
            project.meta["credit"] = branding.CREDIT
        status("Чтение текста игры…", 0.1)
        prepared = plugin.pre_extract(game_dir, project)
        effective = Path(prepared) if prepared else game_dir
        plugin.extract(effective, project)
        status("Подстановка перевода…", 0.4)
        n = apply_translations(project, pkg.translations())
        if not n:
            raise PackageError("Ни одна строка перевода не подошла к этой игре — похоже, это другая игра.")
        status("Внедрение перевода…", 0.55)
        plugin.inject(effective, project)
        status("Шрифт с кириллицей…", 0.85)
        plugin.install_font(effective, project, ensure_font())
        for f in pkg.info.get("files") or []:          # дополнительные файлы движка, если есть
            _check_rel(f["path"])
            project.backup.copy_new(pkg.data / "files" / f["path"], game_dir / f["path"])
        total = len([e for e in project.entries if e.status != EntryStatus.SKIPPED])
        if total and n < total:
            notes.append(f"Переведено {n} из {total} строк: остальные строки в вашей версии игры новые "
                         "или изменились — они останутся на английском.")
        notes += [w for w in project.warnings if "шрифт" in w.lower()][:2]
        return n
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply_translations(project: TranslationProject, rows: List[Dict[str, str]]) -> int:
    """Подставить переводы из архива: сначала по адресу и тексту строки, потом просто по тексту."""
    by_key: Dict[Tuple[str, str], Dict[str, str]] = {}
    by_src: Dict[str, Dict[str, str]] = {}
    for r in rows:
        by_key[(r.get("k", ""), r.get("s", ""))] = r
        by_src.setdefault(r.get("s", ""), r)
    n = 0
    for e in project.entries:
        r = by_key.get((e.target_key, e.source)) or by_src.get(e.source)
        if r is None or not r.get("t"):
            continue
        e.translation = r["t"]
        e.status = EntryStatus.APPROVED if r.get("st") == EntryStatus.APPROVED.value else EntryStatus.TRANSLATED
        n += 1
    return n


def uninstall(game_dir: Path) -> Tuple[int, List[str]]:
    from .restore import restore_backups
    return restore_backups(Path(game_dir))
