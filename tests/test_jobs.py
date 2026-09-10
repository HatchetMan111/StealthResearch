"""Jobs-Tabelle + Assistent-Helfer (ohne Browser, ohne Server)."""
import sys

sys.path.insert(0, ".")

from app.providers import quick_search_url, slugify
from app.scheduler import next_run_ts
from app.watcher import WatchDB


def test_slugify():
    assert slugify("ThinkPad T14") == "thinkpad-t14"
    assert slugify("Müller Fahrräder!!") == "mueller-fahrraeder"
    assert slugify("") == ""


def test_quick_search_url():
    u = quick_search_url("kleinanzeigen", "ThinkPad T14")
    assert u == "https://www.kleinanzeigen.de/s-thinkpad-t14/k0", u
    u = quick_search_url("ebay", "lego technic")
    assert "ebay.de" in u and "lego+technic" in u, u
    assert quick_search_url("mobile", "Golf") == ""  # manuell nötig
    assert quick_search_url("kleinanzeigen", "") == ""
    assert quick_search_url("gibtsnicht", "x") == ""


def test_next_run():
    w = {"interval_minutes": 60}
    assert next_run_ts(w, 1000, now=2000) == 1000 + 3600
    # überfällig -> jetzt fällig
    assert next_run_ts(w, 1000, now=9000) == 9000
    # Minimum 15 Min wird erzwungen
    w2 = {"interval_minutes": 1}
    assert next_run_ts(w2, 0, now=0) == 900


def test_state_summary_roundtrip():
    db = WatchDB(":memory:")
    st = db.get_state("neu")
    assert st["last_status"] == "nie" and st["last_run"] == 0
    db.set_state("job1", "ok", "", {"angebote": 42, "deals": 3, "median": 450.5, "mit_preis": 40})
    st = db.get_state("job1")
    assert st["last_status"] == "ok"
    assert st["last_angebote"] == 42 and st["last_deals"] == 3
    assert st["last_mitpreis"] == 40
    assert st["last_median"] == 450.5
    assert db.last_run("job1") > 0
