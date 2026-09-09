"""FastAPI: POST /scrape, POST /price, POST /targets/check, GET /health, Mini-Dashboard /."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .cache import TTLCache
from .config import load_config
from .parser import auto_price, extract_with_selectors
from .scraper import Scraper

CFG = load_config(Path(__file__).resolve().parent.parent / "config.yaml"
                  if (Path(__file__).resolve().parent.parent / "config.yaml").exists()
                  else "config.yaml")
CACHE = TTLCache(CFG.get("cache", {}).get("db_path", "data/cache.db"),
                 CFG.get("cache", {}).get("ttl_hours", 6))
SCRAPER = Scraper(CFG)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await SCRAPER.start()
    yield
    await SCRAPER.stop()


app = FastAPI(title="StealthScraper-LXC", lifespan=lifespan)


def _auth(x_token: str | None) -> None:
    want = (CFG.get("server", {}) or {}).get("api_token") or ""
    if want and x_token != want:
        raise HTTPException(status_code=401, detail="Ungültiger Token")


class ScrapeReq(BaseModel):
    url: str
    selectors: dict[str, str] = Field(default_factory=dict)
    wait_for: str = ""
    fresh: bool = False  # True = Cache umgehen


class PriceReq(BaseModel):
    url: str
    wait_for: str = ""
    fresh: bool = False


@app.get("/health")
async def health():
    return {"ok": True, "time": int(time.time())}


@app.get("/", response_class=HTMLResponse)
async def index():
    targets = [t.get("name") for t in CFG.get("targets", []) if t.get("enabled")]
    return f"""<html><body style="font-family:sans-serif;max-width:720px;margin:2em auto">
<h1>StealthScraper-LXC</h1>
<p>API läuft. Aktive Targets: {", ".join(targets) or "(keine)"}</p>
<ul>
<li><code>POST /scrape</code> {{"url": "...", "selectors": {{"preis": ".price"}}}}</li>
<li><code>POST /price</code> {{"url": "..."}} (Auto: JSON-LD/Meta/Regex)</li>
<li><code>POST /targets/check</code> alle aktiven Targets prüfen</li>
<li><code>GET /health</code></li>
</ul>
<p>Auth: Header <code>X-Token</code> (falls api_token gesetzt).</p>
</body></html>"""


async def _fetch_cached(url: str, wait_for: str, extra: str, fresh: bool) -> dict:
    if not fresh:
        hit = CACHE.get(url, extra)
        if hit:
            hit["cached"] = True
            return hit
    page = await SCRAPER.fetch(url, wait_for=wait_for)
    return {**page, "cached": False}


@app.post("/scrape")
async def scrape(req: ScrapeReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    try:
        page = await _fetch_cached(req.url, req.wait_for, str(sorted(req.selectors.items())), req.fresh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scrape fehlgeschlagen: {e}")
    data = {"url": req.url, "titel": page["title"], "status": page["status"], "cached": page["cached"]}
    if req.selectors:
        data["felder"] = extract_with_selectors(page["html"], req.selectors)
    else:
        data["auto"] = auto_price(page["html"])
    if not req.fresh:
        CACHE.set(req.url, str(sorted(req.selectors.items())), {k: v for k, v in data.items() if k != "cached"})
    return JSONResponse(data)


@app.post("/price")
async def price(req: PriceReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    try:
        page = await _fetch_cached(req.url, req.wait_for, "price", req.fresh)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scrape fehlgeschlagen: {e}")
    result = {"url": req.url, "titel": page["title"], "status": page["status"],
              "cached": page["cached"], **auto_price(page["html"])}
    if not req.fresh:
        CACHE.set(req.url, "price", {k: v for k, v in result.items() if k != "cached"})
    return JSONResponse(result)


@app.post("/targets/check")
async def targets_check(x_token: str | None = Header(default=None)):
    _auth(x_token)
    out = []
    for t in CFG.get("targets", []) or []:
        if not t.get("enabled"):
            continue
        try:
            page = await _fetch_cached(t["url"], t.get("wait_for", ""), "price", fresh=True)
            info = auto_price(page["html"])
            if t.get("selectors"):
                info["felder"] = extract_with_selectors(page["html"], t["selectors"])
            out.append({"name": t.get("name"), "ok": True, "status": page["status"], **info})
        except Exception as e:
            out.append({"name": t.get("name"), "ok": False, "fehler": str(e)})
    # Ergebnisse als JSON ablegen (für Timer-Läufe)
    results_dir = Path((CFG.get("output", {}) or {}).get("results_dir", "data/results"))
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"check-{int(time.time())}.json").write_text(
        __import__("json").dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse(out)


@app.delete("/cache")
async def cache_clear(x_token: str | None = Header(default=None)):
    _auth(x_token)
    CACHE.clear()
    return {"ok": True}
