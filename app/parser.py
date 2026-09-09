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


def auto_price(html: str) -> dict:
    """Gibt {name, preis, waehrung, verfuegbarkeit, quelle} zurück."""
    soup = BeautifulSoup(html, "lxml")
    jl = extract_jsonld(html)
    if jl.get("preis"):
        avail = str(jl.get("verfuegbarkeit", ""))
        return {
            "name": jl.get("name") or (soup.title.string.strip() if soup.title and soup.title.string else ""),
            "preis": str(jl["preis"]).replace(",", "."),
            "waehrung": jl.get("waehrung") or "EUR",
            "verfuegbarkeit": "InStock" if "InStock" in avail else ("OutOfStock" if "OutOfStock" in avail else avail),
            "quelle": "json-ld",
        }
    price = _meta(soup, "product:price:amount", "og:price:amount", "price")
    if not price:
        el = soup.find(attrs={"itemprop": "price"})
        if el:
            price = el.get("content", "") or el.get_text(" ", strip=True)
    quelle = "meta" if price else "regex"
    if not price:
        text = soup.get_text(" ", strip=True)[:20000]
        m = PRICE_RE.search(text)
        price = m.group(1) if m else ""
    avail_text = soup.get_text(" ", strip=True).lower()[:20000]
    if any(n in avail_text for n in STOCK_NEGATIVE):
        avail = "OutOfStock"
    elif any(p in avail_text for p in STOCK_POSITIVE):
        avail = "InStock"
    else:
        avail = ""
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    name = _meta(soup, "og:title") or title
    if price:
        price = price.replace("€", "").strip().replace(".", "").replace(",", ".") if "," in price else price.strip()
    return {"name": name, "preis": price, "waehrung": "EUR", "verfuegbarkeit": avail, "quelle": quelle}


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
