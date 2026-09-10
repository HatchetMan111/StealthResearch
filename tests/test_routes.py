"""Routen-Guard: keine Seite darf von einer API-Route verdeckt werden."""
import sys

sys.path.insert(0, ".")


def test_no_shadowed_routes():
    import app.server as s
    from collections import Counter
    c = Counter()
    for r in s.app.routes:
        p, m = getattr(r, "path", None), tuple(sorted(getattr(r, "methods", None) or []))
        if p:
            c[(p, m)] += 1
    dups = [k for k, v in c.items() if v > 1]
    assert not dups, f"routen-kollision (seite vs. api): {dups}"
    paths = {getattr(r, "path", None) for r in s.app.routes}
    for must in ("/", "/scrape", "/jobs", "/watch/{name}", "/settings",
                 "/api/jobs", "/watches", "/deals", "/health"):
        assert must in paths, f"route fehlt: {must}"
