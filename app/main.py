"""CLI für manuelle Tests + Cron: python -m app.main --url ... / --target all."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cache import TTLCache  # noqa: E402
from app.config import load_config  # noqa: E402
from app.parser import auto_price, extract_with_selectors  # noqa: E402
from app.scraper import Scraper  # noqa: E402


async def _run_url(cfg: dict, url: str, selectors: dict, wait_for: str, price_only: bool) -> dict:
    scraper = Scraper(cfg)
    try:
        page = await scraper.fetch(url, wait_for=wait_for)
    finally:
        await scraper.stop()
    if price_only or not selectors:
        return {"url": url, "titel": page["title"], "status": page["status"], **auto_price(page["html"])}
    return {"url": url, "titel": page["title"], "status": page["status"],
            "felder": extract_with_selectors(page["html"], selectors)}


async def _run_targets(cfg: dict, cache: TTLCache) -> list[dict]:
    scraper = Scraper(cfg)
    out: list[dict] = []
    try:
        for t in cfg.get("targets", []) or []:
            if not t.get("enabled"):
                continue
            try:
                page = await scraper.fetch(t["url"], wait_for=t.get("wait_for", ""))
                info = auto_price(page["html"])
                if t.get("selectors"):
                    info["felder"] = extract_with_selectors(page["html"], t["selectors"])
                out.append({"name": t.get("name"), "ok": True, **info})
            except Exception as e:
                out.append({"name": t.get("name"), "ok": False, "fehler": str(e)})
    finally:
        await scraper.stop()
    results_dir = Path((cfg.get("output", {}) or {}).get("results_dir", "data/results"))
    results_dir.mkdir(parents=True, exist_ok=True)
    import time
    (results_dir / f"check-{int(time.time())}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="StealthScraper-LXC CLI")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--url", help="Einzelne URL scrapen")
    ap.add_argument("--price", action="store_true", help="Nur Preis/Verfügbarkeit (Auto-Erkennung)")
    ap.add_argument("--selector", action="append", default=[], help="feld=css (wiederholbar)")
    ap.add_argument("--wait-for", default="")
    ap.add_argument("--target", help="'all' = alle aktiven Targets prüfen")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cache = TTLCache(cfg.get("cache", {}).get("db_path", "data/cache.db"),
                     cfg.get("cache", {}).get("ttl_hours", 6))

    if args.target == "all":
        print(json.dumps(asyncio.run(_run_targets(cfg, cache)), ensure_ascii=False, indent=2))
    elif args.url:
        selectors = dict(s.split("=", 1) for s in args.selector if "=" in s)
        print(json.dumps(asyncio.run(_run_url(cfg, args.url, selectors, args.wait_for, args.price)),
                         ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
