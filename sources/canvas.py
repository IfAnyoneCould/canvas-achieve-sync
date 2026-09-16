"""Canvas LMS source: pulls active-course assignments via the REST API."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Iterator

import requests
from dateutil import parser as dateparser

from common import Item, now_utc

TIMEOUT = 30


class Canvas:
    def __init__(self, base_url: str, token: str, logger: logging.Logger):
        self.base = base_url.rstrip("/")
        self.log = logger
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}"})

    def _paged(self, path: str, **params: Any) -> Iterator[dict]:
        """Walk Canvas' Link-header pagination."""
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        params.setdefault("per_page", 100)
        while url:
            r = self.s.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 401:
                raise SystemExit(
                    "Canvas rejected the token (401). Generate a new access token at "
                    f"{self.base}/profile/settings and update config.json."
                )
            r.raise_for_status()
            body = r.json()
            if isinstance(body, dict):  # some endpoints wrap results
                body = body.get("assignments") or body.get("courses") or []
            yield from body
            url = r.links.get("next", {}).get("url")
            params = {}  # the next URL already carries them

    def courses(self, skip: list[int | str]) -> list[dict]:
        skip_ids = {str(x) for x in skip}
        out = []
        for c in self._paged(
            "courses",
            enrollment_state="active",
            state=["available"],
            include=["term"],
        ):
            if not c.get("name"):
                continue  # unpublished / restricted
            if str(c["id"]) in skip_ids or c["name"] in skip_ids:
                self.log.debug("skipping course %s", c["name"])
                continue
            out.append(c)
        return out


# Canvas course codes arrive SIS-flavored: "GE1501.MERGED.202710",
# "PHIL2390.16190.202710". Strip the section/term noise down to "GE 1501".
_NOISE = re.compile(r"^(merged|sec\d*|\d{4,6}|[a-z]{2}\d{2})$", re.I)
_SUBJECT_NUM = re.compile(r"^([A-Za-z]{2,6})[ _-]?(\d{3,4})([A-Za-z]?)$")


def _clean_code(code: str) -> str:
    parts = [p for p in re.split(r"[.]", code) if p.strip() and not _NOISE.match(p.strip())]
    if not parts:
        return code
    m = _SUBJECT_NUM.match(parts[0].strip())
    if m:
        parts[0] = f"{m.group(1).upper()} {m.group(2)}{m.group(3).upper()}"
    return " ".join(p.strip() for p in parts)


def _short_course_name(course: dict, overrides: dict[str, str]) -> str:
    """Prefer a short 'GE 1501' style label over the long official title."""
    code = (course.get("course_code") or "").strip()
    name = (course.get("name") or "").strip()

    for key in (str(course.get("id")), code, name):
        if key and key in overrides:
            return overrides[key]

    label = _clean_code(code) if code else ""
    if label and len(label) <= 24:
        return label
    return (name or f"course-{course.get('id')}")[:24]


def fetch(cfg: dict, logger: logging.Logger) -> list[Item]:
    ccfg = cfg.get("canvas", {})
    token = (ccfg.get("token") or "").strip()
    if not token or token.startswith("PASTE_"):
        raise SystemExit("No Canvas token configured. See README step 1.")

    api = Canvas(ccfg["base_url"], token, logger)
    window = cfg.get("window", {})
    lo = now_utc() - timedelta(days=window.get("past_days", 3))
    hi = now_utc() + timedelta(days=window.get("future_days", 120))
    include_undated = ccfg.get("include_undated", False)

    items: list[Item] = []
    overrides = {str(k): str(v) for k, v in (ccfg.get("course_names") or {}).items()}
    courses = api.courses(ccfg.get("skip_courses", []))
    logger.info("Canvas: %d active course(s)", len(courses))

    for course in courses:
        cname = _short_course_name(course, overrides)
        try:
            assignments = list(
                api._paged(
                    f"courses/{course['id']}/assignments",
                    include=["submission"],
                    order_by="due_at",
                )
            )
        except requests.HTTPError as e:
            logger.warning("Canvas: could not read assignments for %s (%s)", cname, e)
            continue

        kept = 0
        for a in assignments:
            due_raw = a.get("due_at")
            due = dateparser.isoparse(due_raw) if due_raw else None

            if due is None:
                if not include_undated:
                    continue
            elif not (lo <= due <= hi):
                continue

            sub = a.get("submission") or {}
            completed = bool(
                sub.get("submitted_at")
                or sub.get("workflow_state") in {"graded", "complete"}
                or sub.get("excused")
            )
            # A graded zero-point placeholder still counts as done; a missing
            # submission past its due date does not.

            items.append(
                Item(
                    uid=f"canvas:{a['id']}",
                    source="canvas",
                    course=cname,
                    title=(a.get("name") or "Untitled assignment").strip(),
                    due=due,
                    url=a.get("html_url"),
                    completed=completed,
                    extra={"points": a.get("points_possible")},
                )
            )
            kept += 1
        logger.debug("Canvas: %s -> %d item(s)", cname, kept)

    logger.info("Canvas: %d assignment(s) in window", len(items))
    return items
