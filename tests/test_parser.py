"""Parser-Tests: JSON-LD, Meta-Fallback, Selektoren (ohne Browser nötig)."""
from app.parser import auto_price, extract_jsonld, extract_with_selectors

HTML_JSONLD = """<html><head><title>Testshop</title>
<script type="application/ld+json">
{"@type":"Product","name":"Winkelschleifer X","offers":{"price":"49.99","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}
</script></head><body></body></html>"""

HTML_META = """<html><head><title>Shop</title>
<meta property="product:price:amount" content="129.00"/></head>
<body><p>Auf Lager, sofort lieferbar</p></body></html>"""


def test_jsonld():
    r = auto_price(HTML_JSONLD)
    assert r["preis"] == "49.99", r
    assert r["verfuegbarkeit"] == "InStock", r
    assert r["quelle"] == "json-ld"


def test_meta_fallback():
    r = auto_price(HTML_META)
    assert r["preis"] == "129.00", r
    assert r["verfuegbarkeit"] == "InStock", r


def test_selectors():
    html = '<div class="price">99,99 €</div><div class="stock">Lieferbar</div>'
    out = extract_with_selectors(html, {"preis": ".price", "lager": ".stock"})
    assert "99,99" in out["preis"]
    assert out["lager"] == "Lieferbar"
    assert extract_jsonld("<html></html>") == {}
