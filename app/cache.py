"""SQLite-TTL-Cache: gleiche URL+Selektoren verbrauchen keinen neuen Browser-Lauf."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def _key(url: str, extra: str = "") -> str:
    return hashlib.sha256(f"{url}|{extra}".encode()).hexdigest()


class TTLCache:
    def __init__(self, db_path: str = "data/cache.db", ttl_hours: float = 6):
        self.db_path = db_path
        self.ttl = float(ttl_hours or 0) * 3600
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts REAL)"
        )
        self._db.commit()

    def get(self, url: str, extra: str = "") -> dict | None:
        if not self.ttl:
            return None
        row = self._db.execute(
            "SELECT v, ts FROM cache WHERE k=?", (_key(url, extra),)
        ).fetchone()
        if not row:
            return None
        v, ts = row
        if time.time() - ts > self.ttl:
            return None
        try:
            return json.loads(v)
        except Exception:
            return None

    def set(self, url: str, extra: str, value: dict) -> None:
        if not self.ttl:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO cache (k, v, ts) VALUES (?, ?, ?)",
            (_key(url, extra), json.dumps(value, ensure_ascii=False), time.time()),
        )
        self._db.commit()

    def clear(self) -> None:
        self._db.execute("DELETE FROM cache")
        self._db.commit()
