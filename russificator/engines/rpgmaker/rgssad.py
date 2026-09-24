"""Архивы RPG Maker XP/VX/VX Ace: Game.rgssad (v1), Game.rgss2a (v1), Game.rgss3a (v3).

Почти все такие игры хранят Data/*.rxdata|rvdata|rvdata2 (и графику) внутри
зашифрованного архива. Алгоритм общеизвестный — XOR с ключом, который
меняется по формуле key = key * 7 + 3:
  * v1: ключ 0xDEADCAFE; записи идут подряд: длина имени, имя (каждый байт
    XOR младший байт ключа), размер, данные (XOR 4-байтным ключом файла);
  * v3: ключ = seed * 9 + 3; в начале таблица записей (смещение, размер,
    ключ файла, длина имени — всё XOR ключ), имя XOR байты ключа по кругу.

Движок сначала ищет файлы в архиве, поэтому для русификации архив
распаковывается рядом с игрой и убирается в бэкап (откат его вернёт).
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MASK = 0xFFFFFFFF
ARCHIVE_NAMES = ("Game.rgss3a", "Game.rgss2a", "Game.rgssad")


def find_archive(game_root: Path) -> Optional[Path]:
    for name in ARCHIVE_NAMES:
        p = Path(game_root) / name
        if p.is_file():
            return p
    return None


def _decrypt_data(data: bytes, key: int) -> bytes:
    out = bytearray(len(data))
    n = len(data)
    i = 0
    while i < n:
        kb = key.to_bytes(4, "little")
        for j in range(min(4, n - i)):
            out[i + j] = data[i + j] ^ kb[j]
        key = (key * 7 + 3) & MASK
        i += 4
    return bytes(out)


def _xor_block(data: bytes, key: int) -> bytes:
    """Быстрый XOR потока 4-байтных ключей (numpy, если есть)."""
    try:
        import numpy as np
    except ImportError:
        return _decrypt_data(data, key)
    n = len(data)
    if n == 0:
        return b""
    words = (n + 3) // 4
    # k_i = 7^i * k_0 + 3 * (7^0 + ... + 7^(i-1))  (mod 2^32); uint64 переполняется по mod 2^64 — это ок
    powers = np.full(words, 7, dtype=np.uint64)
    powers[0] = 1
    a = np.cumprod(powers, dtype=np.uint64)
    s = np.cumsum(a, dtype=np.uint64) - a
    keys = (a * np.uint64(key) + np.uint64(3) * s) & np.uint64(MASK)
    kbytes = keys.astype("<u4").tobytes()[:n]
    return (np.frombuffer(data, np.uint8) ^ np.frombuffer(kbytes, np.uint8)).tobytes()


class RgssArchive:
    """Оглавление архива; содержимое файлов читается по требованию."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: Dict[str, Tuple[int, int, int]] = {}   # имя -> (смещение, размер, ключ файла)
        self.version = 0
        raw = self.path.read_bytes()[:16]
        if raw[:7] != b"RGSSAD\0":
            raise ValueError("не архив RGSSAD")
        self.version = raw[7]
        if self.version == 1:
            self._index_v1()
        elif self.version == 3:
            self._index_v3()
        else:
            raise ValueError(f"неизвестная версия архива RGSSAD {self.version}")

    def _index_v1(self) -> None:
        key = 0xDEADCAFE
        with self.path.open("rb") as fh:
            fh.seek(8)
            size_total = self.path.stat().st_size
            pos = 8
            while pos + 4 <= size_total:
                raw = fh.read(4)
                if len(raw) < 4:
                    break
                name_len = struct.unpack("<I", raw)[0] ^ key
                key = (key * 7 + 3) & MASK
                name_raw = bytearray(fh.read(name_len))
                for i in range(len(name_raw)):
                    name_raw[i] ^= key & 0xFF
                    key = (key * 7 + 3) & MASK
                size = struct.unpack("<I", fh.read(4))[0] ^ key
                key = (key * 7 + 3) & MASK
                offset = fh.tell()
                self.entries[_name(name_raw)] = (offset, size, key)
                fh.seek(size, 1)
                pos = offset + size

    def _index_v3(self) -> None:
        with self.path.open("rb") as fh:
            fh.seek(8)
            seed = struct.unpack("<I", fh.read(4))[0]
            key = (seed * 9 + 3) & MASK
            while True:
                raw = fh.read(16)
                if len(raw) < 16:
                    break
                offset, size, fkey, name_len = (v ^ key for v in struct.unpack("<IIII", raw))
                if offset == 0:
                    break
                name_raw = bytearray(fh.read(name_len))
                kb = key.to_bytes(4, "little")
                for i in range(len(name_raw)):
                    name_raw[i] ^= kb[i % 4]
                self.entries[_name(name_raw)] = (offset, size, fkey)

    def names(self) -> List[str]:
        return sorted(self.entries)

    def read(self, name: str) -> Optional[bytes]:
        e = self.entries.get(name) or self.entries.get(name.replace("/", "\\"))
        if e is None:
            return None
        offset, size, key = e
        with self.path.open("rb") as fh:
            fh.seek(offset)
            return _xor_block(fh.read(size), key)


def _name(raw: bytes) -> str:
    try:
        return raw.decode("utf-8").replace("\\", "/")
    except UnicodeDecodeError:
        return raw.decode("cp932", "replace").replace("\\", "/")


def build_v3(files: Dict[str, bytes], seed: int = 0x1234) -> bytes:
    """Собрать архив v3 (для тестов: проверка симметрии шифрования)."""
    key = (seed * 9 + 3) & MASK
    header = bytearray(b"RGSSAD\0\x03" + struct.pack("<I", seed))
    table_size = sum(16 + len(n.encode("utf-8")) for n in files) + 16
    offset = len(header) + table_size
    table = bytearray()
    blobs = bytearray()
    for i, (name, data) in enumerate(files.items()):
        nb = name.replace("/", "\\").encode("utf-8")
        fkey = (0x1000 + i * 17) & MASK
        table += struct.pack("<IIII", offset ^ key, len(data) ^ key, fkey ^ key, len(nb) ^ key)
        kb = key.to_bytes(4, "little")
        table += bytes(b ^ kb[j % 4] for j, b in enumerate(nb))
        blobs += _decrypt_data(data, fkey)
        offset += len(data)
    table += struct.pack("<IIII", 0 ^ key, 0, 0, 0)
    return bytes(header + table + blobs)
