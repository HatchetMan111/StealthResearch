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

# Schnäppchen-Watch anlegen (läuft danach automatisch im Intervall)
curl -X POST http://$IP:8001/watches \
  -H 'Content-Type: application/json' \
  -d '{"name":"thinkpad-10km","search_url":"https://www.kleinanzeigen.de/s-laptop/...r10",
       "query":"ThinkPad","plz":"10115","radius_km":10,"max_preis":500,
       "deal_schwelle_prozent":25,"interval_minutes":60}'
# Deals abrufen / Watch sofort prüfen
curl "http://$IP:8001/deals?limit=20"
curl -X POST http://$IP:8001/watches/thinkpad-10km/run
```

Auth (nur wenn `api_token` gesetzt): Header `X-Token: ...` mitsenden.
Für Zugriff von außen hinter Reverse-Proxy mit Auth oder VPN — wie beim Research-LXC.

## Weboberfläche (alles einstellbar)

- `/` — **Watcher**: Assistent (Anbieter → Suche öffnen → URL testen → Abstand wählen),
  Jobs-Tabelle, Deals.
- `/scrape` — **Seite prüfen**: URL + optionale CSS-Selektoren (jedes Feld hat ein
  `?` mit Erklärung + Anleitung zum Finden von Selektoren), Ergebnis mit
  **Preis-Kandidaten** (alle Quellen transparent), „Als Job speichern“ mit
  eigenem Prüf-Abstand.
- `/jobs` — **Alle Jobs**: Watches + Targets mit Intervall, letztem/nächstem Lauf,
  Ergebnis, Verlauf (Targets) und Angebots-Unterseite (Watches).
- `/watch/{name}` — **Angebots-Unterseite**: alle Treffer des letzten Laufs,
  auch ohne Deal, zum Nachprüfen. Oben der **Markttrend (Median-Verlauf als
  Diagramm)**, pro Inserat ein Trend-Pfeil (▼ fallend / ▲ steigend / ─ stabil
  ab ±3 %) aus seinem Preisverlauf.
- `/target/{name}` — **Preisverlauf-Seite** für Seite-prüfen-Jobs: Diagramm
  aller bisherigen Läufe + Verlaufstabelle. Der Verlauf wächst mit jedem
  Lauf (Targets zeigen dank gespeichertem Verlauf sofort Kurven).
- `/settings` — Einstellungen: API-Token, Scraper (Timeout, Headless,
  User-Agent, Viewport, Proxy, Bilder blocken), Cache-TTL, Watcher an/aus.
  Speichern wirkt sofort (nur Headless-Umschaltung braucht einen Neustart:
  `systemctl restart stealth-scraper.service`).
- Kein SSH nötig — außer für Updates (Installer erneut laufen lassen).

## Schnäppchen-Watcher (Kleinanzeigen & Co.)

Prinzip — für **alle Anbieter gleich**. Dashboard Sektion 4 führt in 3 Schritten:

1. **Anbieter + Suchbegriff** wählen → „Suche im Browser öffnen“ (vorgefüllt).
2. Dort **PLZ + Umkreis** (z.B. 10 km), Preis und „Neueste zuerst“ wählen,
   **URL kopieren** und einfügen → **„URL testen“** zeigt sofort Treffer +
   Median (nichts wird gespeichert).
3. **Abstand wählen** (Buttons: 15 Min – täglich, Minimum 15 Min Bot-Schutz),
   optional Max-Preis/Webhook → **speichern & einplanen**.

Das System prüft jede Suche automatisch im Intervall, berechnet den **Median**
und meldet Inserate **X % darunter als Deal** (`UNTER_MARKT`) sowie deutliche
**Preissenkungen** (`PREISSENKUNG`). Die **Jobs-Tabelle** zeigt pro Suche:
Intervall, letzter Lauf, **nächster geplanter Lauf**, Ergebnis (Angebote,
Median, Deals) und Status — mit Aktionen (**Jetzt prüfen**, **Angebote**
der letzten Prüfung ansehen, **Bearbeiten**, Pausieren, Löschen).
Bestehende Jobs lassen sich per „Bearbeiten“ ändern (Name bleibt fest),
ohne sie neu anzulegen. Fehler kommen immer als lesbares JSON
(statt „Internal Server Error“).

Vorkonfiguriert: `kleinanzeigen`, `mobile`, `autoscout`, `ebay`, `idealo`,
`generisch` (eigene Selektoren). Neuer Anbieter = 10 Zeilen in
`app/providers.py` (`PROVIDERS`-Eintrag mit Item-/Titel-/Preis-/Link-Selektoren).
Deal-Historie in `data/deals.db`, einsehbar unter `GET /deals` bzw. Dashboard.

Fair use: Intervalle nicht zu aggressiv wählen (mind. 15–30 Min pro Suche),
`robots.txt`/AGB beachten — Kleinanzeigen bannt bei Dauerfeuer schnell die IP,
ggf. Proxy in `config.yaml` eintragen.

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
