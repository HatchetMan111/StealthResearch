"""FastAPI: POST /scrape, POST /price, POST /targets/check, GET /health, Dashboard /."""
from __future__ import annotations

import json
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

RESULTS_DIR = Path((CFG.get("output", {}) or {}).get("results_dir", "data/results"))


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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StealthScraper-LXC</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:1.5em auto;padding:0 1em;background:#111;color:#eee}
.card{background:#1c1c1c;border:1px solid #333;border-radius:10px;padding:1em;margin-bottom:1em}
label{display:block;margin:.5em 0 .2em;color:#bbb;font-size:.9em}
input[type=text],textarea{width:100%;box-sizing:border-box;background:#0d0d0d;color:#eee;border:1px solid #444;border-radius:6px;padding:.5em}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:.5em 1em}
button{background:#2e7d32;color:#fff;border:0;border-radius:6px;padding:.6em 1em;margin:.4em .3em .1em 0;cursor:pointer}
button.sec{background:#333}button.warn{background:#6d4c00}button:disabled{opacity:.5}
table{border-collapse:collapse;width:100%;margin-top:.5em}
td,th{border:1px solid #444;padding:.4em .6em;text-align:left;font-size:.9em}
th{background:#222;color:#bbb}pre{background:#0d0d0d;padding:.7em;overflow:auto;border-radius:6px;font-size:.8em}
.ok{color:#7fdc7f}.err{color:#ff8a8a}.mut{color:#999;font-size:.85em}
@media(max-width:700px){.grid{grid-template-columns:1fr}}
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

<div class="card mut">API: <code>POST /scrape</code> &middot; <code>POST /price</code> &middot;
<code>POST /targets/check</code> &middot; <code>GET /health</code> &middot; <code>DELETE /cache</code></div>

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
