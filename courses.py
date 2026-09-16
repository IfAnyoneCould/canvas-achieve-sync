"""One place that decides which class a thing belongs to.

Both the calendar colourer and the task titles need the same answer for the
same string - "Dr. Murga Office Hours" and "CHEM 1151 HW 3 due" must land the
same way every run - so the matching lives here rather than in either caller.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_KEY = "_default"


def load(cfg: dict) -> dict[str, dict[str, Any]]:
    """The configured classes, with comment keys dropped but order kept."""
    return {
        k: v
        for k, v in (cfg.get("courses") or {}).items()
        if not k.startswith("_comment") and isinstance(v, dict)
    }


def identify(text: str, courses: dict[str, dict[str, Any]]) -> str:
    """Which class `text` belongs to; DEFAULT_KEY when nothing claims it.

    An exact course label wins over any pattern, so a task already labelled
    "CHEM 1151" never depends on regex order. Otherwise the first `match` that
    fires wins, which is why office hours are configured first.
    """
    text = (text or "").strip()
    if not text:
        return DEFAULT_KEY

    for key in courses:
        if key != DEFAULT_KEY and key.lower() == text.lower():
            return key

    for key, spec in courses.items():
        pattern = spec.get("match")
        if key == DEFAULT_KEY or not pattern:
            continue
        try:
            if re.search(pattern, text, re.I):
                return key
        except re.error:
            continue
    return DEFAULT_KEY


def marker(text: str, courses: dict[str, dict[str, Any]]) -> str:
    key = identify(text, courses)
    spec = courses.get(key) or courses.get(DEFAULT_KEY) or {}
    return str(spec.get("marker") or "")


def color(text: str, courses: dict[str, dict[str, Any]]) -> str | None:
    key = identify(text, courses)
    spec = courses.get(key) or courses.get(DEFAULT_KEY) or {}
    value = spec.get("calendar_color")
    return str(value) if value else None
