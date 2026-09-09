"""FastAPI: Scrape/Price/Targets + Schnäppchen-Watcher (Watches, Deals) + Dashboard /."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .cache import TTLCache
from .config import load_config, resolve_config_path, save_config
from .parser import auto_price, extract_with_selectors
from .providers import PROVIDERS, preset_for_url
from .scheduler import due_watches, scheduler_loop
from .scraper import Scraper
from .watcher import MIN_INTERVAL_MINUTES, WatchDB, run_watch

CONFIG_PATH = resolve_config_path()
CFG = load_config(CONFIG_PATH)
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


@app.get("/targets")
async def targets_list(x_token: str | None = Header(default=None)):
    _auth(x_token)
    return JSONResponse([
        {"name": t.get("name"), "enabled": bool(t.get("enabled")),
         "url": t.get("url"), "wait_for": t.get("wait_for", ""),
         "selectors": t.get("selectors", {})}
        for t in CFG.get("targets", []) or []
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
         "hinweis": v.get("hinweis", "")}
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
    out = []
    for w in cfg.get("watches", []) or []:
        d = dict(w)
        d["last_run"] = WDB.last_run(w.get("name", ""))
        st = WDB._db.execute("SELECT last_status, last_error FROM watch_state WHERE name=?",
                             (w.get("name", ""),)).fetchone()
        d["last_status"], d["last_error"] = (st or ("nie", ""))
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
        WDB.set_state(name, "ok")
    except Exception as e:
        WDB.set_state(name, "fehler", str(e))
        raise HTTPException(status_code=502, detail=f"Watch fehlgeschlagen: {e}")
    return JSONResponse(result)


@app.get("/deals")
async def deals_list(watch: str | None = None, limit: int = 50,
                     x_token: str | None = Header(default=None)):
    _auth(x_token)
    return JSONResponse(WDB.deals(watch or None, max(1, min(limit, 200))))


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
    _apply_live(cisclean)
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


@app.post("/targets")
async def target_create(req: TargetReq, x_token: str | None = Header(default=None)):
    _auth(x_token)
    name, url = req.name.strip(), req.url.strip()
    if not name or len(name) > 60:
        raise HTTPException(status_code=400, detail="Name fehlt/ungültig")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL muss mit http(s):// beginnen")
    cfg = _fresh_cfg()
    targets = cfg.get("targets", []) or []
    if any(t.get("name") == name for t in targets):
        raise HTTPException(status_code=409, detail="Target existiert bereits")
    targets.append({"name": name, "url": url, "wait_for": req.wait_for.strip(),
                    "selectors": dict(req.selectors or {})})
    cfg["targets"] = targets
    save_config(cfg, CONFIG_PATH)
    return {"ok": True}


@app.delete("/targets/{name}")
async def target_delete(name: str, x_token: str | None = Header(default=None)):
    _auth(x_token)
    cfg = _fresh_cfg()
    cfg["targets"] = [t for t in (cfg.get("targets", []) or []) if t.get("name") != name]
    save_config(cfg, CONFIG_PATH)
    return {"ok": True}


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StealthScraper-LXC</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:1.5em auto;padding:0 1em;background:#111;color:#eee}
.card{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:1em;margin-bottom:1em}
label{display:block;margin:.5em 0 .2em;color:#bbb;font-size:.9em}
input[type=text],input[type=number],textarea,select{width:100%;box-sizing:border-box;background:#0d0d0d;color:#eee;border:1px solid #444;border-radius:6px;padding:.5em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:.5em 1em}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.5em 1em}
button{background:#2e7d32;color:#fff;border:0;border-radius:6px;padding:.6em 1em;margin:.4em .3em .1em 0;cursor:pointer}
button.sec{background:#333}button.warn{background:#6d4c00}button:disabled{opacity:.5}
table{border-collapse:collapse;width:100%;margin-top:.5em}
td,th{border:1px solid #444;padding:.4em .6em;text-align:left;font-size:.9em}
th{background:#222;color:#bbb}pre{background:#0d0d0d;padding:.7em;overflow:auto;border-radius:6px;font-size:.8em}
.ok{color:#7fdc7f}.err{color:#ff8a8a}.mut{color:#999;font-size:.85em}
a{color:#7fdc7f}
.badge{display:inline-block;background:#1b5e20;border-radius:10px;padding:.1em .6em;font-size:.85em}
@media(max-width:700px){.grid,.grid3{grid-template-columns:1fr}}
</style></head><body>
<h1>StealthScraper-LXC</h1>
<p><a href="/">Start</a> &middot; <a href="/settings">Einstellungen</a></p>

<div class="card"><h3>1. Seite pr&uuml;fen</h3>
<label>URL</label>
<input id="url" type="text" placeholder="https://shop.example/produkt/123">
<div class="grid">
<div><label>Preis-Selektor (CSS, optional)</label><input id="s_preis" type="text" placeholder=".price"></div>
<div><label>Verf&uuml;gbarkeit-Selektor (CSS, optional)</label><input id="s_avail" type="text" placeholder=".stock, .availability"></div>
<div><label>Anzahl/Menge-Selektor (CSS, optional)</label><input id="s_qty" type="text" placeholder=".qty, .amount"></div>
<div><label>Titel-Selektor (CSS, optional)</label><input id="s_title" type="text" placeholder="h1"></div>
</div>
<label>Eigene Felder (je Zeile <i>feld=css-selektor</i>, optional)</label>
<textarea id="s_custom" rows="2" placeholder="artikelnummer=.sku&#10;bewertung=.stars"></textarea>
<div class="grid">
<div><label>Warten auf Selektor (wait_for, optional)</label><input id="waitfor" type="text" placeholder=".price"></div>
<div><label>API-Token (nur wenn in config.yaml gesetzt)</label><input id="token" type="text" placeholder="X-Token"></div>
</div>
<label><input id="fresh" type="checkbox"> Cache umgehen (fresh)</label>
<div>
<button id="b_price">Auto-Preis pr&uuml;fen</button>
<button id="b_scrape" class="sec">Mit Selektoren scrapen</button>
</div>
<p class="mut">Auto-Preis braucht keine Selektoren (JSON-LD &rarr; Meta &rarr; Regex).
Selektoren &uuml;berschreiben/erg&auml;nzen die Auto-Erkennung.</p>
</div>

<div class="card"><h3>2. Ergebnis</h3><div id="res"><span class="mut">Noch nichts geprüft.</span></div></div>

<div class="card"><h3>3. Targets &amp; Verlauf</h3>
<div><button id="b_targets" class="sec">Alle aktiven Targets pr&uuml;fen</button>
<button id="b_hist" class="sec">Verlauf laden</button>
<button id="b_cache" class="warn">Cache leeren</button></div>
<div id="targets"></div><div id="hist"></div>
<details><summary>Target anlegen / l&ouml;schen</summary>
<div class="grid">
<div><label>Name</label><input id="t_name" type="text" placeholder="shop-xyz"></div>
<div><label>Warten auf Selektor (optional)</label><input id="t_wait" type="text" placeholder=".price"></div>
</div>
<label>URL</label><input id="t_url" type="text" placeholder="https://shop.example/produkt/1">
<div><button id="b_tsave" class="sec">Target speichern</button>
<button id="b_tlist" class="sec">Targets laden</button></div>
<div id="tlist"></div></details></div>

<div class="card"><h3>4. Schn&auml;ppchen-Watcher</h3>
<p class="mut">Im Browser filtern (Suchbegriff, PLZ + Umkreis, Preis, &bdquo;Neueste zuerst&ldquo;),
die fertige <b>Such-URL kopieren</b> und hier als Watch anlegen. Das System pr&uuml;ft sie
automatisch im eingestellten Abstand, vergleicht jeden Fund mit dem Median und meldet
unterbewertete Treffer als Deal. Gleiches Prinzip f&uuml;r alle Anbieter.</p>
<div class="grid">
<div><label>Name</label><input id="w_name" type="text" placeholder="thinkpad-umkreis"></div>
<div><label>Anbieter</label><select id="w_provider"><option value="auto">Automatisch (per Domain)</option></select></div>
</div>
<label>Such-URL (aus dem Browser kopiert)</label>
<input id="w_url" type="text" placeholder="https://www.kleinanzeigen.de/s-laptop/...">
<div class="grid3">
<div><label>Suchbegriff (Doku)</label><input id="w_query" type="text" placeholder="ThinkPad T14"></div>
<div><label>PLZ (Doku)</label><input id="w_plz" type="text" placeholder="10115"></div>
<div><label>Umkreis km (Doku)</label><input id="w_radius" type="number" value="10" min="0" max="200"></div>
<div><label>Max-Preis &euro; (Filter)</label><input id="w_maxpreis" type="number" placeholder="500" min="0"></div>
<div><label>Deal-Schwelle % unter Median</label><input id="w_schwelle" type="number" value="25" min="1" max="90"></div>
<div><label>Pr&uuml;f-Abstand (Minuten, min. 15)</label><input id="w_interval" type="number" value="60" min="15"></div>
</div>
<label>Webhook f&uuml;r Deal-Meldungen (optional, POST als JSON)</label>
<input id="w_hook" type="text" placeholder="https://ntfy.sh/mein-topic oder Discord-Webhook">
<label><input id="w_enabled" type="checkbox" checked> Aktiviert</label>
<div><button id="b_wsave">Watch speichern</button>
<button id="b_watches" class="sec">Watches laden</button>
<button id="b_deals" class="sec">Deals laden</button></div>
<div id="watches"></div><div id="deals"></div></div>

<div class="card mut">API: <code>POST /scrape</code> &middot; <code>POST /price</code> &middot;
<code>POST /targets/check</code> &middot; <code>GET /watches</code> &middot;
<code>POST /watches</code> &middot; <code>POST /watches/{name}/run</code> &middot;
<code>GET /deals</code> &middot; <code>GET /providers</code> &middot;
<code>GET /health</code> &middot; <code>DELETE /cache</code></div>

<script>
const $=id=>document.getElementById(id);
$('token').value=localStorage.getItem('sr_token')||'';
$('token').onchange=e=>localStorage.setItem('sr_token',e.target.value);
function hdr(){const h={'Content-Type':'application/json'};const t=$('token').value.trim();if(t)h['X-Token']=t;return h;}
function tokQ(){const t=$('token').value.trim();return t?{headers:{'X-Token':t}}:{}}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function busy(b){['b_price','b_scrape','b_targets'].forEach(i=>document.getElementById(i).disabled=b);}
function buildSel(){const s={};const g=(id,n)=>{const v=$(id).value.trim();if(v)s[n]=v;};
g('s_preis','preis');g('s_avail','verfuegbarkeit');g('s_qty','anzahl');g('s_title','titel');
$('s_custom').value.split('\\n').forEach(l=>{const i=l.indexOf('=');if(i>0){const k=l.slice(0,i).trim(),v=l.slice(i+1).trim();if(k&&v)s[k]=v;}});return s;}
function row(k,v,cls){return '<tr><th>'+esc(k)+'</th><td class="'+(cls||'')+'">'+esc(v)+'</td></tr>';}
function render(d){
let h='<table>';
['titel','preis','waehrung','verfuegbarkeit','quelle','status','cached','url'].forEach(k=>{if(d[k]!==undefined&&d[k]!==''&&d[k]!==null)h+=row(k,d[k],k==='preis'?'ok':'');});
const sub=(o,t)=>{if(o&&typeof o==='object')Object.keys(o).forEach(k=>{h+=row(t+': '+k,o[k]);});};
sub(d.felder,'Feld');sub(d.auto,'Auto');
if(d.fehler)h+=row('Fehler',d.fehler,'err');
h+='</table><details><summary>Roh-JSON</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>';
$('res').innerHTML=h;}
async function call(path,body){
const u=$('url').value.trim();if(!u&&body.url!==undefined&&!body.url){$('res').innerHTML='<span class="err">Bitte URL eingeben.</span>';return;}
busy(true);$('res').innerHTML='<span class="mut">L&auml;dt &hellip;</span>';
try{const r=await fetch(path,{method:'POST',headers:hdr(),body:JSON.stringify(body)});
const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);render(d);}
catch(e){$('res').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}busy(false);}
$('b_price').onclick=()=>call('/price',{url:$('url').value.trim(),wait_for:$('waitfor').value.trim(),fresh:$('fresh').checked});
$('b_scrape').onclick=()=>call('/scrape',{url:$('url').value.trim(),selectors:buildSel(),wait_for:$('waitfor').value.trim(),fresh:$('fresh').checked});
$('b_targets').onclick=async()=>{busy(true);$('targets').innerHTML='<span class="mut">Pr&uuml;fe &hellip;</span>';
try{const r=await fetch('/targets/check',{method:'POST',headers:hdr(),body:'{}'});const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);
let h='<table><tr><th>Name</th><th>Preis</th><th>Verf&uuml;gbarkeit</th><th>Status</th></tr>';
d.forEach(t=>{h+='<tr><td>'+esc(t.name)+'</td><td>'+esc(t.preis||t.fehler||'-')+'</td><td>'+esc(t.verfuegbarkeit||'-')+'</td><td>'+(t.ok?'<span class="ok">ok</span>':'<span class="err">fehler</span>')+'</td></tr>';});
h+='</table>';$('targets').innerHTML=h;}catch(e){$('targets').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}busy(false);};
$('b_hist').onclick=async()=>{const o=tokQ();try{const r=await fetch('/results',o);const d=await r.json();
if(!d.length){$('hist').innerHTML='<span class="mut">Keine Verlaufsdaten.</span>';return;}
let h='';d.forEach(f=>{h+='<details><summary>'+esc(f.datei)+' ('+f.eintraege.length+' Eintr&auml;ge)</summary><pre>'+esc(JSON.stringify(f.eintraege,null,2).slice(0,4000))+'</pre></details>';});
$('hist').innerHTML=h;}catch(e){$('hist').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}};
$('b_cache').onclick=async()=>{try{await fetch('/cache',{method:'DELETE',headers:hdr()});alert('Cache geleert');}catch(e){alert('Fehler: '+e.message);}};
$('b_tsave').onclick=async()=>{const b={name:$('t_name').value.trim(),url:$('t_url').value.trim(),wait_for:$('t_wait').value.trim()};
if(!b.name||!b.url){alert('Name und URL ausfüllen');return;}
try{const r=await fetch('/targets',{method:'POST',headers:hdr(),body:JSON.stringify(b)});const d=await r.json();
if(!r.ok)throw new Error(d.detail||r.status);$('t_name').value='';$('t_url').value='';loadTargets();}catch(e){alert('Fehler: '+e.message);}};
async function loadTargets(){try{const r=await fetch('/targets',tokQ());const d=await r.json();
if(!d.length){$('tlist').innerHTML='<p class="mut">Keine Targets.</p>';return;}
let h='<table><tr><th>Name</th><th>URL</th><th>Aktion</th></tr>';
d.forEach(t=>{h+='<tr><td>'+esc(t.name)+'</td><td><span class="mut">'+esc((t.url||'').slice(0,70))+'&hellip;</span></td>'
+'<td><button class="warn" onclick="delTarget(\\''+esc(t.name)+'\\')">L&ouml;schen</button></td></tr>';});
h+='</table>';$('tlist').innerHTML=h;}catch(e){$('tlist').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
async function delTarget(n){if(!confirm('Target \\''+n+'\\' löschen?'))return;
try{await fetch('/targets/'+encodeURIComponent(n),{method:'DELETE',headers:hdr()});loadTargets();}catch(e){alert('Fehler: '+e.message);}}
$('b_tlist').onclick=loadTargets;loadTargets();
async function loadProviders(){try{const r=await fetch('/providers',tokQ());const d=await r.json();
const s=$('w_provider');d.forEach(p=>{const o=document.createElement('option');o.value=p.key;o.textContent=p.label;if(p.key==='generisch')o.textContent+=' (alle anderen Shops)';s.appendChild(o);});}catch(e){}}
function wBody(){const v=id=>$(id).value.trim();
const b={name:v('w_name'),enabled:$('w_enabled').checked,provider:$('w_provider').value,search_url:v('w_url'),
query:v('w_query'),plz:v('w_plz'),radius_km:parseInt(v('w_radius'))||10,
deal_schwelle_prozent:parseFloat(v('w_schwelle'))||25,interval_minutes:parseInt(v('w_interval'))||60,
notify_webhook:v('w_hook')};const mp=v('w_maxpreis');if(mp)b.max_preis=parseFloat(mp);return b;}
$('b_wsave').onclick=async()=>{const b=wBody();if(!b.name||!b.search_url){alert('Name und Such-URL ausfüllen');return;}
try{const r=await fetch('/watches',{method:'POST',headers:hdr(),body:JSON.stringify(b)});const d=await r.json();
if(!r.ok)throw new Error(d.detail||r.status);alert('Watch gespeichert (Prüf-Abstand: '+d.interval_minutes+' Min)');loadWatches();}catch(e){alert('Fehler: '+e.message);}};
async function loadWatches(){try{const r=await fetch('/watches',tokQ());const d=await r.json();
if(!d.length){$('watches').innerHTML='<p class="mut">Noch keine Watches.</p>';return;}
let h='<table><tr><th>Name</th><th>Suche</th><th>Abstand</th><th>Status</th><th>Aktion</th></tr>';
d.forEach(w=>{const lr=w.last_run?new Date(w.last_run*1000).toLocaleString('de-DE'):'nie';
h+='<tr><td>'+esc(w.name)+(w.enabled?'':' (pausiert)')+'</td><td>'+esc((w.query||'')+' '+(w.plz||''))+'<br><span class="mut">'+esc(w.search_url.slice(0,60))+'&hellip;</span></td>'
+'<td>alle '+w.interval_minutes+' Min<br><span class="mut">zuletzt: '+esc(lr)+'</span></td>'
+'<td>'+esc(w.last_status||'-')+'</td>'
+'<td><button class="sec" onclick="runWatch(\\''+esc(w.name)+'\\')">Jetzt pr&uuml;fen</button> '
+'<button class="warn" onclick="delWatch(\\''+esc(w.name)+'\\')">L&ouml;schen</button></td></tr>';});
h+='</table>';$('watches').innerHTML=h;}catch(e){$('watches').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
$('b_watches').onclick=loadWatches;
async function runWatch(n){try{const r=await fetch('/watches/'+encodeURIComponent(n)+'/run',{method:'POST',headers:hdr()});
const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);
alert(d.watch+': '+d.angebote_gesamt+' Angebote, Median '+(d.median??'-')+' €, '+d.deals.length+' Deals');loadWatches();loadDeals();}catch(e){alert('Fehler: '+e.message);}}
async function delWatch(n){if(!confirm('Watch \\''+n+'\\' löschen?'))return;
try{await fetch('/watches/'+encodeURIComponent(n),{method:'DELETE',headers:hdr()});loadWatches();}catch(e){alert('Fehler: '+e.message);}}
async function loadDeals(){try{const r=await fetch('/deals?limit=50',tokQ());const d=await r.json();
if(!d.length){$('deals').innerHTML='<p class="mut">Noch keine Deals gefunden.</p>';return;}
let h='<table><tr><th>Deal</th><th>Preis</th><th>Median</th><th>Grund</th><th>Wann</th></tr>';
d.forEach(t=>{h+='<tr><td><a style="color:#7fdc7f" target="_blank" href="'+esc(t.url)+'">'+esc(t.titel||t.url)+'</a><br><span class="mut">'+esc(t.watch)+'</span></td>'
+'<td>'+esc(t.preis)+' € <span class="badge">-'+esc(t.rabatt)+'%</span></td><td>'+esc(t.median)+' €</td>'
+'<td>'+esc(t.grund)+'</td><td>'+new Date(t.zeit*1000).toLocaleString('de-DE')+'</td></tr>';});
h+='</table>';$('deals').innerHTML=h;}catch(e){$('deals').innerHTML='<span class="err">Fehler: '+esc(e.message)+'</span>';}}
$('b_deals').onclick=loadDeals;
loadProviders();loadWatches();loadDeals();
</script></body></html>"""


SETTINGS_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Einstellungen &middot; StealthScraper-LXC</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:1.5em auto;padding:0 1em;background:#111;color:#eee}
.card{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:1em;margin-bottom:1em}
label{display:block;margin:.5em 0 .2em;color:#bbb;font-size:.9em}
input[type=text],input[type=password],input[type=number],textarea{width:100%;box-sizing:border-box;background:#0d0d0d;color:#eee;border:1px solid #444;border-radius:6px;padding:.5em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:.5em 1em}
button{background:#2e7d32;color:#fff;border:0;border-radius:6px;padding:.6em 1em;margin:.4em .3em .1em 0;cursor:pointer}
button.sec{background:#333}.ok{color:#7fdc7f}.err{color:#ff8a8a}.mut{color:#999;font-size:.85em}
a{color:#7fdc7f}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Einstellungen</h1>
<p><a href="/">Start</a> &middot; <a href="/settings">Einstellungen</a></p>
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
    return DASHBOARD_HTML


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
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"check-{int(time.time())}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse(out)


@app.delete("/cache")
async def cache_clear(x_token: str | None = Header(default=None)):
    _auth(x_token)
    CACHE.clear()
    return {"ok": True}
