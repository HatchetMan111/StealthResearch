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
.badge{display:inline-block;background:#1b5e20;border-radius:10px;padding:.1em .6em;font-size:.85em}
@media(max-width:700px){.grid,.grid3{grid-template-columns:1fr}}
</style></head><body>
<h1>StealthScraper-LXC</h1>

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
<div id="targets"></div><div id="hist"></div></div>

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
