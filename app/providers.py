"""Provider-Presets für den Schnäppchen-Watcher.

Prinzip (für ALLE Anbieter gleich): Du filterst einmal im Browser
(Suchbegriff, PLZ + Umkreis, Preis, Sortierung) und kopierst die fertige
Such-URL in die Watch. Der Watcher blättert die Trefferseiten durch und
extrahiert die Inserate mit den Selektoren unten.

Eigene Selektoren pro Watch überschreiben das Preset (Feld `selectors`).
Neuer Anbieter = neuer Eintrag in PROVIDERS (Name, Domains, Selektoren).
"""
from __future__ import annotations

from urllib.parse import urlparse

PROVIDERS: dict[str, dict] = {
    "kleinanzeigen": {
        "label": "Kleinanzeigen.de",
        "domains": ["kleinanzeigen.de"],
        "hinweis": ("Im Browser filtern (Suchbegriff, Ort/PLZ + Umkreis z.B. 10 km, "
                    "ggf. Preis, Sortierung 'Neueste zuerst') und die URL kopieren. "
                    "Beispiel: https://www.kleinanzeigen.de/s-laptop/12345-ort/preis::500/c...l...r10"),
        # mehrere Kandidaten je Feld: der erste Treffer gewinnt (robust gegen Redesigns)
        "item": ["li.ad-listitem", "article.aditem", "[data-adid]"],
        "titel": ["h2 a", ".aditem-main--middle--title", "a[href*='/s-anzeige/']"],
        "preis": [".aditem-main--middle--price-shipping--price", ".aditem-price", "[class*='price']"],
        "link": ["h2 a", "a[href*='/s-anzeige/']"],
        "ort": [".aditem-main--top--left", "[class*='location']", ".aditem-main--middle--location"],
    },
    "mobile": {
        "label": "Mobile.de",
        "domains": ["mobile.de"],
        "hinweis": "Suche im Browser filtern (PLZ + Umkreis, Preis) und URL kopieren.",
        "item": ["a.result-item", "[data-testid='result-item']", ".cBox-body--resultitem"],
        "titel": ["h3", "[data-testid='result-title']"],
        "preis": ["[data-testid='price']", ".price-block", "[class*='price']"],
        "link": ["a.result-item", "a[href*='/fahrzeuge/details.html']"],
        "ort": ["[data-testid='seller-location']", "[class*='location']"],
    },
    "autoscout": {
        "label": "Autoscout24",
        "domains": ["autoscout24.de"],
        "hinweis": "Suche im Browser filtern (PLZ + Umkreis, Preis) und URL kopieren.",
        "item": ["article.cldt-summary-full-item", "[data-testid='list-item']"],
        "titel": ["h2", "[data-testid='vehicle-title']"],
        "preis": ["[data-testid='regular-price']", ".cldt-price", "[class*='price']"],
        "link": ["a[href*='/angebote/']", "h2 a"],
        "ort": ["[data-testid='seller-location']", "[class*='location']"],
    },
    "ebay": {
        "label": "eBay.de",
        "domains": ["ebay.de"],
        "hinweis": "Suche im Browser filtern und URL kopieren.",
        "item": ["li.s-card", ".s-item", "[data-testid='s-card']"],
        "titel": [".s-card__title", ".s-item__title"],
        "preis": [".s-card__price", ".s-item__price"],
        "link": [".s-card__link", ".s-item__link"],
        "ort": [".s-card__location", ".s-item__location"],
    },
    "idealo": {
        "label": "Idealo (Preisvergleich)",
        "domains": ["idealo.de"],
        "hinweis": "Produktseite oder Suche kopieren. Gut für 'unter Median'-Vergleiche über Händler.",
        "item": [".productList-item", "[data-testid='product-card']", ".offerList-item"],
        "titel": [".productList-title", "[data-testid='product-title']"],
        "preis": [".productList-price", "[data-testid='product-price']", "[class*='price']"],
        "link": ["a.productList-title", "a[href*='/preisvergleich/']"],
        "ort": [],
    },
    "generisch": {
        "label": "Generisch (eigene Selektoren)",
        "domains": [],
        "hinweis": "Für jeden anderen Shop: URL kopieren + Selektoren in der Watch eintragen.",
        "item": ["article", ".product", ".item", "li"],
        "titel": ["h2", "h3", ".title"],
        "preis": [".price"],
        "link": ["a"],
        "ort": [],
    },
}


def preset_for_url(url: str) -> tuple[str, dict]:
    """Preset anhand der Domain wählen, Fallback 'generisch'."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    for key, p in PROVIDERS.items():
        if key == "generisch":
            continue
        if any(d in host for d in p.get("domains", [])):
            return key, p
    return "generisch", PROVIDERS["generisch"]


def select_first(soup, candidates: list[str]):
    """Ersten passenden Selektor zurückgeben (Element), sonst None."""
    for sel in candidates or []:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            return el
    return None
