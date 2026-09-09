"""YAML-Config mit Env-Overrides (SCRAPER_TOKEN, SCRAPER_PORT)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULTS: dict = {}


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    cfg: dict = {}
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    # Env-Overrides für sensible / Deployment-Werte
    token = os.environ.get("SCRAPER_TOKEN")
    if token:
        cfg.setdefault("server", {})["api_token"] = token
    port = os.environ.get("SCRAPER_PORT")
    if port and str(port).isdigit():
        cfg.setdefault("server", {})["port"] = int(port)
    return cfg
