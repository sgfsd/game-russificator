"""Архив скачан из интернета: пометка Windows «из интернета» снимается с файлов программы
(иначе .NET не загружает pythonnet — «Failed to resolve Python.Runtime.Loader.Initialize»)."""

from __future__ import annotations

import sys

import pytest

from russificator import unblock

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="потоки NTFS есть только в Windows")


def _mark(path):
    with open(str(path) + unblock.STREAM, "w", encoding="ascii") as f:
        f.write("[ZoneTransfer]\r\nZoneId=3\r\n")


def _bundle(root):
    for rel in unblock.PROBES + ("numpy/core.pyd", "base_library.zip"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"MZ")
        _mark(p)
    return root


def test_unblock_removes_mark_of_the_web(tmp_path):
    base = _bundle(tmp_path / "_internal")
    assert all(unblock.marked(base / p) for p in unblock.PROBES)
    assert unblock.ensure_unblocked(base) is None
    assert not any(unblock.marked(p) for p in base.rglob("*") if p.is_file())
    assert (base / unblock.PROBES[0]).read_bytes() == b"MZ"          # сам файл не тронут


def test_unblock_is_noop_when_not_marked(tmp_path):
    base = tmp_path / "_internal"
    (base / "pythonnet" / "runtime").mkdir(parents=True)
    (base / unblock.PROBES[0]).write_bytes(b"MZ")
    assert unblock.ensure_unblocked(base) is None
    assert unblock.ensure_unblocked(tmp_path / "missing") is None


def test_unblock_reports_when_still_blocked(tmp_path, monkeypatch):
    base = _bundle(tmp_path / "_internal")
    monkeypatch.setattr(unblock, "unblock_tree", lambda root, recursive=True: (0, 5))   # нет прав на запись
    msg = unblock.ensure_unblocked(base)
    assert msg and "Разблокировать" in msg
