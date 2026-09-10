"""FastAPI: Scrape/Price/Targets + Schnäppchen-Watcher (Watches, Deals) + Dashboard /."""
from __future__ import annotations

import asyncio
import html
import json
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .cache import TTLCache
from .config import load_config, resolve_config_path, save_config
from .parser import auto_price, extract_with_selectors
from .providers import PROVIDERS, preset_for_url, quick_search_url
from .scheduler import due_watches, next_run_ts, scheduler_loop, target_interval
from .scraper import Scraper
from .watcher import MIN_INTERVAL_MINUTES, WatchDB, preview_search, run_target, run_watch

CONFIG_PATH = resolve_config_path()
CFG = load_config(CONFIG_PATH)
VERSION = "2026.09.10-ui5"  # im Dashboard-Footer sichtbar (prüfen ob neuer Code läuft)
CACHE = TTLCache(CFG.get("cache", {}).get("db_path", "data/cache.db"),
                 CFG.get("cache", {}).get("ttl_hours", 6))
SCRAPER = Scraper(CFG)
WDB = WatchDB((CFG.get("watcher", {}) or {}).get("db_path", "data/deals.db"))
STOP = asyncio.Event()

RESULTS_DIR = Path((CFG.get("output", {}) or {}).get("results_dir", "data/results"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await SCRAPER.start()
    STOP.clear()
    task = asyncio.create_task(scheduler_loop(lambda: SCRAPER, WDB, STOP))
    yield
    STOP.set()
    await task
    await SCRAPER.stop()


app = FastAPI(title="StealthScraper-LXC", lifespan=lifespan)


@app.exception_handler(Exception)
async def _json_500(request, exc: Exception):
    """Jeder Fehler als JSON (kein 'Internal Server Error'-HTML mehr im UI)."""
    if isinstance(exc, HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    traceback.print_exc()
    return JSONResponse({"detail": f"Interner Fehler ({type(exc).__name__}): {exc}"},
                        status_code=500)


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
    return {"ok": True, "time": int(time.time()), "version": VERSION}


@app.get("/targets")
async def targets_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    return JSONResponse([
        {"name": t.get("name"), "enabled": bool(t.get("enabled")),
         "url": t.get("url"), "wait_for": t.get("wait_for", ""),
         "selectors": t.get("selectors", {})}
        for t in cfg.get("targets", []) or []
    ])


@app.get("/results")
async def results_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    if not RESULTS_DIR.exists():
        return JSONResponse([])
    files = sorted(RESULTS_DIR.glob("check-*.json"), reverse=True)[:10]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"datei": f.name, "zeit": int(f.stat().st_mtime), "eintraege": data})
    return JSONResponse(out)


@app.get("/providers")
async def providers_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    return JSONResponse([
        {"key": k, "label": v.get("label"), "domains": v.get("domains", []),
         "hinweis": v.get("hinweis", ""), "suchlink": v.get("suchlink", "")}
        for k, v in PROVIDERS.items()
    ])


class WatchReq(BaseModel):
    name: str = ""
    enabled: bool = True
    provider: str = "auto"  # Key aus /providers oder "auto" (per Domain erkennen)
    search_url: str = ""
    query: str = ""       # Doku/Anzeige (Suchbegriff)
    plz: str = ""         # Doku/Anzeige (PLZ des Suchmittelpunkts)
    radius_km: int = 10   # Doku/Anzeige (Umkreis)
    max_preis: float | None = None
    deal_schwelle_prozent: float = 25
    min_angebote: int = 5
    max_seiten: int = 2
    interval_minutes: int = 60
    notify_webhook: str = ""
    selectors: dict[str, str] = Field(default_factory=dict)


def _fresh_cfg() -> dict:
    return load_config(CONFIG_PATH)


def _validate_watch(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name fehlt/ungültig (max. 60 Zeichen)")
    url = (data.get("search_url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="search_url muss mit http(s):// beginnen")
    provider = (data.get("provider") or "auto").strip()
    if provider in (None, "", "auto"):
        provider, _ = preset_for_url(url)
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unbekannter Provider '{provider}'")
    interval = int(data.get("interval_minutes", 60) or 60)
    if interval < MIN_INTERVAL_MINUTES:
        interval = MIN_INTERVAL_MINUTES  # Bot-Schutz: nicht öfter als alle 15 Min
    try:
        max_preis = float(data["max_preis"]) if data.get("max_preis") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_preis ungültig")
    return {
        "name": name, "enabled": bool(data.get("enabled", True)), "provider": provider,
        "search_url": url, "query": str(data.get("query", "") or "")[:120],
        "plz": str(data.get("plz", "") or "")[:10],
        "radius_km": max(0, min(int(data.get("radius_km", 10) or 0), 200)),
        "max_preis": max_preis,
        "deal_schwelle_prozent": max(1, min(float(data.get("deal_schwelle_prozent", 25) or 25), 90)),
        "min_angebote": max(2, min(int(data.get("min_angebote", 5) or 5), 50)),
        "max_seiten": max(1, min(int(data.get("max_seiten", 2) or 2), 5)),
        "interval_minutes": interval,
        "notify_webhook": str(data.get("notify_webhook", "") or "")[:500],
        "selectors": dict(data.get("selectors") or {}),
    }


@app.get("/watches")
async def watches_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    now = int(time.time())
    out = []
    for w in cfg.get("watches", []) or []:
        d = dict(w)
        st = WDB.get_state(w.get("name", ""))
        d["last_run"] = st["last_run"]
        d["last_status"] = st["last_status"]
        d["last_error"] = st["last_error"]
        d["summary"] = {"angebote": st["last_angebote"], "deals": st["last_deals"],
                        "mit_preis": st["last_mitpreis"], "median": st["last_median"]}
        d["next_run"] = next_run_ts(w, st["last_run"], now) if w.get("enabled") else None
        d["due"] = bool(w.get("enabled")) and d["next_run"] is not None and d["next_run"] <= now
        out.append(d)
    return JSONResponse(out)


@app.post("/watches")
async def watch_create(req: WatchReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    watches = cfg.get("watches", []) or []
    clean = _validate_watch(req.model_dump())
    if any(w.get("name") == clean["name"] for w in watches):
        raise HTTPException(status_code=409, detail="Watch mit diesem Namen existiert bereits")
    watches.append(clean)
    cfg["watches"] = watches
    save_config(cfg, CONFIG_PATH)
    return JSONResponse(clean)


@app.put("/watches/{name}")
async def watch_update(name: str, req: WatchReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    watches = cfg.get("watches", []) or []
    idx = next((i for i, w in enumerate(watches) if w.get("name") == name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Watch nicht gefunden")
    clean = _validate_watch({**req.model_dump(), "name": name})
    watches[idx] = clean
    cfg["watches"] = watches
    save_config(cfg, CONFIG_PATH)
    return JSONResponse(clean)


@app.delete("/watches/{name}")
async def watch_delete(name: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    watches = [w for w in (cfg.get("watches", []) or []) if w.get("name") != name]
    cfg["watches"] = watches
    save_config(cfg, CONFIG_PATH)
    return {"ok": True}


@app.post("/watches/{name}/run")
async def watch_run(name: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    watch = next((w for w in (cfg.get("watches", []) or []) if w.get("name") == name), None)
    if watch is None:
        raise HTTPException(status_code=404, detail="Watch nicht gefunden")
    try:
        result = await run_watch(SCRAPER, watch, WDB)
        WDB.set_state(name, "ok", "", {
            "angebote": result.get("angebote_gesamt", 0),
            "deals": len(result.get("deals", [])),
            "mit_preis": result.get("mit_preis", 0),
            "median": result.get("median")})
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        WDB.set_state(name, "fehler", str(e))
        raise HTTPException(status_code=502, detail=f"Watch fehlgeschlagen: {e}")
    return JSONResponse(result)


@app.get("/watches/{name}/angebote")
async def watch_angebote(name: str, limit: int = 100,
                         x_token: str | None = Header(default=None)):
    """Zuletzt gefundene Angebote dieser Watch (Snapshot vom letzten Lauf)."""
    _auth(x_token)
    return JSONResponse(WDB.get_snapshot(name, limit))


@app.get("/deals")
async def deals_list(watch: str | None = None, limit: int = 50,
                     x_token: str | None = Header(default=None)):
    _auth(x_token)
    return JSONResponse(WDB.deals(watch or None, max(1, min(limit, 200))))


class PreviewReq(BaseModel):
    search_url: str = ""
    provider: str = "auto"
    max_preis: float | None = None


@app.post("/watches/preview")
async def watch_preview(req: PreviewReq, x_token: str | None = Header(default=None)):
    """URL-Test im Einrichtungs-Assistenten: zeigt Treffer + Median, ohne zu speichern."""
    _auth(x_token)
    url = (req.search_url or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="search_url muss mit http(s):// beginnen")
    try:
        mp = float(req.max_preis) if req.max_preis not in (None, "") else None
    except (TypeError, ValueError):
        mp = None
    try:
        return JSONResponse(await preview_search(SCRAPER, url, req.provider or "auto", mp))
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Vorschau fehlgeschlagen: {e}")


@app.post("/watches/quicklink")
async def watch_quicklink(payload: dict, x_token: str | None = Header(default=None)):
    """Vorgefüllte Anbieter-Suche für Schritt 1 ('Suche öffnen')."""
    _auth(x_token)
    url = quick_search_url((payload or {}).get("provider", ""), (payload or {}).get("query", ""))
    return {"url": url}


# ---------- Einstellungen: alles über Web UI (wie BraveResearch /settings) ----------
def apply_settings(data: dict) -> dict:
    """Validiert + klemmt Settings-Payload. Port bleibt fix (Restart-Thema entfällt)."""
    data = data or {}
    s_in = data.get("server", {}) or {}
    c_in = data.get("scraper", {}) or {}
    cache_in = data.get("cache", {}) or {}
    w_in = data.get("watcher", {}) or {}

    def _int(v, default, lo, hi):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(v, hi))

    def _float(v, default, lo, hi):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(v, hi))
    return {
        "server": {"api_token": str(s_in.get("api_token", "") or "")[:200]},
        "scraper": {
            "timeout_seconds": _int(c_in.get("timeout_seconds", 30), 30, 5, 120),
            "extra_wait_ms": _int(c_in.get("extra_wait_ms", 1500), 1500, 0, 10000),
            "headless": bool(c_in.get("headless", True)),
            "locale": str(c_in.get("locale", "de-DE") or "de-DE")[:20],
            "timezone": str(c_in.get("timezone", "Europe/Berlin") or "Europe/Berlin")[:50],
            "viewport_width": _int(c_in.get("viewport_width", 1366), 1366, 800, 3840),
            "viewport_height": _int(c_in.get("viewport_height", 768), 768, 600, 2160),
            "user_agent": str(c_in.get("user_agent", "") or "")[:500],
            "block_images": bool(c_in.get("block_images", True)),
            "proxy": str(c_in.get("proxy", "") or "")[:300],
        },
        "cache": {"ttl_hours": _float(cache_in.get("ttl_hours", 6), 6, 0, 720)},
        "watcher": {"enabled": bool(w_in.get("enabled", True))},
    }


def _apply_live(cisclean: dict) -> None:
    """Übernimmt Settings sofort ins laufende System (ohne Neustart)."""
    CFG.setdefault("server", {})["api_token"] = cisclean["server"]["api_token"]
    CFG.setdefault("scraper", {}).update(cisclean["scraper"])
    CFG.setdefault("cache", {})["ttl_hours"] = cisclean["cache"]["ttl_hours"]
    CFG.setdefault("watcher", {})["enabled"] = cisclean["watcher"]["enabled"]
    s = CFG["scraper"]
    SCRAPER.timeout = int(s.get("timeout_seconds", 30)) * 1000
    SCRAPER.extra_wait = int(s.get("extra_wait_ms", 1500))
    CACHE.ttl = float(CFG["cache"].get("ttl_hours", 6)) * 3600
    # Hinweis: headless wirkt erst nach Service-Neustart (Browser läuft bereits).


@app.get("/settings/data")
async def settings_data(x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    s, sc, c, w = (cfg.get("server", {}) or {}, cfg.get("scraper", {}) or {},
                   cfg.get("cache", {}) or {}, cfg.get("watcher", {}) or {})
    return JSONResponse({
        "port": (CFG.get("server", {}) or {}).get("port", 8001),
        "server": {"api_token": s.get("api_token", "")},
        "scraper": {k: sc.get(k) for k in ("timeout_seconds", "extra_wait_ms", "headless",
                    "locale", "timezone", "viewport_width", "viewport_height",
                    "user_agent", "block_images", "proxy")},
        "cache": {"ttl_hours": c.get("ttl_hours", 6)},
        "watcher": {"enabled": w.get("enabled", True)},
    })


@app.post("/settings/save")
async def settings_save(payload: dict, x_token: str | None = Header(default=None)):
    _auth(x_token)
    payload = payload or {}
    clean = apply_settings(payload)
    cfg = _fresh_cfg()
    # Token nur anfassen, wenn das Formular ihn mitgeschickt hat (sonst behalten)
    if "server" in payload:
        cfg.setdefault("server", {})["api_token"] = clean["server"]["api_token"]
    else:
        clean["server"]["api_token"] = (cfg.get("server", {}) or {}).get("api_token", "")
    old_headless = bool((cfg.get("scraper", {}) or {}).get("headless", True))
    cfg.setdefault("scraper", {}).update(clean["scraper"])
    cfg.setdefault("cache", {})["ttl_hours"] = clean["cache"]["ttl_hours"]
    cfg.setdefault("watcher", {})["enabled"] = clean["watcher"]["enabled"]
    save_config(cfg, CONFIG_PATH)
    _apply_live(clean)
    return {"ok": True,
            "restart_needed": clean["scraper"]["headless"] != old_headless,
            "hinweis": ("Headless geändert: 'systemctl restart stealth-scraper.service' nötig."
                        if clean["scraper"]["headless"] != old_headless
                        else "Alle Werte sofort aktiv.")}


class TargetReq(BaseModel):
    name: str = ""
    url: str = ""
    wait_for: str = ""
    selectors: dict[str, str] = Field(default_factory=dict)
    interval_minutes: int = 0  # 0 = nur manuell/täglicher Timer, sonst Cron-Abstand
    enabled: bool = True


def _clean_target(data: dict) -> dict:
    name, url = (data.get("name") or "").strip(), (data.get("url") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name fehlt/ungültig")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL muss mit http(s):// beginnen")
    iv = int(data.get("interval_minutes", 0) or 0)
    if 0 < iv < MIN_INTERVAL_MINUTES:
        iv = MIN_INTERVAL_MINUTES
    return {"name": name, "url": url, "wait_for": (data.get("wait_for") or "").strip(),
            "selectors": dict(data.get("selectors") or {}),
            "interval_minutes": iv, "enabled": bool(data.get("enabled", True))}


@app.post("/targets")
async def target_create(req: TargetReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    targets = cfg.get("targets", []) or []
    clean = _clean_target(req.model_dump())
    if any(t.get("name") == clean["name"] for t in targets):
        raise HTTPException(status_code=409, detail="Target existiert bereits")
    targets.append(clean)
    cfg["targets"] = targets
    save_config(cfg, CONFIG_PATH)
    return JSONResponse(clean)


@app.put("/targets/{name}")
async def target_update(name: str, req: TargetReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    targets = cfg.get("targets", []) or []
    idx = next((i for i, t in enumerate(targets) if t.get("name") == name), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Target nicht gefunden")
    clean = _clean_target({**req.model_dump(), "name": name})
    targets[idx] = clean
    cfg["targets"] = targets
    save_config(cfg, CONFIG_PATH)
    return JSONResponse(clean)


@app.delete("/targets/{name}")
async def target_delete(name: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    cfg["targets"] = [t for t in (cfg.get("targets", []) or []) if t.get("name") != name]
    save_config(cfg, CONFIG_PATH)
    return {"ok": True}


@app.post("/targets/{name}/run")
async def target_run(name: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    target = next((t for t in (cfg.get("targets", []) or []) if t.get("name") == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Target nicht gefunden")
    try:
        return JSONResponse(await run_target(SCRAPER, target, WDB))
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Target fehlgeschlagen: {e}")


@app.get("/targets/{name}/runs")
async def target_runs(name: str, limit: int = 20,
                      x_token: str | None = Header(default=None)):
    """Verlauf eines Seite-prüfen-Jobs (für Nachprüfung)."""
    _auth(x_token)
    return JSONResponse(WDB.target_runs(name, limit))


def _watch_ergebnis(st: dict) -> str:
    """Lesbare Zusammenfassung (nie 'Median None €')."""
    if not st.get("last_run"):
        return "noch nie gelaufen"
    med = f"Median {st['last_median']} €" if st.get("last_median") is not None else "kein Median (zu wenig Preise)"
    return (f"{st.get('last_angebote', 0)} Treffer · {st.get('last_mitpreis', 0)} mit Preis · "
            f"{med} · {st.get('last_deals', 0)} Deals")


@app.get("/api/jobs")
async def jobs_list(x_token: str | None = Header(default=None)):
    """Alle Jobs einheitlich: Watches + Targets mit Intervall, Läufen, Ergebnis."""
    _auth(x_token)
    cfg = _fresh_cfg()
    now = int(time.time())
    watches = []
    for w in cfg.get("watches", []) or []:
        st = WDB.get_state(w.get("name", ""))
        watches.append({
            "typ": "watch", "name": w.get("name"), "enabled": bool(w.get("enabled")),
            "intervall": f"alle {w.get('interval_minutes', 60)} Min",
            "last_run": st["last_run"], "next_run": next_run_ts(w, st["last_run"], now),
            "ergebnis": _watch_ergebnis(st), "status": st["last_status"],
            "fehler": st["last_error"],
        })
    targets = []
    for t in cfg.get("targets", []) or []:
        last = WDB.last_target_run(t.get("name", ""))
        iv = target_interval(t)
        nxt = (last["zeit"] + iv) if (last and iv) else (now if (iv and t.get("enabled", True)) else None)
        if nxt is not None and nxt <= now and t.get("enabled", True):
            nxt = now  # fällig
        erg = "noch nie gelaufen"
        if last:
            erg = (f"{'ok' if last['ok'] else 'fehler'} · {last['titel'][:50]} · "
                   f"{last['preis']} €" if last["preis"] else f"{'ok' if last['ok'] else 'fehler'}")
        targets.append({
            "typ": "target", "name": t.get("name"), "enabled": bool(t.get("enabled", True)),
            "intervall": f"alle {t.get('interval_minutes')} Min" if iv else "manuell",
            "last_run": last["zeit"] if last else 0, "next_run": nxt,
            "ergebnis": erg, "status": "ok" if not last or last["ok"] else "fehler",
            "fehler": "" if not last or last["ok"] else last["fehler"],
        })
    return JSONResponse({"watches": watches, "targets": targets})


STYLE = """<style>
:root{--bg:#0f1115;--card:#1a1e26;--line:#2c3340;--txt:#e8ecf1;--mut:#9aa4b2;--acc:#4caf7d;--acc-d:#1b5e20;--err:#ff8a8a}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;max-width:960px;margin:0 auto;padding:1.2em 1em 3em;background:var(--bg);color:var(--txt);line-height:1.45}
h1{font-size:1.5em;margin:.2em 0;letter-spacing:.3px}
h1::after{content:"";display:block;height:3px;width:64px;margin-top:.3em;border-radius:2px;background:linear-gradient(90deg,var(--acc),transparent)}
nav{position:sticky;top:0;background:rgba(15,17,21,.95);padding:.5em 0;z-index:5;border-bottom:1px solid var(--line);margin-bottom:1em}
nav a{margin-right:.9em}
nav a.on{font-weight:bold;color:#fff;text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1em 1.2em;margin-bottom:1.1em;box-shadow:0 2px 10px rgba(0,0,0,.35)}
.card h3{margin:.1em 0 .6em;font-size:1.05em}
label{display:block;margin:.6em 0 .25em;color:var(--mut);font-size:.88em}
input[type=text],input[type=password],input[type=number],textarea,select{width:100%;background:#0c0e12;color:var(--txt);border:1px solid #3a4353;border-radius:8px;padding:.55em .7em;font-size:.95em}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 2px rgba(76,175,125,.25)}
input:disabled{opacity:.6}
input[type=checkbox]{width:auto;accent-color:var(--acc)}
input[type=range]{accent-color:var(--acc);padding:0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:.4em 1em}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.4em 1em}
button{background:#2e7d32;color:#fff;border:1px solid transparent;border-radius:8px;padding:.6em 1.1em;margin:.45em .35em .1em 0;cursor:pointer;font-size:.92em;transition:filter .15s,transform .05s}
button:hover{filter:brightness(1.15)}button:active{transform:scale(.98)}
button.sec{background:#2a313d}button.warn{background:#5d4a00}button:disabled{opacity:.5;cursor:wait}
table{border-collapse:collapse;width:100%;margin-top:.6em;font-size:.9em}
div:has(>table){overflow-x:auto}
td,th{border:1px solid var(--line);padding:.45em .65em;text-align:left;vertical-align:top}
th{background:#222836;color:var(--mut);white-space:nowrap}
tr:nth-child(even) td{background:rgba(255,255,255,.02)}
pre{background:#0c0e12;padding:.7em;overflow:auto;border-radius:8px;font-size:.8em;border:1px solid var(--line)}
.ok{color:#7fdc7f}.err{color:var(--err)}.mut{color:var(--mut);font-size:.85em}
a{color:#7fdc7f}
.badge{display:inline-block;background:var(--acc-d);border-radius:10px;padding:.1em .6em;font-size:.85em;white-space:nowrap}
.pill{border-radius:16px !important;padding:.42em .95em !important;background:#2a313d !important}
.pill.on{background:var(--acc-d) !important;border:1px solid var(--acc) !important}
#toast{position:sticky;top:3em;z-index:10;font-weight:bold}
#toast span{display:inline-block;background:#222836;border:1px solid var(--line);border-radius:8px;padding:.5em .9em;margin-bottom:.5em}
details{margin-top:.7em}summary{cursor:pointer;color:var(--mut);padding:.2em 0}
code{background:#0c0e12;padding:.1em .4em;border-radius:4px;font-size:.88em}
.h{display:inline-block;min-width:1.15em;height:1.15em;line-height:1.15em;text-align:center;border:1px solid var(--mut);border-radius:50%;font-size:.8em;cursor:help;margin-left:.4em;color:var(--mut)}
.steps{background:#0c0e12;border:1px solid var(--line);border-radius:8px;padding:.7em 1em;font-size:.9em}
@media(max-width:700px){.grid,.grid3{grid-template-columns:1fr}body{padding:1em .7em 3em}}
</style>"""

JS_BASE = """<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hdr(){const h={'Content-Type':'application/json'};const t=localStorage.getItem('sr_token')||'';if(t)h['X-Token']=t;return h;}
function tokQ(){const t=localStorage.getItem('sr_token')||'';return t?{headers:{'X-Token':t}}:{}}
function toast(m,err){const t=$('toast');if(!t){alert(String(m).replace(/<[^>]*>/g,''));return;}t.innerHTML='<span class="'+(err?'err':'ok')+'">'+esc(m)+'</span>';clearTimeout(t._h);t._h=setTimeout(()=>t.innerHTML='',8000);}
document.addEventListener('click',e=>{const h=e.target.closest?e.target.closest('.h'):null;if(h&&h.dataset.h)toast(h.dataset.h);});
async function apiJSON(path,opts){const r=await fetch(path,opts);const t=await r.text();
let d;try{d=t?JSON.parse(t):{}}catch(e){throw new Error(t.slice(0,160)||('HTTP '+r.status));}
if(!r.ok)throw new Error((d&&d.detail)||('HTTP '+r.status));return d;}
function fmtT(ts){return ts?new Date(ts*1000).toLocaleString('de-DE'):'nie';}
function fmtIn(ts){if(!ts)return'&ndash;';const s=ts-Math.floor(Date.now()/1000);if(s<=0)return'<b>f&auml;llig</b>';if(s<3600)return'in '+Math.ceil(s/60)+' Min';if(s<86400)return'in '+(s/3600).toFixed(1)+' Std';return'in '+Math.round(s/86400)+' Tg';}
function slug(s){return (s||'').toLowerCase().replace(/[äÄ]/g,'ae').replace(/[öÖ]/g,'oe').replace(/[üÜ]/g,'ue').replace(/ß/g,'ss').replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,40);}
function pillInit(id,hidden){const box=$(id);if(!box)return;box.querySelectorAll('.pill').forEach(b=>{b.onclick=()=>{box.querySelectorAll('.pill').forEach(x=>x.classList.remove('on'));b.classList.add('on');if(hidden&&$(hidden))$(hidden).value=b.dataset.v;};});}
function setPills(id,val){document.querySelectorAll('#'+id+' .pill').forEach(b=>b.classList.toggle('on',b.dataset.v==String(val)));}
async function health(){try{const r=await fetch('/health');const d=await r.json();
const h=$('health');if(h)h.innerHTML='<span class="ok">● bereit</span>';
const b=$('build');if(b)b.textContent='· Build '+d.version;}catch(e){const h=$('health');if(h)h.innerHTML='<span class="err">● offline</span>';}}
</script>"""


def _nav(active):
    links = [("/", "Watcher"), ("/scrape", "Seite pr\u00fcfen"),
             ("/jobs", "Jobs"), ("/settings", "Einstellungen")]
    out = "<nav>"
    for href, label in links:
        cls = ' class="on"' if href == active else ""
        out += '<a href="' + href + '"' + cls + ">" + label + "</a>"
    return out + "</nav>"


def _page(title, active, body, script):
    return ("<!DOCTYPE html>\n<html lang=\"de\"><head><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>" + title + " \u00b7 StealthScraper-LXC</title>\n" + STYLE +
            "</head><body>\n<h1>StealthScraper-LXC</h1>\n" + _nav(active) + "\n" + body +
            "\n" + JS_BASE + "\n<script>\n" + script + "\n</script></body></html>")



INDEX_BODY = """<div class="card"><h3>Schn&auml;ppchen-Watcher <span id="health" class="mut"></span></h3>
<div id="toast"></div>
<details open><summary><b>Neue Suche anlegen (Assistent)</b></summary>
<p class="mut"><b>Schritt 1:</b> Anbieter + Suchbegriff w&auml;hlen und Suche &ouml;ffnen.
<b>Schritt 2:</b> Dort PLZ + Umkreis + Preis filtern, URL kopieren und unten einf&uuml;gen.
<b>Schritt 3:</b> Testen, Abstand w&auml;hlen, speichern &mdash; fertig.</p>
<div class="grid">
<div><label>1. Anbieter <span class="h" data-h="Für welchen Marktplatz gilt die Suche? Erkennung geht auch automatisch per Domain.">?</span></label><select id="w_provider"><option value="auto">Automatisch (per Domain)</option></select>
<div class="mut" id="w_phinweis"></div></div>
<div><label>Suchbegriff <span class="h" data-h="Wonach suchst du? Z.B. ThinkPad T14, OSB-Platte, Golf 7. Wird für Suche-öffnen und Namensvorschlag genutzt.">?</span></label><input id="w_query" type="text" placeholder="z.B. ThinkPad T14">
<div><button id="b_quick" class="sec">Suche im Browser &ouml;ffnen</button></div></div>
</div>
<label>2. Kopierte Such-URL hier einf&uuml;gen <span class="h" data-h="Im geöffneten Tab: PLZ/Ort + Umkreis (z.B. 10 km), Preis und Sortierung 'Neueste zuerst' einstellen, dann die Adresse aus der Browser-Adresszeile kopieren.">?</span></label>
<input id="w_url" type="text" placeholder="https://www.kleinanzeigen.de/s-laptop/...">
<div><button id="b_preview" class="sec">URL testen (Treffer + Median anzeigen)</button></div>
<div id="preview"></div>
<div class="grid3">
<div><label>Name (Vorschlag aus Suchbegriff)</label><input id="w_name" type="text" placeholder="thinkpad-t14"></div>
<div><label>PLZ (Merkfeld)</label><input id="w_plz" type="text" placeholder="10115"></div>
<div><label>Max-Preis &euro; (Filter)</label><input id="w_maxpreis" type="number" placeholder="500" min="0"></div>
</div>
<label>Umkreis (Merkfeld, steckt in der kopierten URL)</label>
<div id="r_pills">
<button class="sec pill" data-v="5">5 km</button><button class="sec pill on" data-v="10">10 km</button><button class="sec pill" data-v="20">20 km</button><button class="sec pill" data-v="30">30 km</button><button class="sec pill" data-v="50">50 km</button><button class="sec pill" data-v="100">100 km</button>
</div>
<label>Deal-Schwelle: <b><span id="schw_val">25</span> %</b> unter Median <span class="h" data-h="So rechnet das Programm: Es sammelt alle Treffer mit Preis, sortiert sie und nimmt den mittleren Wert = Median. Beispiel: Treffer zu 400, 450, 500, 550, 900 Euro, Median = 500. Bei 25 % Schwelle meldet es alles bis 375 Euro als Deal. 0 Deals heißt: kein Treffer lag weit genug darunter (oder es wurden zu wenig Preise erkannt, siehe Treffer/Median in der Tabelle).">?</span></label>
<input id="w_schwelle_r" type="range" min="5" max="70" value="25" style="width:100%">
<label>3. Wie oft pr&uuml;fen? (Minimum 15 Min &mdash; Bot-Schutz)</label>
<div id="i_pills">
<button class="sec pill" data-v="15">alle 15 Min</button><button class="sec pill" data-v="30">alle 30 Min</button><button class="sec pill on" data-v="60">st&uuml;ndlich</button><button class="sec pill" data-v="360">alle 6 Std</button><button class="sec pill" data-v="1440">t&auml;glich</button>
</div>
<input id="w_interval" type="hidden" value="60">
<details><summary>Erweitert: Webhook + eigene Selektoren</summary>
<label>Webhook f&uuml;r Deal-Meldungen (optional)</label>
<input id="w_hook" type="text" placeholder="https://ntfy.sh/mein-topic oder Discord-Webhook">
<label>Eigene Selektoren (je Zeile <i>feld=css</i>, &uuml;berschreibt Preset)</label>
<textarea id="w_sel" rows="2" placeholder="preis=.mein-preis"></textarea>
</details>
<label><input id="w_enabled" type="checkbox" checked> Aktiviert (pausiert = kein automatischer Lauf)</label>
<div><button id="b_wsave">Watch speichern &amp; einplanen</button>
<button id="b_wcancel" class="sec" style="display:none">Abbrechen</button></div>
</details>
<h3>Geplante &amp; gelaufene Jobs</h3>
<p class="mut">Alle Jobs mit Intervall auch auf der <a href="/jobs">Jobs-Seite</a>. Details pro Suche auf der Angebots-Unterseite.</p>
<div><button id="b_watches" class="sec">Aktualisieren</button>
<button id="b_deals" class="sec">Deals laden</button></div>
<div id="watches"></div>
<h3>Gefundene Deals <select id="deal_filter" style="width:auto"><option value="">alle Watches</option></select></h3>
<div id="deals"></div></div>

<div class="card mut">StealthScraper-LXC <span id="build"></span></div>"""

INDEX_JS = """async function loadProviders(){try{const d=await apiJSON('/providers',tokQ());
PROV=d;const s=$('w_provider');d.forEach(p=>{const o=document.createElement('option');o.value=p.key;o.textContent=p.label;if(p.key==='generisch')o.textContent+=' (alle anderen Shops)';s.appendChild(o);});updPhinweis();}catch(e){}}
let PROV=[];
function updPhinweis(){const p=(PROV||[]).find(x=>x.key===$('w_provider').value);
$('w_phinweis').textContent=p&&p.hinweis?p.hinweis:'';
$('b_quick').style.display=(p&&p.suchlink)?'':'none';}
$('w_provider').onchange=updPhinweis;
$('w_query').oninput=e=>{if(!$('w_name').value.trim())$('w_name').value=slug(e.target.value);};
pillInit('r_pills');pillInit('i_pills','w_interval');
$('w_schwelle_r').oninput=e=>{$('schw_val').textContent=e.target.value;};
function radiusVal(){const b=document.querySelector('#r_pills .pill.on');return b?parseInt(b.dataset.v):10;}
$('b_quick').onclick=async()=>{const q=$('w_query').value.trim();if(!q){toast('Erst Suchbegriff eingeben',1);return;}
try{const d=await apiJSON('/watches/quicklink',{method:'POST',headers:hdr(),body:JSON.stringify({provider:$('w_provider').value,query:q})});
if(!d.url){toast('Für diesen Anbieter: Suche manuell öffnen (komplexe Filter)',1);return;}
window.open(d.url,'_blank');toast('Suche geöffnet: dort PLZ + Umkreis + Preis wählen, URL kopieren');}catch(e){toast('Fehler: '+e.message,1);}};
$('b_preview').onclick=async()=>{const u=$('w_url').value.trim();if(!u){toast('Erst Such-URL einfügen',1);return;}
$('preview').innerHTML='<span class="mut">Teste URL &hellip; (dauert ca. 10&ndash;20 s)</span>';
try{const mp=$('w_maxpreis').value.trim();
const body={search_url:u,provider:$('w_provider').value};if(mp)body.max_preis=parseFloat(mp);
const d=await apiJSON('/watches/preview',{method:'POST',headers:hdr(),body:JSON.stringify(body)});
let h='<p>'+d.angebote_gesamt+' Treffer, '+d.mit_preis+' mit Preis, Median <b>'+(d.median??'&ndash;')+' &euro;</b> <span class="mut">('+esc(d.provider_erkannt)+')</span></p>';
if(d.beispiele&&d.beispiele.length){h+='<table><tr><th>Beispiel</th><th>Preis</th></tr>';
d.beispiele.forEach(b=>{h+='<tr><td>'+esc(b.titel||b.url)+'</td><td>'+(b.preis??'&ndash;')+' &euro;</td></tr>';});h+='</table>';}
else h+='<p class="err">Keine Treffer erkannt &mdash; URL oder Selektoren prüfen.</p>';
$('preview').innerHTML=h;}catch(e){$('preview').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}};
function wBody(){const v=id=>$(id).value.trim();
const sel={};$('w_sel').value.split('\\n').forEach(l=>{const i=l.indexOf('=');if(i>0){const k=l.slice(0,i).trim(),vv=l.slice(i+1).trim();if(k&&vv)sel[k]=vv;}});
const b={name:v('w_name')||slug(v('w_query'))||('watch-'+Date.now().toString(36)),enabled:$('w_enabled').checked,provider:$('w_provider').value,search_url:v('w_url'),
query:v('w_query'),plz:v('w_plz'),radius_km:radiusVal(),
deal_schwelle_prozent:parseInt($('w_schwelle_r').value)||25,interval_minutes:parseInt($('w_interval').value)||60,
notify_webhook:v('w_hook'),selectors:sel};const mp=v('w_maxpreis');if(mp)b.max_preis=parseFloat(mp);return b;}
let EDIT=null;
function setPillsLocal(id,val){setPills(id,val);}
$('b_wsave').onclick=async()=>{const b=wBody();if(!b.search_url){toast('Such-URL einfügen',1);return;}
try{let d;
if(EDIT){d=await apiJSON('/watches/'+encodeURIComponent(EDIT),{method:'PUT',headers:hdr(),body:JSON.stringify(b)});cancelEdit();toast('Gespeichert: '+d.name);}
else{d=await apiJSON('/watches',{method:'POST',headers:hdr(),body:JSON.stringify(b)});toast('Eingeplant: '+d.name+' (alle '+d.interval_minutes+' Min)');}
loadWatches();}catch(e){toast('Fehler: '+e.message+(EDIT?'':' (Tipp: erst „URL testen“)'),1);}};
$('b_wcancel').onclick=cancelEdit;
function cancelEdit(){EDIT=null;$('w_name').disabled=false;$('b_wsave').textContent='Watch speichern & einplanen';$('b_wcancel').style.display='none';}
async function editWatch(n){try{const d=await apiJSON('/watches',tokQ());const w=d.find(x=>x.name===n);if(!w){toast('Watch nicht gefunden',1);return;}
EDIT=n;$('w_provider').value=w.provider||'auto';updPhinweis();
$('w_query').value=w.query||'';$('w_url').value=w.search_url||'';$('w_name').value=w.name;$('w_name').disabled=true;
$('w_plz').value=w.plz||'';$('w_maxpreis').value=w.max_preis??'';
setPills('r_pills',w.radius_km||10);setPills('i_pills',w.interval_minutes||60);$('w_interval').value=w.interval_minutes||60;
$('w_schwelle_r').value=w.deal_schwelle_prozent||25;$('schw_val').textContent=w.deal_schwelle_prozent||25;
$('w_hook').value=w.notify_webhook||'';$('w_enabled').checked=w.enabled!==false;
const sel=w.selectors||{};$('w_sel').value=Object.keys(sel).map(k=>k+'='+sel[k]).join('\\n');
$('b_wsave').textContent='Änderungen speichern';$('b_wcancel').style.display='';
toast('Bearbeite '+n+' (Name fest, Rest änderbar)');window.scrollTo({top:0,behavior:'smooth'});
}catch(e){toast('Fehler: '+e.message,1);}}
async function loadWatches(){try{const r=await fetch('/watches',tokQ());const t=await r.text();const d=t?JSON.parse(t):[];
const df=$('deal_filter');const cur=df.value;df.innerHTML='<option value="">alle Watches</option>';
d.forEach(w=>{const o=document.createElement('option');o.value=w.name;o.textContent=w.name;df.appendChild(o);});df.value=cur;
if(!d.length){$('watches').innerHTML='<p class="mut">Noch keine Jobs. Lege oben deine erste Suche an.</p>';return;}
let h='<table><tr><th>Job</th><th>Intervall</th><th>Zuletzt</th><th>N&auml;chster Lauf</th><th>Ergebnis</th><th>Status</th><th>Aktion</th></tr>';
d.forEach(w=>{const s=w.summary||{};
const med=s.median?' &middot; Median '+s.median+' &euro;':' &middot; <span class="mut">kein Median (zu wenig Preise)</span>';
const erg=(s.angebote||0)+' Treffer &middot; '+(s.mit_preis||0)+' mit Preis'+med+' &middot; <b>'+(s.deals||0)+' Deals</b>';
h+='<tr><td><b>'+esc(w.name)+'</b>'+(w.enabled?'':' <span class="mut">(pausiert)</span>')+'<br><span class="mut">'+esc((w.query||'')+' '+(w.plz||'')+(w.radius_km?' +'+w.radius_km+'km':''))+'</span></td>'
+'<td>alle '+w.interval_minutes+' Min</td><td>'+fmtT(w.last_run)+'</td><td>'+(w.enabled?fmtIn(w.next_run):'&ndash;')+'</td>'
+'<td>'+erg+'</td><td>'+(w.last_status==='ok'?'<span class="ok">ok</span>':w.last_status==='nie'?'<span class="mut">wartet</span>':'<span class="err">'+esc(w.last_status)+'</span>')+(w.last_error?'<br><span class="mut">'+esc(w.last_error.slice(0,80))+'</span>':'')+'</td>'
+'<td><button class="sec" onclick="runWatch(\\''+esc(w.name)+'\\')">Jetzt pr&uuml;fen</button><br>'
+'<a target="_blank" href="/watch/'+encodeURIComponent(w.name)+'"><button class="sec">Angebote</button></a> '
+'<button class="sec" onclick="editWatch(\\''+esc(w.name)+'\\')">Bearbeiten</button><br>'
+'<button class="sec" onclick="toggleWatch(\\''+esc(w.name)+'\\','+(w.enabled?'0':'1')+')">'+(w.enabled?'Pausieren':'Aktivieren')+'</button> '
+'<button class="warn" onclick="delWatch(\\''+esc(w.name)+'\\')">L&ouml;schen</button></td></tr>';});
h+='</table>';$('watches').innerHTML=h;}catch(e){$('watches').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
$('b_watches').onclick=loadWatches;
async function toggleWatch(n,en){try{const d=await apiJSON('/watches',tokQ());
const w=d.find(x=>x.name===n);if(!w)return;w.enabled=!!en;
await apiJSON('/watches/'+encodeURIComponent(n),{method:'PUT',headers:hdr(),body:JSON.stringify(w)});loadWatches();}catch(e){toast('Fehler: '+e.message,1);}}
async function runWatch(n){toast('Prüfe '+n+' … (dauert ca. 30–60 s)');
try{const d=await apiJSON('/watches/'+encodeURIComponent(n)+'/run',{method:'POST',headers:hdr()});
toast(d.watch+': '+d.angebote_gesamt+' Angebote, Median '+(d.median??'-')+' €, '+d.deals.length+' Deals');loadWatches();loadDeals();}catch(e){toast('Fehler: '+e.message,1);loadWatches();}}
async function delWatch(n){if(!confirm('Watch \\''+n+'\\' wirklich löschen?'))return;
await apiJSON('/watches/'+encodeURIComponent(n),{method:'DELETE',headers:hdr()}).then(loadWatches).catch(e=>toast('Fehler: '+e.message,1));}
async function loadDeals(){const wf=$('deal_filter').value;const q=wf?'?watch='+encodeURIComponent(wf)+'&limit=50':'?limit=50';
try{const d=await apiJSON('/deals'+q,tokQ());
if(!d.length){$('deals').innerHTML='<p class="mut">Noch keine Deals gefunden.</p>';return;}
let h='<table><tr><th>Deal</th><th>Preis</th><th>Median</th><th>Grund</th><th>Wann</th></tr>';
d.forEach(t=>{h+='<tr><td><a target="_blank" href="'+esc(t.url)+'">'+esc(t.titel||t.url)+'</a><br><span class="mut">'+esc(t.watch)+'</span></td>'
+'<td>'+esc(t.preis)+' € <span class="badge">-'+esc(t.rabatt)+'%</span></td><td>'+esc(t.median)+' €</td>'
+'<td>'+esc(t.grund)+'</td><td>'+new Date(t.zeit*1000).toLocaleString('de-DE')+'</td></tr>';});
h+='</table>';$('deals').innerHTML=h;}catch(e){$('deals').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
$('b_deals').onclick=loadDeals;$('deal_filter').onchange=loadDeals;
loadProviders();loadWatches();loadDeals();health();setInterval(loadWatches,60000);
const _eq=new URLSearchParams(location.search).get('edit');if(_eq)editWatch(_eq);"""



SCRAPE_BODY = """<div class="card"><h3>Seite pr&uuml;fen <span id="health" class="mut"></span></h3>
<div id="toast"></div>
<label>URL <span class="h" data-h="Genaue Adresse der Seite aus der Browser-Adresszeile kopieren, z.B. https://shop.de/produkt/123.">?</span></label>
<input id="url" type="text" placeholder="https://shop.example/produkt/123">
<div class="grid">
<div><label>Preis-Selektor (optional) <span class="h" data-h="CSS-Selektor, der genau auf den Preis zeigt, z.B. .price. Leer lassen = Auto-Erkennung (JSON-LD, Meta, sichtbarer Preis). Finden: siehe Anleitung unten.">?</span></label><input id="s_preis" type="text" placeholder=".price"></div>
<div><label>Verf&uuml;gbarkeit-Selektor (optional) <span class="h" data-h="CSS-Selektor für den Lager-Text, z.B. .stock oder .availability. Leer = automatische Textsuche (auf Lager, lieferbar, ausverkauft ...).">?</span></label><input id="s_avail" type="text" placeholder=".stock, .availability"></div>
<div><label>Anzahl/Menge-Selektor (optional) <span class="h" data-h="CSS-Selektor für Mengen-/Stückzahl-Angaben, z.B. .qty. Nur wenn die Seite so etwas zeigt.">?</span></label><input id="s_qty" type="text" placeholder=".qty, .amount"></div>
<div><label>Titel-Selektor (optional) <span class="h" data-h="CSS-Selektor für die Überschrift, z.B. h1. Leer = Seitentitel wird genommen.">?</span></label><input id="s_title" type="text" placeholder="h1"></div>
</div>
<label>Eigene Felder (je Zeile <i>feld=css-selektor</i>, optional) <span class="h" data-h="Eigene Werte auslesen, z.B. artikelnummer=.sku pro Zeile. Links der Feldname, rechts der CSS-Selektor.">?</span></label>
<textarea id="s_custom" rows="2" placeholder="artikelnummer=.sku&#10;bewertung=.stars"></textarea>
<div class="grid">
<div><label>Warten auf Selektor (optional) <span class="h" data-h="Wenn Preise per JavaScript nachladen: Selektor angeben, auf den gewartet wird (z.B. .price). Leer lassen wenn unsicher.">?</span></label><input id="waitfor" type="text" placeholder=".price"></div>
<div><label>API-Token (nur wenn in config.yaml gesetzt)</label><input id="token" type="text" placeholder="X-Token"></div>
</div>
<label><input id="fresh" type="checkbox"> Cache umgehen (fresh)</label>
<div>
<button id="b_price">Auto-Preis pr&uuml;fen</button>
<button id="b_scrape" class="sec">Mit Selektoren scrapen</button>
</div>
<p class="mut">Auto-Preis braucht keine Selektoren (JSON-LD &rarr; Meta &rarr; sichtbarer Preis &rarr; Regex).
Alle gefundenen Preisquellen werden als Kandidaten angezeigt.</p>
<details><summary><b>So findest du CSS-Selektoren (30 Sekunden)</b></summary>
<div class="steps"><ol>
<li>Seite im <b>Chrome/Firefox</b> öffnen, <b>F12</b> drücken (Entwicklertools).</li>
<li>Oben links das <b>Pfeil-Symbol</b> (Element auswählen) anklicken, dann auf den <b>Preis</b> klicken.</li>
<li>Im HTML-Baum Rechtsklick auf die markierte Zeile &rarr; <b>Kopieren &rarr; Selector kopieren</b>.</li>
<li>Kopierten Wert oben einfügen und testen. Reicht eine Klasse wie <code>.price</code>, nimm die kurze Form.</li>
<li>Mehrere Treffer? Komma-getrennt angeben, der erste Treffer gewinnt.</li>
</ol></div></details>
</div>

<div class="card"><h3>Ergebnis</h3><div id="res"><span class="mut">Noch nichts geprüft.</span></div></div>

<div class="card"><h3>Als wiederkehrenden Job speichern</h3>
<p class="mut">Legt diese Abfrage als Target an &mdash; erscheint in der <a href="/jobs">Jobs-Tabelle</a> und läuft automatisch im gewählten Abstand.</p>
<div class="grid">
<div><label>Job-Name</label><input id="j_name" type="text" placeholder="bauhaus-osb"></div>
<div><label>Abstand</label><div id="j_pills">
<button class="sec pill on" data-v="0">manuell</button><button class="sec pill" data-v="60">st&uuml;ndlich</button><button class="sec pill" data-v="360">alle 6 Std</button><button class="sec pill" data-v="1440">t&auml;glich</button>
</div></div>
</div>
<input id="j_interval" type="hidden" value="0">
<div><button id="b_jsave" class="sec">Als Job speichern</button></div>
</div>

<div class="card mut">StealthScraper-LXC <span id="build"></span></div>"""

SCRAPE_JS = """$('token').value=localStorage.getItem('sr_token')||'';
$('token').onchange=e=>localStorage.setItem('sr_token',e.target.value);
function hdr(){const h={'Content-Type':'application/json'};const t=$('token').value.trim();if(t)h['X-Token']=t;return h;}
function tokQ(){const t=$('token').value.trim();return t?{headers:{'X-Token':t}}:{}}
function busy(b){['b_price','b_scrape'].forEach(i=>{const e=$(i);if(e)e.disabled=b;});}
function buildSel(){const s={};const g=(id,n)=>{const v=$(id).value.trim();if(v)s[n]=v;};
g('s_preis','preis');g('s_avail','verfuegbarkeit');g('s_qty','anzahl');g('s_title','titel');
$('s_custom').value.split('\\n').forEach(l=>{const i=l.indexOf('=');if(i>0){const k=l.slice(0,i).trim(),v=l.slice(i+1).trim();if(k&&v)s[k]=v;}});return s;}
function row(k,v,cls){return '<tr><th>'+esc(k)+'</th><td class="'+(cls||'')+'">'+esc(v)+'</td></tr>';}
function render(d){
let h='<table>';
['titel','preis','waehrung','verfuegbarkeit','quelle','status','cached','url'].forEach(k=>{if(d[k]!==undefined&&d[k]!==''&&d[k]!==null)h+=row(k,d[k],k==='preis'?'ok':'');});
const sub=(o,t)=>{if(o&&typeof o==='object')Object.keys(o).forEach(k=>{h+=row(t+': '+k,o[k]);});};
sub(d.felder,'Feld');sub(d.auto,'Auto');
if(d.kandidaten&&d.kandidaten.length){h+='<tr><th>Preis-Kandidaten</th><td><table>';
d.kandidaten.forEach(k=>{h+='<tr><td><b>'+esc(k.wert)+' &euro;</b></td><td>'+esc(k.quelle)+'</td><td><span class="mut">'+esc(k.detail||'')+'</span></td></tr>';});
h+='</table><span class="mut">Gewählt: '+esc(d.quelle)+' &mdash; stimmt der Preis nicht, eigenen Preis-Selektor setzen.</span></td></tr>';}
if(d.fehler)h+=row('Fehler',d.fehler,'err');
h+='</table><details><summary>Roh-JSON</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>';
$('res').innerHTML=h;}
async function call(path,body){
const u=$('url').value.trim();if(!u&&body.url!==undefined&&!body.url){$('res').innerHTML='<span class="err">Bitte URL eingeben.</span>';return;}
busy(true);$('res').innerHTML='<span class="mut">L&auml;dt &hellip;</span>';
try{render(await apiJSON(path,{method:'POST',headers:hdr(),body:JSON.stringify(body)}));}
catch(e){$('res').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}busy(false);}
$('b_price').onclick=()=>call('/price',{url:$('url').value.trim(),wait_for:$('waitfor').value.trim(),fresh:$('fresh').checked});
$('b_scrape').onclick=()=>call('/scrape',{url:$('url').value.trim(),selectors:buildSel(),wait_for:$('waitfor').value.trim(),fresh:$('fresh').checked});
pillInit('j_pills','j_interval');
$('b_jsave').onclick=async()=>{const u=$('url').value.trim();const n=$('j_name').value.trim()||slug(u.split('/')[2]||'seite');
if(!u){toast('Erst URL eingeben und prüfen',1);return;}
try{const d=await apiJSON('/targets',{method:'POST',headers:hdr(),body:JSON.stringify({name:n,url:u,wait_for:$('waitfor').value.trim(),selectors:buildSel(),interval_minutes:parseInt($('j_interval').value)||0})});
toast('Job gespeichert: '+d.name+(d.interval_minutes?' (alle '+d.interval_minutes+' Min)':' (manuell)')+' — siehe Jobs-Seite');}catch(e){toast('Fehler: '+e.message,1);}};
health();"""



JOBS_BODY = """<div class="card"><h3>Alle Jobs <span id="health" class="mut"></span></h3>
<div id="toast"></div>
<p class="mut">Watches (Schnäppchen) und Targets (Seite prüfen) mit eigenem Prüf-Abstand.
Bearbeiten: Watches auf der <a href="/">Startseite</a> (oder ?edit=), Targets hier unten.</p>
<div><button id="b_reload" class="sec">Aktualisieren</button></div>
<h3>Schnäppchen-Watches</h3>
<div id="jw"></div>
<h3>Seite-prüfen-Jobs (Targets)</h3>
<div id="jt"></div>
<div id="thist"></div>
<details id="teditbox" style="display:none"><summary><b>Target bearbeiten</b></summary>
<div class="grid">
<div><label>Name (fest)</label><input id="t2_name" type="text" disabled></div>
<div><label>Warten auf Selektor</label><input id="t2_wait" type="text"></div>
</div>
<label>URL</label><input id="t2_url" type="text">
<label>Abstand (0 = nur manuell)</label>
<div id="t2_pills">
<button class="sec pill" data-v="0">manuell</button><button class="sec pill" data-v="15">alle 15 Min</button><button class="sec pill" data-v="60">st&uuml;ndlich</button><button class="sec pill" data-v="360">alle 6 Std</button><button class="sec pill" data-v="1440">t&auml;glich</button>
</div>
<input id="t2_interval" type="hidden" value="0">
<label>Selektoren (je Zeile <i>feld=css</i>)</label>
<textarea id="t2_sel" rows="2"></textarea>
<label><input id="t2_enabled" type="checkbox" checked> Aktiviert</label>
<div><button id="b_t2save">Änderungen speichern</button>
<button id="b_t2cancel" class="sec">Abbrechen</button></div>
</details></div>

<div class="card mut">StealthScraper-LXC <span id="build"></span></div>"""

JOBS_JS = """async function loadJobs(){try{const d=await apiJSON('/api/jobs',tokQ());
let h='<table><tr><th>Job</th><th>Intervall</th><th>Zuletzt</th><th>N&auml;chster Lauf</th><th>Ergebnis</th><th>Status</th><th>Aktion</th></tr>';
if(!d.watches.length)h+='<tr><td colspan="7"><span class="mut">Keine Watches — <a href="/">anlegen</a></span></td></tr>';
d.watches.forEach(w=>{
h+='<tr><td><b>'+esc(w.name)+'</b>'+(w.enabled?'':' <span class="mut">(pausiert)</span>')+'</td>'
+'<td>'+esc(w.intervall)+'</td><td>'+fmtT(w.last_run)+'</td><td>'+(w.enabled?fmtIn(w.next_run):'&ndash;')+'</td>'
+'<td>'+esc(w.ergebnis)+'</td><td>'+(w.status==='ok'?'<span class="ok">ok</span>':w.status==='nie'?'<span class="mut">wartet</span>':'<span class="err">'+esc(w.status)+'</span>')+'</td>'
+'<td><button class="sec" onclick="runWatch(\\''+esc(w.name)+'\\')">Jetzt pr&uuml;fen</button><br>'
+'<a target="_blank" href="/watch/'+encodeURIComponent(w.name)+'"><button class="sec">Angebote</button></a> '
+'<a href="/?edit='+encodeURIComponent(w.name)+'"><button class="sec">Bearbeiten</button></a></td></tr>';});
h+='</table>';$('jw').innerHTML=h;
let t='<table><tr><th>Job</th><th>Intervall</th><th>Zuletzt</th><th>N&auml;chster Lauf</th><th>Ergebnis</th><th>Status</th><th>Aktion</th></tr>';
if(!d.targets.length)t+='<tr><td colspan="7"><span class="mut">Keine Targets — auf <a href="/scrape">Seite prüfen</a> als Job speichern.</span></td></tr>';
d.targets.forEach(w=>{
t+='<tr><td><b>'+esc(w.name)+'</b>'+(w.enabled?'':' <span class="mut">(pausiert)</span>')+'</td>'
+'<td>'+esc(w.intervall)+'</td><td>'+fmtT(w.last_run)+'</td><td>'+(w.enabled?fmtIn(w.next_run):'&ndash;')+'</td>'
+'<td>'+esc(w.ergebnis)+(w.fehler?'<br><span class="mut">'+esc(w.fehler.slice(0,80))+'</span>':'')+'</td>'
+'<td>'+(w.status==='ok'?'<span class="ok">ok</span>':'<span class="err">fehler</span>')+'</td>'
+'<td><button class="sec" onclick="runTarget(\\''+esc(w.name)+'\\')">Jetzt pr&uuml;fen</button><br>'
+'<button class="sec" onclick="showRuns(\\''+esc(w.name)+'\\')">Verlauf</button> '
+'<button class="sec" onclick="editTarget(\\''+esc(w.name)+'\\')">Bearbeiten</button><br>'
+'<button class="warn" onclick="delTarget(\\''+esc(w.name)+'\\')">L&ouml;schen</button></td></tr>';});
t+='</table>';$('jt').innerHTML=t;}catch(e){$('jw').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
$('b_reload').onclick=loadJobs;
async function runWatch(n){toast('Prüfe '+n+' …');
try{const d=await apiJSON('/watches/'+encodeURIComponent(n)+'/run',{method:'POST',headers:hdr()});
toast(d.watch+': '+d.angebote_gesamt+' Angebote, '+d.deals.length+' Deals');loadJobs();}catch(e){toast('Fehler: '+e.message,1);loadJobs();}}
async function runTarget(n){toast('Prüfe '+n+' …');
try{const d=await apiJSON('/targets/'+encodeURIComponent(n)+'/run',{method:'POST',headers:hdr()});
toast(d.ok?(d.titel.slice(0,60)+' — '+d.preis+' €'):'Fehler: '+d.fehler, d.ok?0:1);loadJobs();showRuns(n);}catch(e){toast('Fehler: '+e.message,1);}}
async function delTarget(n){if(!confirm('Target \\''+n+'\\' löschen?'))return;
await apiJSON('/targets/'+encodeURIComponent(n),{method:'DELETE',headers:hdr()}).then(loadJobs).catch(e=>toast('Fehler: '+e.message,1));}
async function showRuns(n){try{const d=await apiJSON('/targets/'+encodeURIComponent(n)+'/runs?limit=20',tokQ());
if(!d.length){$('thist').innerHTML='<p class="mut">Noch keine Läufe.</p>';return;}
let h='<h3>Verlauf: '+esc(n)+'</h3><table><tr><th>Wann</th><th>Ergebnis</th><th>Status</th></tr>';
d.forEach(r=>{h+='<tr><td>'+fmtT(r.zeit)+'</td><td>'+esc((r.titel||'').slice(0,60)+' '+(r.preis||'')+(r.preis?' €':''))+(r.fehler?'<br><span class="err">'+esc(r.fehler.slice(0,100))+'</span>':'')+'</td><td>'+(r.ok?r.status:'<span class="err">fehler</span>')+'</td></tr>';});
h+='</table>';$('thist').innerHTML=h;}catch(e){toast('Fehler: '+e.message,1);}}
let TEDIT=null;
pillInit('t2_pills','t2_interval');
async function editTarget(n){try{const d=await apiJSON('/targets',tokQ());const w=d.find(x=>x.name===n);if(!w)return;
TEDIT=n;$('t2_name').value=w.name;$('t2_url').value=w.url||'';$('t2_wait').value=w.wait_for||'';
setPills('t2_pills',w.interval_minutes||0);$('t2_interval').value=w.interval_minutes||0;
$('t2_enabled').checked=w.enabled!==false;
const sel=w.selectors||{};$('t2_sel').value=Object.keys(sel).map(k=>k+'='+sel[k]).join('\\n');
$('teditbox').style.display='';window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'});}catch(e){toast('Fehler: '+e.message,1);}}
$('b_t2cancel').onclick=()=>{TEDIT=null;$('teditbox').style.display='none';};
$('b_t2save').onclick=async()=>{if(!TEDIT)return;const sel={};
$('t2_sel').value.split('\\n').forEach(l=>{const i=l.indexOf('=');if(i>0){const k=l.slice(0,i).trim(),v=l.slice(i+1).trim();if(k&&v)sel[k]=v;}});
try{await apiJSON('/targets/'+encodeURIComponent(TEDIT),{method:'PUT',headers:hdr(),body:JSON.stringify({name:TEDIT,url:$('t2_url').value.trim(),wait_for:$('t2_wait').value.trim(),selectors:sel,interval_minutes:parseInt($('t2_interval').value)||0,enabled:$('t2_enabled').checked})});
toast('Gespeichert: '+TEDIT);TEDIT=null;$('teditbox').style.display='none';loadJobs();}catch(e){toast('Fehler: '+e.message,1);}};
loadJobs();health();setInterval(loadJobs,60000);"""



INDEX_HTML = _page("Watcher", "/", INDEX_BODY, INDEX_JS)
SCRAPE_HTML = _page("Seite pr\u00fcfen", "/scrape", SCRAPE_BODY, SCRAPE_JS)
JOBS_HTML = _page("Jobs", "/jobs", JOBS_BODY, JOBS_JS)


def watch_detail_page(name, snapshot):
    rows = "".join(
        "<tr><td><a target=\"_blank\" href=\"" + html.escape(a.get("url", "")) + "\">" +
        html.escape(a.get("titel") or a.get("url", "")) + "</a></td><td>" +
        html.escape(str(a.get("preis", "")) if a.get("preis") is not None else
                    a.get("preis_text", "")) + " &euro;</td><td>" +
        html.escape(a.get("ort") or "&ndash;") + "</td></tr>"
        for a in snapshot
    )
    body = ("<div class=\"card\"><h3>Angebote: " + html.escape(name) + "</h3>"
            "<div id=\"toast\"></div>"
            "<p class=\"mut\">Alle Treffer des letzten Laufs &mdash; auch ohne Deal, zum Nachpr\u00fcfen. "
            "<a href=\"/\">Zur\u00fcck zum Watcher</a> &middot; <a href=\"/jobs\">Alle Jobs</a></p>"
            + ("<table><tr><th>Angebot</th><th>Preis</th><th>Ort</th></tr>" + rows + "</table>"
               if snapshot else "<p class=\"mut\">Noch keine Angebote gespeichert &mdash; erst „Jetzt pr\u00fcfen“.</p>")
            + "</div><div class=\"card mut\">StealthScraper-LXC</div>")
    return ("<!DOCTYPE html>\n<html lang=\"de\"><head><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Angebote \u00b7 StealthScraper-LXC</title>\n" + STYLE +
            "</head><body>\n<h1>StealthScraper-LXC</h1>\n" + _nav("/") + "\n" + body +
            "\n</body></html>")



SETTINGS_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Einstellungen &middot; StealthScraper-LXC</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:960px;margin:0 auto;padding:1.2em 1em 3em;background:#0f1115;color:#e8ecf1;line-height:1.45}
h1{font-size:1.5em;margin:.2em 0}
nav{position:sticky;top:0;background:rgba(15,17,21,.95);padding:.5em 0;z-index:5;border-bottom:1px solid #2c3340;margin-bottom:1em}
.card{background:#1a1e26;border:1px solid #2c3340;border-radius:12px;padding:1.1em 1.2em;margin-bottom:1.1em;box-shadow:0 2px 10px rgba(0,0,0,.35)}
.card h3{margin:.1em 0 .6em;font-size:1.05em}
label{display:block;margin:.6em 0 .25em;color:#9aa4b2;font-size:.88em}
input[type=text],input[type=password],input[type=number],textarea{width:100%;box-sizing:border-box;background:#0c0e12;color:#e8ecf1;border:1px solid #3a4353;border-radius:8px;padding:.55em .7em;font-size:.95em}
input:focus{outline:none;border-color:#4caf7d;box-shadow:0 0 0 2px rgba(76,175,125,.25)}
input[type=checkbox]{width:auto;accent-color:#4caf7d}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:.4em 1em}
button{background:#2e7d32;color:#fff;border:1px solid transparent;border-radius:8px;padding:.6em 1.1em;margin:.45em .35em .1em 0;cursor:pointer;font-size:.92em}
button:hover{filter:brightness(1.15)}button.sec{background:#2a313d}
.ok{color:#7fdc7f}.err{color:#ff8a8a}.mut{color:#9aa4b2;font-size:.85em}
a{color:#7fdc7f}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Einstellungen</h1>
<nav><a href="/">Watcher</a> &middot; <a href="/scrape">Seite pr&uuml;fen</a> &middot; <a href="/jobs">Jobs</a> &middot; <a href="/settings">Einstellungen</a></nav>
<div id="msg"></div>
<div class="card"><h3>Server</h3>
<div class="grid">
<div><label>Port (nur Anzeige &mdash; &Auml;nderung per config.yaml + Neustart)</label><input id="g_port" type="text" disabled></div>
<div><label>API-Token (leer = kein Auth, nur Heimnetz!)</label><input id="g_token_new" type="password" placeholder="Nur bei Änderung ausfüllen">
<label><input id="g_tokclear" type="checkbox"> Token komplett entfernen</label>
</div>
</div>
<label>Gespeicherter Token (Anzeige)</label><input id="g_token_saved" type="text" disabled>
<p class="mut">Achtung: Nach Token-&Auml;nderung hier oben unter &bdquo;Start&ldquo; den neuen Token eintragen (wird im Browser gespeichert).</p>
</div>
<div class="card"><h3>Scraper</h3>
<div class="grid">
<div><label>Timeout Sekunden (5&ndash;120)</label><input id="g_timeout" type="number" min="5" max="120"></div>
<div><label>Nachlade-Wartezeit ms (0&ndash;10000)</label><input id="g_wait" type="number" min="0" max="10000"></div>
<div><label>Sprache (locale)</label><input id="g_locale" type="text"></div>
<div><label>Zeitzone</label><input id="g_tz" type="text"></div>
<div><label>Viewport Breite</label><input id="g_vw" type="number" min="800" max="3840"></div>
<div><label>Viewport H&ouml;he</label><input id="g_vh" type="number" min="600" max="2160"></div>
</div>
<label>User-Agent</label><input id="g_ua" type="text">
<label>Proxy (leer = direkt, z.B. http://user:pass@proxy:8080)</label><input id="g_proxy" type="text">
<label><input id="g_headless" type="checkbox"> Headless (aus = sichtbarer Browser; &Auml;nderung braucht Neustart)</label>
<label><input id="g_nobilder" type="checkbox"> Bilder blockieren (schneller, sparsamer)</label>
</div>
<div class="card"><h3>Cache &amp; Watcher</h3>
<div class="grid">
<div><label>Cache TTL Stunden (0 = aus)</label><input id="g_ttl" type="number" min="0" max="720" step="0.5"></div>
<div><label>Watcher</label><br><label><input id="g_watcher" type="checkbox"> Schn&auml;ppchen-Watcher aktiv</label></div>
</div></div>
<div><button id="b_save">Speichern (sofort aktiv)</button></div>
<p class="mut" id="hint"></p>
<script>
const $=id=>document.getElementById(id);
const tok=localStorage.getItem('sr_token')||'';
function hdr(){const h={'Content-Type':'application/json'};if(tok)h['X-Token']=tok;return h;}
function tq(){return tok?{headers:{'X-Token':tok}}:{}}
async function load(){try{const r=await fetch('/settings/data',tq());const d=await r.json();
if(!r.ok)throw new Error(d.detail||r.status);
$('g_port').value=d.port;$('g_token_saved').value=d.server.api_token||'(kein Token gesetzt)';
$('g_timeout').value=d.scraper.timeout_seconds??30;$('g_wait').value=d.scraper.extra_wait_ms??1500;
$('g_locale').value=d.scraper.locale||'de-DE';$('g_tz').value=d.scraper.timezone||'Europe/Berlin';
$('g_vw').value=d.scraper.viewport_width??1366;$('g_vh').value=d.scraper.viewport_height??768;
$('g_ua').value=d.scraper.user_agent||'';$('g_proxy').value=d.scraper.proxy||'';
$('g_headless').checked=d.scraper.headless!==false;$('g_nobilder').checked=d.scraper.block_images!==false;
$('g_ttl').value=d.cache.ttl_hours??6;$('g_watcher').checked=d.watcher.enabled!==false;
}catch(e){$('msg').innerHTML='<span class="err">Fehler: '+e.message+' (ggf. Token oben auf Start-Seite setzen)</span>';}}
$('b_save').onclick=async()=>{const b={
scraper:{timeout_seconds:+$('g_timeout').value,extra_wait_ms:+$('g_wait').value,headless:$('g_headless').checked,
locale:$('g_locale').value,timezone:$('g_tz').value,viewport_width:+$('g_vw').value,viewport_height:+$('g_vh').value,
user_agent:$('g_ua').value,block_images:$('g_nobilder').checked,proxy:$('g_proxy').value},
cache:{ttl_hours:+$('g_ttl').value},watcher:{enabled:$('g_watcher').checked}};
const nt=$('g_token_new').value;if(nt)b.server={api_token:nt};
if($('g_tokclear').checked)b.server={api_token:''};
try{const r=await fetch('/settings/save',{method:'POST',headers:hdr(),body:JSON.stringify(b)});const d=await r.json();
if(!r.ok)throw new Error(d.detail||r.status);
$('msg').innerHTML='<span class="ok">Gespeichert: '+d.hinweis+'</span>';
if(d.restart_needed)$('hint').textContent='Neustart nötig: pct exec <CTID> -- systemctl restart stealth-scraper.service';
load();}catch(e){$('msg').innerHTML='<span class="err">Fehler: '+e.message+'</span>';}};
load();
</script></body></html>"""


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return SETTINGS_HTML


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/scrape", response_class=HTMLResponse)
async def scrape_page():
    return SCRAPE_HTML


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page():
    return JOBS_HTML


@app.get("/watch/{name}", response_class=HTMLResponse)
async def watch_page(name: str):
    cfg = _fresh_cfg()
    if not any(w.get("name") == name for w in (cfg.get("watches", []) or [])):
        raise HTTPException(status_code=404, detail="Watch nicht gefunden")
    return watch_detail_page(name, WDB.get_snapshot(name, 200))


async def _fetch_cached(url: str, wait_for: str, extra: str, fresh: bool) -> dict:
    if not fresh:
        hit = CACHE.get(url, extra)
        # Nur echte Seiten-Treffer verwenden (alte Cache-Einträge ohne html/title ignorieren)
        if hit and isinstance(hit, dict) and "html" in hit and "title" in hit:
            hit["cached"] = True
            return hit
    page = await SCRAPER.fetch(url, wait_for=wait_for)
    return {**page, "cached": False}


def _store_page(url: str, extra: str, page: dict, fresh: bool) -> None:
    """Ganze Seite cachen (mit html/title) statt abgeleiteter Daten."""
    if not fresh:
        CACHE.set(url, extra, {"url": page.get("url", url), "html": page.get("html", ""),
                               "title": page.get("title", ""), "status": page.get("status", 0)})


@app.post("/scrape")
async def scrape(req: ScrapeReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cache_key = str(sorted(req.selectors.items()))
    try:
        page = await _fetch_cached(req.url, req.wait_for, cache_key, req.fresh)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Scrape fehlgeschlagen: {e}")
    data = {"url": req.url, "titel": page["title"], "status": page["status"], "cached": page["cached"]}
    if req.selectors:
        data["felder"] = extract_with_selectors(page["html"], req.selectors)
    else:
        data["auto"] = auto_price(page["html"])
    _store_page(req.url, cache_key, page, req.fresh)
    return JSONResponse(data)


@app.post("/price")
async def price(req: PriceReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    try:
        page = await _fetch_cached(req.url, req.wait_for, "price", req.fresh)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Scrape fehlgeschlagen: {e}")
    result = {"url": req.url, "titel": page["title"], "status": page["status"],
              "cached": page["cached"], **auto_price(page["html"])}
    _store_page(req.url, "price", page, req.fresh)
    return JSONResponse(result)


@app.post("/targets/check")
async def targets_check(x_token: str | None = Header(default=None)):
    _auth(x_token)
    out = []
    for t in _fresh_cfg().get("targets", []) or []:
        if not t.get("enabled", True):
            continue
        out.append(await run_target(SCRAPER, t, WDB))
    # Ergebnisse als JSON ablegen (für Timer-Läufe)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"check-{int(time.time())}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse(out)


@app.delete("/cache")
async def cache_clear(x_token: str | None = Header(default=None)):
    _auth(x_token)
    CACHE.clear()
    return {"ok": True}
