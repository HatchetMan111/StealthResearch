"""YAML-Config mit Env-Overrides (SCRAPER_TOKEN, SCRAPER_PORT)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DEFAULTS: dict = {}


def resolve_config_path() -> Path:
    """config.yaml neben app/ bevorzugen, sonst CWD (identisch zu server.py)."""
    here = Path(__file__).resolve().parent.parent / "config.yaml"
    return here if here.exists() else Path("config.yaml")


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


def save_config(cfg: dict, path: str | Path | None = None) -> None:
    """Schreibt cfg als YAML zurück (für Dashboard-CRUD: watches)."""
    p = Path(path) if path else resolve_config_path()
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
