"""End-to-End: Watch-CRUD + Preview + Run + Jobs-Tabelle + Deals mit Stub-Scraper."""
import asyncio
import json
import sys

sys.path.insert(0, ".")

HTML = """<html><body><ul>
<li class="ad-listitem"><h2><a href="/s-anzeige/a-111111111.htm">ThinkPad T14</a></h2>
<div class="aditem-main--middle--price-shipping--price">350 €</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/b-222222222.htm">ThinkPad T14s</a></h2>
<div class="aditem-main--middle--price-shipping--price">500 €</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/c-333333333.htm">ThinkPad T14 G2</a></h2>
<div class="aditem-main--middle--price-shipping--price">480 €</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/d-444444444.htm">ThinkPad X1</a></h2>
<div class="aditem-main--middle--price-shipping--price">900 €</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/e-555555555.htm">ThinkPad T15</a></h2>
<div class="aditem-main--middle--price-shipping--price">520 €</div></li>
<li class="ad-listitem"><h2><a href="/s-anzeige/f-666666666.htm">ThinkPad P14s</a></h2>
<div class="aditem-main--middle--price-shipping--price">90 €</div></li>
</ul></body></html>"""


class StubScraper:
    async def fetch(self, url, wait_for=""):
        return {"url": url, "html": HTML, "title": "Suche", "status": 200}


