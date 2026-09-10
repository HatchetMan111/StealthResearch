"""Deterministischer Parser: KEINE KI für Preise (halluzinationsfrei).

Priorität: 1) JSON-LD Product/Offer  2) Meta/OpenGraph  3) Microdata
4) Custom-Selektoren  5) Regex-Fallback (€-Muster).
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*,\d{2}|\d+\.\d{2})\s*€?")
STOCK_POSITIVE = ("auf lager", "in stock", "lieferbar", "sofort", "verfügbar", "available")
STOCK_NEGATIVE = ("ausverkauft", "nicht verfügbar", "out of stock", "vergriffen")

# Generische sichtbare Preis-Elemente (Fallback, wenn JSON-LD fehlt/unstimmig).
# Exakte Klassen zuerst, Substring-Treffer zuletzt (können Container mit viel Text sein).
GENERIC_PRICE_SELECTORS = [
    ".price", ".product-price", ".price-current", ".current-price", ".sale-price",
    ".regular-price", ".offer-price", ".preis", ".product__price", ".amount",
    "#price", "#product-price", "[itemprop='price']",
    "[class*='preis']", "[class*='price']",
]


def _norm_preis(roh: str) -> str:
    p = parse_preis(roh)
    return f"{p:.2f}" if p is not None else ""


def extract_jsonld(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", ""))
            graph = node.get("@graph")
            candidates = graph if isinstance(graph, list) and graph else [node]
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                if c.get("@type") in ("Product", "Offer", "AggregateOffer") or t == "Product":
                    offer = c.get("offers", {})
                    if isinstance(offer, list):
                        offer = offer[0] if offer else {}
                    if isinstance(offer, dict):
                        return {
                            "name": c.get("name"),
                            "preis": offer.get("price") or c.get("price"),
                            "waehrung": offer.get("priceCurrency") or c.get("priceCurrency"),
                            "verfuegbarkeit": offer.get("availability", ""),
                        }
    return {}


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for n in names:
        tag = soup.find("meta", {"property": n}) or soup.find("meta", {"name": n}) or soup.find("meta", {"itemprop": n})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def jsonld_offers(html: str) -> list[dict]:
    """Alle JSON-LD-Angebote einsammeln (statt nur dem ersten)."""
    out: list[dict] = []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads((tag.get_text() or "").strip())
        except Exception:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", ""))
            graph = node.get("@graph")
            cands = graph if isinstance(graph, list) and graph else [node]
            for c in cands:
                if not isinstance(c, dict):
                    continue
                if c.get("@type") not in ("Product", "Offer", "AggregateOffer") and t != "Product":
                    continue
                offers = c.get("offers", [])
                offers = offers if isinstance(offers, list) else [offers]
                for o in offers:
                    if not isinstance(o, dict):
                        continue
                    roh = o.get("price", o.get("lowPrice", ""))
                    p = parse_preis(str(roh)) if roh not in (None, "") else None
                    if p is None:
                        continue
                    out.append({
                        "preis": f"{p:.2f}",
                        "waehrung": o.get("priceCurrency") or c.get("priceCurrency") or "EUR",
                        "verfuegbarkeit": str(o.get("availability", "")),
                        "name": c.get("name") or "",
                    })
    return out


def _avail_status(avail_raw: str, soup_text: str) -> str:
    if "InStock" in avail_raw:
        return "InStock"
    if "OutOfStock" in avail_raw:
        return "OutOfStock"
    tl = soup_text.lower()
    if any(n in tl for n in STOCK_NEGATIVE):
        return "OutOfStock"
    if any(p in tl for p in STOCK_POSITIVE):
        return "InStock"
    return ""


def auto_price(html: str) -> dict:
    """Gibt {name, preis, waehrung, verfuegbarkeit, quelle, kandidaten} zurück.

    Sammelt ALLE Preisquellen (JSON-LD alle Offers, Meta, Microdata, sichtbare
    Preis-Elemente, Regex) und wählt: InStock-JSON-LD > JSON-LD > Meta >
    Microdata > sichtbar > Regex. `kandidaten` macht die Wahl nachprüfbar.
    """
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    text = soup.get_text(" ", strip=True)[:20000]
    kands: list[dict] = []

    for o in jsonld_offers(html):
        kands.append({"wert": o["preis"], "quelle": "json-ld",
                      "detail": ("lagernd" if "InStock" in o["verfuegbarkeit"] else
                                 "vergriffen" if "OutOfStock" in o["verfuegbarkeit"] else ""),
                      "lager": ("InStock" if "InStock" in o["verfuegbarkeit"] else
                                "OutOfStock" if "OutOfStock" in o["verfuegbarkeit"] else "")})

    meta = _meta(soup, "product:price:amount", "og:price:amount", "price")
    if meta and _norm_preis(meta):
        kands.append({"wert": _norm_preis(meta), "quelle": "meta", "detail": "", "lager": ""})

    micro = soup.find(attrs={"itemprop": "price"})
    if micro is not None:
        mraw = micro.get("content", "") or micro.get_text(" ", strip=True)
        if mraw and _norm_preis(mraw):
            kands.append({"wert": _norm_preis(mraw), "quelle": "microdata", "detail": "", "lager": ""})

    for sel in GENERIC_PRICE_SELECTORS:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            etext = el.get_text(" ", strip=True)[:80]
            if etext and _norm_preis(etext) and not any(k["wert"] == _norm_preis(etext) for k in kands):
                kands.append({"wert": _norm_preis(etext), "quelle": f"sichtbar ({sel})",
                              "detail": etext[:60], "lager": ""})

    if not kands:
        m = PRICE_RE.search(text)
        if m and _norm_preis(m.group(1)):
            kands.append({"wert": _norm_preis(m.group(1)), "quelle": "regex", "detail": "", "lager": ""})
    if not kands:
        # Ganze Euro-Beträge ohne Cent ("350 €") fängt PRICE_RE nicht ab
        m = re.search(r"(\d[\d\s.]*)\s*€", text)
        if m and _norm_preis(m.group(1)):
            kands.append({"wert": _norm_preis(m.group(1)), "quelle": "regex",
                          "detail": m.group(0).strip()[:40], "lager": ""})

    jl_stock = [k for k in kands if k["quelle"] == "json-ld" and k["detail"] == "lagernd"]
    jl_any = [k for k in kands if k["quelle"] == "json-ld"]
    first = (jl_stock or jl_any or [k for k in kands if k["quelle"] == "meta"] or
             [k for k in kands if k["quelle"] == "microdata"] or
             [k for k in kands if k["quelle"].startswith("sichtbar")] or kands)
    best = first[0] if first else {"wert": "", "quelle": "keine", "detail": "", "lager": ""}
    name = _meta(soup, "og:title") or title
    return {"name": name, "preis": best["wert"], "waehrung": "EUR",
            "verfuegbarkeit": best.get("lager") or _avail_status("", text),
            "quelle": best["quelle"], "kandidaten": kands}


def extract_with_selectors(html: str, selectors: dict[str, str]) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, str] = {}
    for field, sel in (selectors or {}).items():
        try:
            el = soup.select_one(sel)
        except Exception:
            el = None
        out[field] = el.get_text(" ", strip=True) if el else ""
    return out


def parse_preis(text: str) -> float | None:
    """Deutschen Preis-String -> float (EUR). 'VB'/'Tausch'/leer -> None, 'verschenken' -> 0.0."""
    if not text:
        return None
    t = text.strip().lower()
    if any(w in t for w in ("verschenk", "gratis", "kostenlos")):
        return 0.0
    if any(w in t for w in ("vb", "tausch", "anfrage", "preisvorschlag")) and not re.search(r"\d", t):
        return None
    m = re.search(r"(\d[\d\s.]*)[,.](\d{2})\b", t)
    if m:
        try:
            return float(re.sub(r"[\s.]", "", m.group(1)) + "." + m.group(2))
        except ValueError:
            return None
    m = re.search(r"\b(\d[\d\s.]*)\s*(€|eur)", t)
    if m:
        try:
            return float(re.sub(r"[\s.]", "", m.group(1)))
        except ValueError:
            return None
    return None
