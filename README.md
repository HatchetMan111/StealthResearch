# StealthScraper-LXC

Schlanker Preis-/Verfügbarkeits-Scraper für einen Proxmox-LXC. **Kein AI-Zwang:**
Preise werden deterministisch per Parser (JSON-LD → Meta → Selektor → Regex)
extrahiert — halluzinationsfrei, kein Ollama, kein Brave-Budget.

Bewusst als **eigenes Repo / eigener LXC** neben `BraveResearchProxmox`:
Brave = Discovery (*welche* Seiten gibt es?), dieser Scraper = Extraktion
(*was* steht auf Seite X im Detail, auch JS-rendered?).

## Schnellinstallation

**Auf dem Proxmox-Host als root** (2 Cores / 3072 MB / 10 GB, Port 8001):

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/StealthResearch/main/install/stealthscraper-lxc.sh)"
```

Anpassen: `... -- --ctid 102 --hostname StealthResearch --memory 4096` (`--help` für alles).
Update: gleichen Befehl erneut ausführen. Deinstallation: `... -- --uninstall`.

## API

```bash
IP=$(pct exec 102 -- hostname -I | awk '{print $1}')

# Health
curl http://$IP:8001/health

# Auto-Preis (JSON-LD/Meta/Regex, kein Selektor nötig)
curl -X POST http://$IP:8001/price \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/produkt/123"}'
# -> {"preis":"49.99","waehrung":"EUR","verfuegbarkeit":"InStock","quelle":"json-ld",...}

# Eigene Selektoren
curl -X POST http://$IP:8001/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/...","selectors":{"preis":".price","lager":".stock"}}'

# Alle Targets aus config.yaml prüfen (+ JSON-Ablage in data/results/)
curl -X POST http://$IP:8001/targets/check
```

Auth (nur wenn `api_token` gesetzt): Header `X-Token: ...` mitsenden.
Für Zugriff von außen hinter Reverse-Proxy mit Auth oder VPN — wie beim Research-LXC.

## Zusammenspiel mit BraveResearchProxmox

1. Brave findet Kandidaten-URLs (Discovery, günstig im Budget).
2. Nur diese Detail-URLs gehen an `POST /price` (Extraktion, unlimitiert).
3. Optional: Ergebnis zurück an Ollama zur Zusammenfassung.

```python
import requests
S = "http://StealthResearch:8001"
for url in brave_urls:  # aus Research-Report
    r = requests.post(f"{S}/price", json={"url": url}, timeout=60).json()
    print(r["preis"], r["verfuegbarkeit"], url)
```

## Manuell / CLI / Troubleshooting

```bash
cd /opt/stealth-scraper
sudo -u scraper venv/bin/python -m app.main --url https://example.com --price
sudo -u scraper venv/bin/python -m app.main --target all   # alle aktiven Targets
systemctl status stealth-scraper.service
journalctl -u stealth-scraper.service -n 100
rm data/cache.db  # Cache leeren
```

## Voraussetzungen

Debian 12 LXC, 2 Cores, 2–4 GB RAM, 10 GB Disk, `nesting=1`, Netzzugang.
Chromium wird vom Installer via `playwright install --with-deps chromium` geholt.

Nur Seiten scrapen, bei denen das erlaubt ist (`robots.txt`, AGB beachten).
Bei 403/429/Captcha meldet die API HTTP 502 mit `Block erkannt` — dann
Proxy in `config.yaml` eintragen, `extra_wait_ms` erhöhen oder Intervall senken.

## Layout

```
app/server.py    FastAPI (Endpunkte oben)
app/scraper.py   Playwright + Stealth-Grundeinstellungen
app/parser.py    JSON-LD/Meta/Selektor/Regex (ohne KI)
app/cache.py     SQLite-TTL-Cache
app/main.py      CLI + Batch
deploy/          systemd-Units (API + täglicher Check-Timer)
install/         Proxmox-Installer (Host-/Inner-Modus wie Research-LXC)
tests/           Parser-Tests ohne Browser
```

MIT — siehe LICENSE.
