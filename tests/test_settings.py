"""Settings- und Watch-Validierung (ohne Browser, ohne Server)."""
import sys

sys.path.insert(0, ".")

from fastapi import HTTPException

from app.server import _validate_watch, apply_settings


def test_settings_clamps():
    c = apply_settings({
        "server": {"api_token": "x" * 500},
        "scraper": {"timeout_seconds": 999, "extra_wait_ms": -5, "viewport_width": 100,
                    "locale": "", "proxy": "http://p:8080"},
        "cache": {"ttl_hours": -3},
        "watcher": {},
    })
    assert len(c["server"]["api_token"]) == 200
    assert c["scraper"]["timeout_seconds"] == 120
    assert c["scraper"]["extra_wait_ms"] == 0
    assert c["scraper"]["viewport_width"] == 800
    assert c["scraper"]["locale"] == "de-DE"
    assert c["cache"]["ttl_hours"] == 0
    assert c["watcher"]["enabled"] is True


def test_settings_defaults_empty():
    c = apply_settings({})
    assert c["scraper"]["timeout_seconds"] == 30
    assert c["scraper"]["headless"] is True
    assert c["cache"]["ttl_hours"] == 6


def test_watch_validation():
    w = _validate_watch({"name": "  test  ", "search_url": "https://www.kleinanzeigen.de/s-x/y",
                         "provider": "auto", "interval_minutes": 5, "radius_km": 999})
    assert w["name"] == "test"
    assert w["provider"] == "kleinanzeigen"  # auto-erkannt
    assert w["interval_minutes"] == 15  # Minimum (Bot-Schutz)
    assert w["radius_km"] == 200


def _raises(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except HTTPException:
        return True
    return False


def test_watch_bad_url():
    assert _raises(_validate_watch, {"name": "x", "search_url": "ftp://falsch"})
    assert _raises(_validate_watch, {"name": "", "search_url": "https://ok.de/"})
    assert _raises(_validate_watch, {"name": "x", "search_url": "https://ok.de/",
                                     "provider": "gibtsnicht"})
