"""Interner Scheduler: prüft jede Minute fällige Watches (Intervall pro Watch).

Kein systemd-Neuanlegen nötig: neue/geänderte Watches aus config.yaml werden
automatisch übernommen. Mindestabstand: MIN_INTERVAL_MINUTES (Bot-Schutz).
"""
from __future__ import annotations

import asyncio
import time

from .config import load_config, resolve_config_path
from .watcher import MIN_INTERVAL_MINUTES, WatchDB, run_target, run_watch


def target_interval(target: dict) -> int:
    """0 = nur manuell (kein Cron), sonst Minuten (min. 15)."""
    iv = int(target.get("interval_minutes", 0) or 0)
    if iv <= 0:
        return 0
    return max(iv, MIN_INTERVAL_MINUTES) * 60


def due_targets(cfg: dict, db: WatchDB, now: int | None = None) -> list[dict]:
    now = now if now is not None else int(time.time())
    due = []
    for t in cfg.get("targets", []) or []:
        if not t.get("enabled", True) or not t.get("url"):
            continue
        iv = target_interval(t)
        if not iv:
            continue
        last = db.last_target_run(t.get("name", ""))
        if now - (last["zeit"] if last else 0) >= iv:
            due.append(t)
    return due


def interval_seconds(watch: dict) -> int:
    return max(int(watch.get("interval_minutes", 60) or 60), MIN_INTERVAL_MINUTES) * 60


def next_run_ts(watch: dict, last_run: int, now: int | None = None) -> int:
    """Nächster geplanter Lauf (Jobs-Tabelle: 'geplant')."""
    now = now if now is not None else int(time.time())
    nxt = last_run + interval_seconds(watch)
    return nxt if nxt > now else now  # überfällig -> jetzt fällig


def due_watches(cfg: dict, db: WatchDB, now: int | None = None) -> list[dict]:
    now = now if now is not None else int(time.time())
    due = []
    for w in cfg.get("watches", []) or []:
        if not w.get("enabled") or not w.get("search_url"):
            continue
        if now - db.last_run(w.get("name", "")) >= interval_seconds(w):
            due.append(w)
    return due


async def scheduler_loop(scraper_getter, db: WatchDB, stop_event: asyncio.Event) -> None:
    """Endlosschleife; scraper_getter() liefert den laufenden Scraper."""
    cfg_path = resolve_config_path()
    while not stop_event.is_set():
        try:
            cfg = load_config(cfg_path)
            if cfg.get("watcher", {}).get("enabled", True):
                for w in due_watches(cfg, db):
                    if stop_event.is_set():
                        break
                    name = w.get("name", "?")
                    try:
                        result = await run_watch(scraper_getter(), w, db)
                        db.set_state(name, "ok", "", {
                            "angebote": result.get("angebote_gesamt", 0),
                            "deals": len(result.get("deals", [])),
                            "median": result.get("median")})
                    except Exception as e:  # Watch darf Scheduler nie killen
                        db.set_state(name, "fehler", str(e))
                for t in due_targets(cfg, db):
                    if stop_event.is_set():
                        break
                    try:
                        await run_target(scraper_getter(), t, db)
                    except Exception:
                        pass  # Fehler steht im target_runs-Verlauf
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
