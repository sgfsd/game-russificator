"""Память переводов (SQLite): однажды переведённая строка больше не переводится.

Кэш общий для всех игр и запусков — интерфейсные строки («Save», «Options»)
повторяются из игры в игру, а повторный запуск русификации после правок или
прерывания стоит ноль запросов к API. Ключ включает ``cache_id``
переводчика: смена модели даёт новые переводы, а не старые из другой.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


def _key(source: str, speaker: Optional[str]) -> str:
    return hashlib.sha1(f"{speaker or ''}\x00{source}".encode("utf-8")).hexdigest()


class TranslationMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS tm (ns TEXT NOT NULL, k TEXT NOT NULL, src TEXT, dst TEXT NOT NULL,"
            " ts REAL, PRIMARY KEY (ns, k))")
        self._db.commit()

    def get_many(self, ns: str, items: Iterable[Tuple[str, Optional[str]]]) -> Dict[Tuple[str, Optional[str]], str]:
        items = list(items)
        out: Dict[Tuple[str, Optional[str]], str] = {}
        with self._lock:
            for i in range(0, len(items), 400):
                chunk = items[i:i + 400]
                keys = {_key(s, sp): (s, sp) for s, sp in chunk}
                marks = ",".join("?" * len(keys))
                rows = self._db.execute(f"SELECT k, dst FROM tm WHERE ns=? AND k IN ({marks})",
                                        [ns, *keys]).fetchall()
                for k, dst in rows:
                    out[keys[k]] = dst
        return out

    def put_many(self, ns: str, items: Dict[Tuple[str, Optional[str]], str]) -> None:
        if not items:
            return
        now = time.time()
        rows = [(ns, _key(s, sp), s, dst, now) for (s, sp), dst in items.items()]
        with self._lock:
            self._db.executemany("INSERT OR REPLACE INTO tm (ns, k, src, dst, ts) VALUES (?,?,?,?,?)", rows)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
