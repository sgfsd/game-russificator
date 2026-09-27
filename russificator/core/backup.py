"""Бэкап и откат: одна папка, один манифест, для всех движков одинаково.

Любой плагин перед изменением файла игры вызывает
:meth:`GameBackup.save_original`, а про каждый созданный файл сообщает
:meth:`GameBackup.track_new`. Всё хранится в ``<игра>/russificator_backup/``
с сохранением относительных путей, список — в ``manifest.json``.

Откат (:meth:`GameBackup.restore`) возвращает оригиналы, удаляет созданные
файлы и саму папку бэкапа — игра становится ровно такой, какой была.
Повторная русификация сначала делает откат, поэтому текст всегда
извлекается из оригинальных файлов, а запуск дважды даёт тот же результат.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BACKUP_DIR = "russificator_backup"
MANIFEST = "manifest.json"


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class GameBackup:
    def __init__(self, game_dir: Path):
        self.game_dir = Path(game_dir).resolve()
        self.root = self.game_dir / BACKUP_DIR
        self.modified: Dict[str, str] = {}   # относительный путь -> sha1 оригинала
        self.created: List[str] = []         # файлы, созданные русификатором
        #: состояние файлов сразу после русификации: путь -> [размер, mtime_ns]
        #: (по нему «Мои игры» видят, что обновление игры затёрло перевод)
        self.snapshot_state: Dict[str, List[int]] = {}
        self.info: Dict[str, object] = {}    # чем и когда русифицировано (для «Моих игр»)
        self._load()

    # ---------- манифест ----------

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    def _load(self) -> None:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.modified = dict(data.get("modified", {}))
            self.created = list(data.get("created", []))
            self.snapshot_state = dict(data.get("snapshot", {}))
            self.info = dict(data.get("info", {}))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"version": 2, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "modified": self.modified, "created": self.created,
                "snapshot": self.snapshot_state, "info": self.info}
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.game_dir).as_posix()

    @property
    def exists(self) -> bool:
        return bool(self.modified or self.created)

    # ---------- API для плагинов ----------

    def _created_by_us(self, rel: str) -> bool:
        """Файл сам или одна из его папок создан русификатором."""
        parts = rel.split("/")
        return any("/".join(parts[:i]) in self.created for i in range(1, len(parts) + 1))

    def save_original(self, path: Path) -> None:
        """Сохранить оригинал файла перед первой правкой (повторные вызовы — no-op)."""
        path = Path(path)
        rel = self._rel(path)
        if rel in self.modified or self._created_by_us(rel) or not path.is_file():
            return
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        self.modified[rel] = _sha1(path)
        self._save()

    def track_new(self, path: Path) -> None:
        """Запомнить файл/папку, созданные русификатором (удаляются при откате)."""
        rel = self._rel(Path(path))
        if rel in self.modified or self._created_by_us(rel):
            return
        self.created.append(rel)
        self._save()

    def write_text(self, path: Path, text: str) -> None:
        """Записать текстовый файл игры с автоматическим бэкапом/учётом."""
        path = Path(path)
        if path.exists():
            self.save_original(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.track_new(path)
        path.write_text(text, encoding="utf-8")

    def write_bytes(self, path: Path, data: bytes) -> None:
        path = Path(path)
        if path.exists():
            self.save_original(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.track_new(path)
        path.write_bytes(data)

    def copy_new(self, src: Path, dest: Path) -> None:
        dest = Path(dest)
        if dest.exists():
            self.save_original(dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.track_new(dest)
        shutil.copy2(src, dest)

    # ---------- состояние после русификации ----------

    def _files(self) -> List[str]:
        """Все файлы русификации: изменённые и созданные (папки — по файлам внутри)."""
        out = list(self.modified)
        for rel in self.created:
            p = self.game_dir / rel
            if p.is_dir():
                out += [f.relative_to(self.game_dir).as_posix() for f in p.rglob("*") if f.is_file()]
            else:
                out.append(rel)
        return out

    def snapshot(self, info: Optional[Dict[str, object]] = None) -> None:
        """Запомнить состояние файлов сразу после русификации (и чем русифицировано)."""
        state: Dict[str, List[int]] = {}
        for rel in self._files():
            if len(state) >= 20000:
                break
            try:
                st = (self.game_dir / rel).stat()
            except OSError:
                continue
            state[rel] = [st.st_size, st.st_mtime_ns]
        self.snapshot_state = state
        if info is not None:
            self.info = dict(info)
        self._save()

    def check(self) -> Tuple[bool, List[str]]:
        """Цела ли русификация: (цела, какие файлы пропали или изменились).

        Изменённые файлы игры сверяются с состоянием после русификации: если
        обновление игры или «проверка целостности» Steam вернули оригинал —
        перевод слетел. Созданные файлы достаточно найти на месте (их
        содержимое меняет сама игра — XUnity дописывает свой кэш переводов).
        """
        broken: List[str] = []
        for rel in self.modified:
            p = self.game_dir / rel
            snap = self.snapshot_state.get(rel)
            try:
                st = p.stat()
            except OSError:
                broken.append(rel)
                continue
            if snap and (st.st_size != snap[0] or st.st_mtime_ns != snap[1]):
                broken.append(rel)
        for rel in self.created:
            if not (self.game_dir / rel).exists():
                broken.append(rel)
        return not broken, broken

    # ---------- откат ----------

    def restore(self) -> Tuple[int, List[str]]:
        """Вернуть оригиналы и удалить созданное. Возвращает (файлов, заметки)."""
        notes: List[str] = []
        restored = 0
        for rel in list(self.modified):
            src = self.root / rel
            dest = self.game_dir / rel
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                restored += 1
            else:
                notes.append(f"Нет копии оригинала: {rel}")
        removed = 0
        for rel in sorted(self.created, key=len, reverse=True):
            p = self.game_dir / rel
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
            elif p.exists():
                p.unlink()
                removed += 1
            _prune_empty(p.parent, self.game_dir)
        shutil.rmtree(self.root, ignore_errors=True)
        self.modified, self.created = {}, []
        self.snapshot_state, self.info = {}, {}
        if restored:
            notes.insert(0, f"Восстановлено оригинальных файлов: {restored}.")
        if removed:
            notes.insert(1 if restored else 0, f"Удалено файлов русификатора: {removed}.")
        return restored + removed, notes


def _prune_empty(d: Path, stop: Path) -> None:
    d = Path(d)
    while d != stop and d.is_dir() and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent
