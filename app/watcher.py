"""Schnäppchen-Watcher: Median-Vergleich + History-DB + Webhook.

Deal = Inserat deutlich UNTER dem Median gleichartiger Treffer
(unterbewertet) oder mit deutlicher PREISSENKUNG seit der letzten Prüfung.
Deterministisch, keine KI nötig.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import statistics
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .parser import parse_preis
from .providers import preset_for_url, select_first

MIN_INTERVAL_MINUTES = 15  # Höflichkeits-Limit gegen Bot-Schutz (kleinanzeigen.de bannt schnell)
SEVEN_DAYS = 7 * 86400


def listing_id(url: str) -> str:
    m = re.search(r"(\d{6,})", url or "")
    if m:
        return m.group(1)
    return hashlib.sha1((url or "").encode()).hexdigest()[:12]


def _texts(el) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def extract_listings(html: str, base_url: str, preset: dict, custom: dict | None = None) -> list[dict]:
    """Trefferseite -> [{ext_id, titel, preis, preis_text, url, ort}]."""
    soup = BeautifulSoup(html, "lxml")
    custom = custom or {}
    items: list = []
    for sel in (custom.get("item") or preset.get("item", []) or []):
        if isinstance(sel, str) and "," in sel:
            sels = [s.strip() for s in sel.split(",")]
        else:
            sels = [sel]
        for s in sels:
            try:
                found = soup.select(s)
            except Exception:
                continue
            if found:
                items = found
                break
        if items:
            break
    out, seen = [], set()
    for it in items[:200]:
        link_el = None
        for sel in (custom.get("link") or preset.get("link", []) or []):
            try:
                link_el = it.select_one(sel)
            except Exception:
                continue
            if link_el and link_el.get("href"):
                break
        href = (link_el.get("href", "") if link_el is not None else "") or ""
        url = urljoin(base_url, href)
        if not url or url in seen:
            continue
        seen.add(url)
        titel = _texts(select_first(it, custom.get("titel") or preset.get("titel", [])))
        if not titel and link_el is not None:
            titel = _texts(link_el)
        preis_text = _texts(select_first(it, custom.get("preis") or preset.get("preis", [])))
        ort = _texts(select_first(it, custom.get("ort") or preset.get("ort", [])))
        out.append({
            "ext_id": listing_id(url),
            "titel": titel[:200],
            "preis": parse_preis(preis_text),
            "preis_text": preis_text[:60],
            "url": url,
            "ort": ort[:120],
        })
    return out


def median_preis(listings: list[dict]) -> float | None:
    werte = [a["preis"] for a in listings if a.get("preis") not in (None, 0.0)]
    if len(werte) < 2:
        return None
    return float(statistics.median(werte))


def paginate_urls(search_url: str, max_seiten: int) -> list[str]:
    """Seite 1 + Blättern (kleinanzeigen: /seite:N/, sonst ?page=N)."""
    urls = [search_url]
    if max_seiten < 2:
        return urls
    if "kleinanzeigen.de" in search_url and "/seite:" not in search_url:
        parts = search_url.rstrip("/").rsplit("/", 1)
        if len(parts) == 2 and "." not in parts[1]:
            base = parts[0]
            for n in range(2, max_seiten + 1):
                urls.append(f"{base}/seite:{n}/{parts[1]}")
            return urls
    sep = "&" if "?" in search_url else "?"
    for n in range(2, max_seiten + 1):
        urls.append(f"{search_url}{sep}page={n}")
    return urls


class WatchDB:
    def __init__(self, db_path: str = "data/deals.db"):
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS listings
            (provider TEXT, ext_id TEXT, url TEXT, titel TEXT, preis REAL,
             first_seen INTEGER, last_seen INTEGER, PRIMARY KEY (provider, ext_id))""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS deals
            (id INTEGER PRIMARY KEY AUTOINCREMENT, watch TEXT, listing_url TEXT,
             titel TEXT, preis REAL, median REAL, rabatt REAL, grund TEXT, zeit INTEGER)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS watch_listings
            (watch TEXT, ext_id TEXT, titel TEXT, preis REAL, preis_text TEXT,
             url TEXT, ort TEXT, zeit INTEGER,
             PRIMARY KEY (watch, ext_id))""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS watch_state
            (name TEXT PRIMARY KEY, last_run INTEGER, last_status TEXT, last_error TEXT)""")
        # Migration für ältere DBs (Jobs-Tabelle: letzter Lauf im Detail)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(watch_state)").fetchall()}
        for col, typ in (("last_angebote", "INTEGER DEFAULT 0"),
                         ("last_deals", "INTEGER DEFAULT 0"),
                         ("last_median", "REAL")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE watch_state ADD COLUMN {col} {typ}")
        self._db.commit()

    # -- history --
    def known(self, provider: str, ext_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT url, titel, preis FROM listings WHERE provider=? AND ext_id=?",
            (provider, ext_id)).fetchone()
        return {"url": row[0], "titel": row[1], "preis": row[2]} if row else None

    def upsert(self, provider: str, ad: dict, now: int) -> None:
        self._db.execute(
            """INSERT INTO listings (provider, ext_id, url, titel, preis, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (provider, ext_id) DO UPDATE SET
               url=excluded.url, titel=excluded.titel, preis=excluded.preis, last_seen=excluded.last_seen""",
            (provider, ad["ext_id"], ad["url"], ad["titel"], ad["preis"], now, now))

    def commit(self) -> None:
        self._db.commit()

    def recent_deal(self, watch: str, url: str, grund: str, since_days: int = 7) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM deals WHERE watch=? AND listing_url=? AND grund=? AND zeit>?",
            (watch, url, grund, time.time() - since_days * 86400)).fetchone()
        return bool(row)

    def add_deal(self, watch: str, ad: dict, median: float | None, rabatt: float, grund: str) -> None:
        self._db.execute(
            "INSERT INTO deals (watch, listing_url, titel, preis, median, rabatt, grund, zeit)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (watch, ad["url"], ad["titel"], ad["preis"], median, round(rabatt, 1),
             grund, int(time.time())))
        self._db.commit()

    def deals(self, watch: str | None = None, limit: int = 50) -> list[dict]:
        q = "SELECT watch, listing_url, titel, preis, median, rabatt, grund, zeit FROM deals"
        args: list = []
        if watch:
            q += " WHERE watch=?"
            args.append(watch)
        q += " ORDER BY zeit DESC LIMIT ?"
        args.append(limit)
        return [dict(zip(("watch", "url", "titel", "preis", "median", "rabatt", "grund", "zeit"),
                         r)) for r in self._db.execute(q, args).fetchall()]

    # -- snapshot: zuletzt gefundene angebote je watch (für "angebote ansehen") --
    def save_snapshot(self, watch: str, listings: list[dict], limit: int = 150) -> None:
        now = int(time.time())
        self._db.execute("DELETE FROM watch_listings WHERE watch=?", (watch,))
        for ad in listings[:limit]:
            self._db.execute(
                "INSERT OR REPLACE INTO watch_listings"
                " (watch, ext_id, titel, preis, preis_text, url, ort, zeit)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (watch, ad.get("ext_id", ""), (ad.get("titel") or "")[:200], ad.get("preis"),
                 (ad.get("preis_text") or "")[:60], ad.get("url", ""), (ad.get("ort") or "")[:120], now))
        self._db.commit()

    def get_snapshot(self, watch: str, limit: int = 100) -> list[dict]:
        rows = self._db.execute(
            "SELECT titel, preis, preis_text, url, ort, zeit FROM watch_listings"
            " WHERE watch=? ORDER BY preis IS NULL, preis LIMIT ?",
            (watch, max(1, min(limit, 200)))).fetchall()
        return [dict(zip(("titel", "preis", "preis_text", "url", "ort", "zeit"), r)) for r in rows]

    # -- state --
    def last_run(self, name: str) -> int:
        row = self._db.execute("SELECT last_run FROM watch_state WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else 0

    def set_state(self, name: str, status: str, error: str = "",
                  summary: dict | None = None) -> None:
        summary = summary or {}
        self._db.execute(
            """INSERT INTO watch_state (name, last_run, last_status, last_error,
               last_angebote, last_deals, last_median) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (name) DO UPDATE SET last_run=excluded.last_run,
               last_status=excluded.last_status, last_error=excluded.last_error,
               last_angebote=excluded.last_angebote, last_deals=excluded.last_deals,
               last_median=excluded.last_median""",
            (name, int(time.time()), status, error[:500],
             int(summary.get("angebote", 0) or 0), int(summary.get("deals", 0) or 0),
             summary.get("median")))
        self._db.commit()

    def get_state(self, name: str) -> dict:
        row = self._db.execute(
            "SELECT last_run, last_status, last_error, last_angebote, last_deals, last_median"
            " FROM watch_state WHERE name=?", (name,)).fetchone()
        if not row:
            return {"last_run": 0, "last_status": "nie", "last_error": "",
                    "last_angebote": 0, "last_deals": 0, "last_median": None}
        return {"last_run": int(row[0] or 0), "last_status": row[1] or "",
                "last_error": row[2] or "", "last_angebote": int(row[3] or 0),
                "last_deals": int(row[4] or 0), "last_median": row[5]}


def send_webhook(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read(1024)


async def preview_search(scraper, search_url: str, provider_key: str = "",
                         max_preis: float | None = None) -> dict:
    """Trockenlauf für den Einrichtungs-Assistenten: 1. Seite holen, Treffer +
    Median zeigen, nichts speichern (kein DB-Schreibzugriff)."""
    from .providers import PROVIDERS
    if not provider_key or provider_key == "auto":
        provider_key, _ = preset_for_url(search_url)
    preset = PROVIDERS.get(provider_key, PROVIDERS["generisch"])
    page = await scraper.fetch(search_url)
    alle = extract_listings(page["html"], search_url, preset, {})
    angebote = [a for a in alle
                if max_preis is None or (a.get("preis") is not None and a["preis"] <= max_preis)]
    median = median_preis(angebote)
    beispiele = [{"titel": a["titel"], "preis": a["preis"], "url": a["url"]} for a in angebote[:5]]
    return {"provider_erkannt": provider_key, "angebote_gesamt": len(alle),
            "mit_preis": len([a for a in angebote if a.get("preis") is not None]),
            "median": round(median, 2) if median else None, "beispiele": beispiele}


async def run_watch(scraper, watch: dict, db: WatchDB) -> dict:
    """Eine Watch prüfen: Seiten holen -> Median -> Deals -> History + Webhook."""
    name = watch.get("name", "watch")
    provider_key = watch.get("provider") or preset_for_url(watch.get("search_url", ""))[0]
    from .providers import PROVIDERS
    preset = PROVIDERS.get(provider_key, PROVIDERS["generisch"])
    custom_raw = watch.get("selectors") or {}
    custom = {k: ([v] if isinstance(v, str) else v) for k, v in custom_raw.items()}

    max_seiten = max(1, min(int(watch.get("max_seiten", 2)), 5))
    max_preis = watch.get("max_preis")
    try:
        max_preis = float(max_preis) if max_preis not in (None, "") else None
    except (TypeError, ValueError):
        max_preis = None
    schwelle = float(watch.get("deal_schwelle_prozent", 25) or 25)
    min_angebote = int(watch.get("min_angebote", 5) or 5)

    alle: dict[str, dict] = {}
    for url in paginate_urls(watch["search_url"], max_seiten):
        try:
            page = await scraper.fetch(url)
        except Exception:
            continue  # Folgeseiten dürfen fehlen (404/Block) -> auswerten was da ist
        for ad in extract_listings(page["html"], url, preset, custom):
            alle.setdefault(ad["url"], ad)
    angebote = list(alle.values())
    if max_preis is not None:
        angebote = [a for a in angebote if a.get("preis") is not None and a["preis"] <= max_preis]

    median = median_preis(angebote)
    deals, now = [], int(time.time())
    if median and len([a for a in angebote if a.get("preis")]) >= min_angebote:
        grenze = median * (1 - schwelle / 100)
        for ad in angebote:
            p = ad.get("preis")
            if p is None or p == 0.0:
                continue
            alt = db.known(provider_key, ad["ext_id"])
            if p <= grenze and (alt is None or p < (alt.get("preis") or float("inf"))):
                if not db.recent_deal(name, ad["url"], "UNTER_MARKT"):
                    rabatt = (median - p) / median * 100
                    db.add_deal(name, ad, median, rabatt, "UNTER_MARKT")
                    deals.append({**ad, "median": round(median, 2),
                                  "rabatt_prozent": round(rabatt, 1), "grund": "UNTER_MARKT"})
            elif alt and alt.get("preis") and p < alt["preis"] * 0.85:
                if not db.recent_deal(name, ad["url"], "PREISSENKUNG"):
                    drop = (alt["preis"] - p) / alt["preis"] * 100
                    db.add_deal(name, ad, median, drop, "PREISSENKUNG")
                    deals.append({**ad, "median": round(median, 2),
                                  "rabatt_prozent": round(drop, 1), "grund": "PREISSENKUNG"})
    for ad in angebote:
        db.upsert(provider_key, ad, now)
    db.commit()
    db.save_snapshot(name, angebote)

    if deals and watch.get("notify_webhook"):
        try:
            send_webhook(watch["notify_webhook"], {"watch": name, "median": median, "deals": deals})
        except Exception:
            pass
    return {"watch": name, "provider": provider_key, "angebote_gesamt": len(alle),
            "mit_preis": len([a for a in angebote if a.get("preis") is not None]),
            "median": round(median, 2) if median else None,
            "schwelle_prozent": schwelle, "deals": deals}