def run():
    import tempfile
    from pathlib import Path
    import app.server as s
    from app.server import PreviewReq, WatchReq
    from app.watcher import WatchDB

    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text("watcher:\n  db_path: '" + str(tmp / "deals.db") + "'\n", encoding="utf-8")
    s.CONFIG_PATH = cfg_path
    s.WDB = WatchDB(str(tmp / "deals.db"))
    s.SCRAPER = StubScraper()
    from app.cache import TTLCache
    s.CACHE = TTLCache(":memory:", 6)

    def j(resp):
        return resp if isinstance(resp, dict) else json.loads(resp.body)

    async def flow():
        # 1. anlegen
        w = await s.watch_create(WatchReq(name="thinkpad", search_url="https://www.kleinanzeigen.de/s-laptop/x",
                                          query="ThinkPad", plz="10115", radius_km=10,
                                          interval_minutes=30), x_token=None)
        assert j(w)["name"] == "thinkpad" and j(w)["interval_minutes"] == 30, j(w)
        # 2. duplikat -> 409
        try:
            await s.watch_create(WatchReq(name="thinkpad", search_url="https://x.de/"), x_token=None)
            raise SystemExit("409 fehlt!")
        except Exception as e:
            assert getattr(e, "status_code", None) == 409, e
        # 3. vorschau (trocken, keine deals)
        p = await s.watch_preview(PreviewReq(search_url="https://www.kleinanzeigen.de/s-laptop/x"), x_token=None)
        assert j(p)["angebote_gesamt"] == 6 and j(p)["median"] == 490.0, j(p)
        assert len(j(p)["beispiele"]) == 5
        assert s.WDB.deals() == []
        # 4. lauf -> 90€ bei Median 500 = UNTER_MARKT-deal
        r = await s.watch_run("thinkpad", x_token=None)
        assert j(r)["median"] == 490.0, j(r)
        assert len(j(r)["deals"]) == 2 and 90.0 in [d["preis"] for d in j(r)["deals"]], j(r)
        # 5. jobs-tabelle: summary + next_run
        wl = await s.watches_list(x_token=None)
        job = j(wl)[0]
        assert job["summary"]["angebote"] == 6 and job["summary"]["deals"] == 2, job
        assert job["next_run"] == job["last_run"] + 1800 and job["due"] is False, job
        # 6. deals + filter
        dl = await s.deals_list(watch=None, limit=50, x_token=None)
        assert len(j(dl)) == 2 and all(d["grund"] == "UNTER_MARKT" for d in j(dl)), j(dl)
        dl2 = await s.deals_list(watch="thinkpad", limit=50, x_token=None)
        assert len(j(dl2)) == 2
        # 7. zweiter lauf -> kein doppel-deal (history)
        r2 = await s.watch_run("thinkpad", x_token=None)
        assert j(r2)["deals"] == [], j(r2)
        # 8. pausieren -> next_run None
        await s.watch_update("thinkpad", WatchReq(name="thinkpad", search_url="https://www.kleinanzeigen.de/s-laptop/x",
                                                  enabled=False), x_token=None)
        wl2 = await s.watches_list(x_token=None)
        assert j(wl2)[0]["next_run"] is None and j(wl2)[0]["enabled"] is False
        # 8b. bearbeiten (PUT): intervall + query aendern
        up = await s.watch_update("thinkpad", WatchReq(name="thinkpad", search_url="https://www.kleinanzeigen.de/s-laptop/x",
                                                       query="ThinkPad X", interval_minutes=120,
                                                       enabled=True), x_token=None)
        assert j(up)["query"] == "ThinkPad X" and j(up)["interval_minutes"] == 120, j(up)
        # 8c. angebote-snapshot vom lauf
        ang = await s.watch_angebote("thinkpad", limit=100, x_token=None)
        assert len(j(ang)) == 6 and j(ang)[0]["url"].startswith("https://"), j(ang)
        # 8d. 500er kommen als JSON (kein HTML)
        err = await s._json_500(None, ValueError("boom"))
        assert err.status_code == 500 and "boom" in j(err)["detail"], j(err)
        # 9. settings roundtrip
        sd = await s.settings_data(x_token=None)
        assert "scraper" in j(sd) and "watcher" in j(sd)
        sv = await s.settings_save({"scraper": {"timeout_seconds": 45}, "cache": {"ttl_hours": 2},
                                    "watcher": {"enabled": True}}, x_token=None)
        assert j(sv)["ok"] is True and j(sv)["restart_needed"] is False
        assert s.SCRAPER.timeout == 45000  # _apply_live wirkt sofort
        import yaml
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert saved["scraper"]["timeout_seconds"] == 45 and saved["cache"]["ttl_hours"] == 2
        # 10. target crud
        from app.server import TargetReq
        await s.target_create(TargetReq(name="t1", url="https://shop.example/1"), x_token=None)
        tl = await s.targets_list(x_token=None)
        assert len(j(tl)) == 1
        await s.target_delete("t1", x_token=None)
        # 10b. target mit cron-intervall: jobs-tabelle + scheduler + verlauf
        await s.target_create(TargetReq(name="cronjob", url="https://shop.example/1",
                                        interval_minutes=30), x_token=None)
        jb = await s.jobs_list(x_token=None)
        tj = [x for x in j(jb)["targets"] if x["name"] == "cronjob"][0]
        assert tj["intervall"] == "alle 30 Min" and tj["next_run"] is not None, tj
        from app.scheduler import due_targets
        assert due_targets({"targets": [{"name": "cronjob", "url": "https://shop.example/1",
                                          "interval_minutes": 30, "enabled": True}]}, s.WDB) != []
        tr = await s.target_run("cronjob", x_token=None)
        assert j(tr)["ok"] is True and j(tr)["preis"] == "350.00", j(tr)
        runs = await s.target_runs("cronjob", limit=5, x_token=None)
        assert len(j(runs)) == 1 and j(runs)[0]["ok"] is True, j(runs)
        await s.target_delete("cronjob", x_token=None)
        # 10c. watch-unterseite mit snapshot + trend
        await s.watch_create(WatchReq(name="w2", search_url="https://www.kleinanzeigen.de/s-laptop/x"),
                             x_token=None)
        await s.watch_run("w2", x_token=None)
        page = await s.watch_page("w2")
        assert "ThinkPad" in page and "/jobs" in page, page[:200]
        s.WDB.note_price("kleinanzeigen", "222222222", 400.0, 1000)
        s.WDB.add_watch_stat("w2", 600.0, 6, 0, now=1000)
        page2 = await s.watch_page("w2")
        assert "<svg" in page2 and "Trend" in page2, page2[:300]
        await s.watch_delete("w2", x_token=None)
        # 10d. target-trendseite (nutzt vorhandenen verlauf)
        await s.target_create(TargetReq(name="ttrend", url="https://shop.example/1"),
                              x_token=None)
        await s.target_run("ttrend", x_token=None)
        await s.target_run("ttrend", x_token=None)
        tpage = await s.target_page("ttrend")
        assert "<svg" in tpage and "Preisverlauf" in tpage, tpage[:300]
        await s.target_delete("ttrend", x_token=None)
        try:
            await s.watch_page("weg")
            raise SystemExit("404 fehlt!")
        except Exception as e:
            assert getattr(e, "status_code", None) == 404, e
        # 11. watch löschen
        await s.watch_delete("thinkpad", x_token=None)
        assert j(await s.watches_list(x_token=None)) == []
        # 12. Regression: 2x derselbe Scrape (2. aus Cache) -> kein KeyError 'title'
        from app.server import ScrapeReq
        r1 = await s.scrape(ScrapeReq(url="https://shop.example/p/1",
                                      selectors={"preis": ".aditem-main--middle--price-shipping--price"}),
                            x_token=None)
        assert j(r1)["cached"] is False and "felder" in j(r1), j(r1)
        r2 = await s.scrape(ScrapeReq(url="https://shop.example/p/1",
                                      selectors={"preis": ".aditem-main--middle--price-shipping--price"}),
                            x_token=None)
        assert j(r2)["cached"] is True and j(r2)["felder"]["preis"] != "", j(r2)
        # 13. alter Cache-Eintrag (ohne html/title) wird ignoriert statt zu crashen
        s.CACHE.set("https://shop.example/alt", "[]", {"url": "x", "titel": "Alt"})
        r3 = await s.scrape(ScrapeReq(url="https://shop.example/alt"), x_token=None)
        assert j(r3)["cached"] is False, j(r3)
        print("E2E_OK")

    asyncio.run(flow())


if __name__ == "__main__":
    run()
