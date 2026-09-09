"""Playwright-Chromium mit Stealth-Grundeinstellungen (ohne Extra-Dependency).

Tarnung: echtes UA + Viewport + Locale/Timezone, AutomationControlled-Flag
aus, webdriver-Hook überschrieben, menschliches Scrollen, optionales
Bild-Blocking + Proxy aus config.yaml.
Singleton-Browser, ein Context pro Request (saubere Fingerprints).
"""
from __future__ import annotations

from typing import Any

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-sandbox",  # nötig im unprivilegierten LXC
    "--disable-dev-shm-usage",
]

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""


class Scraper:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        s = cfg.get("scraper", {})
        self.timeout = int(s.get("timeout_seconds", 30)) * 1000
        self.headless = bool(s.get("headless", True))
        self.extra_wait = int(s.get("extra_wait_ms", 1500))
        self._pw = None
        self._browser = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        if self._browser:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, args=STEALTH_ARGS
        )

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._browser = None
            self._pw = None

    async def fetch(self, url: str, wait_for: str = "") -> dict[str, Any]:
        """Gibt {url, html, title, status} zurück. Wirft bei Timeout/Block."""
        await self.start()
        assert self._browser is not None
        s = self.cfg.get("scraper", {})
        proxy = s.get("proxy") or None
        context_kwargs: dict[str, Any] = {
            "user_agent": s.get("user_agent"),
            "viewport": {
                "width": int(s.get("viewport_width", 1366)),
                "height": int(s.get("viewport_height", 768)),
            },
            "locale": s.get("locale", "de-DE"),
            "timezone_id": s.get("timezone", "Europe/Berlin"),
            "ignore_https_errors": True,
        }
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}
        ctx = await self._browser.new_context(**context_kwargs)
        await ctx.add_init_script(STEALTH_INIT_SCRIPT)
        # Bilder blocken = schneller + weniger Traffic (abschaltbar)
        if s.get("block_images", True):
            await ctx.route("**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2}", lambda r: r.abort())
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            status = resp.status if resp else 0
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=min(self.timeout, 15000))
                except Exception:
                    pass
            # kurz scrollen -> triggert Lazy-Load-Preise
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
                await page.wait_for_timeout(min(self.extra_wait, 5000))
            except Exception:
                pass
            html = await page.content()
            title = await page.title()
            if status in (403, 429) or "captcha" in (title or "").lower():
                raise RuntimeError(f"Block erkannt (HTTP {status}, Titel: {title!r})")
            return {"url": url, "html": html, "title": title, "status": status}
        finally:
            await ctx.close()
