"""Shared helpers: config, state, normalized item shape, logging."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "sync.log"


@dataclass
class Item:
    """One piece of coursework, normalized across sources."""

    uid: str
    source: str
    course: str
    title: str
    due: datetime | None = None
    url: str | None = None
    completed: bool = False
    # Further places the same work can be found (a podcast on both Apple and
    # Spotify, a film plus its transcript). `url` stays the one to click first.
    links: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        data: dict[str, Any] = {
            "course": self.course,
            "title": self.title,
            "due": self.due.isoformat() if self.due else None,
            "url": self.url,
            "completed": self.completed,
        }
        # Only fold in `links` when there are some: adding the key unconditionally
        # would change every stored fingerprint and re-patch the whole list once,
        # for items whose task body did not actually change.
        if self.links:
            data["links"] = self.links
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("sync")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        fileh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except OSError:
        pass  # read-only dir; console logging is enough

    return logger


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Missing {CONFIG_PATH}.\n"
            "Copy config.example.json to config.json and fill in your tokens."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    # Env vars win over the file, so secrets can stay out of it if you prefer.
    if os.environ.get("CANVAS_TOKEN"):
        cfg.setdefault("canvas", {})["token"] = os.environ["CANVAS_TOKEN"]
    return cfg


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
