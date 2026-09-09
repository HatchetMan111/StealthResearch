"""Watcher-Tests: Preisparsing, Median, Deal-Erkennung, Pagination (ohne Browser)."""
import sys

sys.path.insert(0, ".")

from app.parser import parse_preis
from app.providers import preset_for_url
from app.watcher import extract_listings, listing_id, median_preis, paginate_urls

HTML = """<html><body><ul>
<li class="ad-listitem"><h2><a href="/s-anzeige/test-123456789.htm">ThinkPad T14</a></h2>
<div class="aditem-main--middle--price-shipping--price">350 €</div>
<div class="aditem-main--top--left">10115 Berlin</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/anderes-987654321.htm">ThinkPad T14s</a></h2>
<div class="aditem-main--middle--price-shipping--price">500 € VB</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/teuer-111222333.htm">ThinkPad X1</a></h2>
<div class="aditem-main--middle--price-shipping--price">1.250 €</div></li>
</ul></body></html>"""


def test_parse_preis():
    assert parse_preis("350 €") == 350.0
    assert parse_preis("1.250 €") == 1250.0
    assert parse_preis("500 € VB") == 500.0
    assert parse_preis("Zu verschenken") == 0.0
    assert parse_preis("Tausch") is None
    assert parse_preis("") is None


def test_extract_and_median():
    from app.providers import PROVIDERS
    ads = extract_listings(HTML, "https://www.kleinanzeigen.de/s-laptop/berlin/c0l3331",
                           PROVIDERS["kleinanzeigen"], {})
    assert len(ads) == 3, ads
    assert ads[0]["titel"] == "ThinkPad T14"
    assert ads[0]["preis"] == 350.0
    assert ads[0]["ext_id"] == "123456789"
    assert ads[0]["ort"] == "10115 Berlin"
    assert median_preis(ads) == 500.0


def test_median_edge():
    assert median_preis([]) is None
    assert median_preis([{"preis": None}]) is None
    assert median_preis([{"preis": 100.0}]) is None  # < 2 Werte
    assert median_preis([{"preis": 0.0}, {"preis": 100.0}, {"preis": 200.0}]) == 150.0


def test_pagination():
    u = "https://www.kleinanzeigen.de/s-laptop/berlin/c0l3331r10"
    pages = paginate_urls(u, 2)
    assert pages[0] == u and "/seite:2/" in pages[1], pages
    g = paginate_urls("https://example-shop.de/suche?q=x", 3)
    assert g[1].endswith("page=2") and g[2].endswith("page=3"), g
    assert paginate_urls(u, 1) == [u]


def test_listing_id_and_preset():
    assert listing_id("https://x.de/s-anzeige/a-123456789.htm") == "123456789"
    assert len(listing_id("https://x.de/ohne-id")) == 12
    key, _ = preset_for_url("https://www.kleinanzeigen.de/s-laptop/x")
    assert key == "kleinanzeigen"
    key, _ = preset_for_url("https://shop.example.de/suche")
    assert key == "generisch"
