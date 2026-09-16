"""Google Calendar source: turns scheduled work (e.g. readings) into todos.

Only events matching `include_pattern` are imported, because a calendar also
holds classes, work shifts and personal life - none of which belong on a
coursework list. Recurring events are expanded to individual instances, so a
weekly reading becomes one task per week.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from googleapiclient.errors import HttpError

import gauth
from common import ROOT, Item, now_utc

_SUBJECT_NUM = re.compile(r"\b([A-Za-z]{2,6})[ _-]?(\d{3,4})\b")
# A course code can only appear in the lead of a title; everything past a dash,
# colon or bracket is a subtitle or citation, where "(Harper's, July 2024)"
# would otherwise be read as the course "JULY 2024".
_LEAD = re.compile(r"[—–:(\[]")


def _course_from_title(title: str, fallback: str) -> str:
    """'PHIL 2390 reading: Ch 4' -> 'PHIL 2390'; otherwise the configured label."""
    m = _SUBJECT_NUM.search(_LEAD.split(title, 1)[0])
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    return fallback


def _load_link_map(
    filename: str | None, logger: logging.Logger
) -> list[tuple[re.Pattern[str], list[str]]]:
    """Read the title-pattern -> reading-URL file named in config, if any.

    A missing or broken file only costs the links, never the sync, so it warns
    and carries on with plain tasks.
    """
    if not filename:
        return []

    path = ROOT / filename
    if not path.exists():
        logger.warning("Calendar: link file %s not found; tasks get no reading links", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Calendar: cannot read %s (%s); tasks get no reading links", path, e)
        return []

    mapping: list[tuple[re.Pattern[str], list[str]]] = []
    for pattern, urls in raw.items():
        if pattern.startswith("_"):  # "_comment" and friends
            continue
        if isinstance(urls, str):
            urls = [urls]
        try:
            mapping.append((re.compile(pattern, re.I), [str(u) for u in urls]))
        except re.error as e:
            logger.warning("Calendar: bad link pattern %r (%s)", pattern, e)

    logger.info("Calendar: %d reading link pattern(s) loaded", len(mapping))
    return mapping


def _links_for(title: str, mapping: list[tuple[re.Pattern[str], list[str]]]) -> list[str]:
    """Every URL whose pattern matches the title, in file order, deduplicated."""
    found: list[str] = []
    for pattern, urls in mapping:
        if pattern.search(title):
            found.extend(u for u in urls if u not in found)
    return found


def _event_due(event: dict, tz: ZoneInfo, all_day_time: str) -> datetime | None:
    """When the event asks to be done: its start, or end-of-day if all-day."""
    start = event.get("start") or {}

    if start.get("dateTime"):
        try:
            return dateparser.isoparse(start["dateTime"])
        except (ValueError, TypeError):
            return None

    if start.get("date"):
        try:
            day = dateparser.isoparse(start["date"]).date()
            hh, _, mm = all_day_time.partition(":")
            return datetime.combine(
                day, time(int(hh), int(mm or 0)), tzinfo=tz
            )
        except (ValueError, TypeError):
            return None

    return None


def fetch(cfg: dict, logger: logging.Logger, interactive: bool = False) -> list[Item]:
    gcfg = cfg.get("google_calendar", {})
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    window = cfg.get("window", {})
    lo = now_utc() - timedelta(days=window.get("past_days", 3))
    hi = now_utc() + timedelta(days=window.get("future_days", 120))

    include = gcfg.get("include_pattern") or ""
    exclude = gcfg.get("exclude_pattern") or ""
    inc_re = re.compile(include, re.I) if include else None
    exc_re = re.compile(exclude, re.I) if exclude else None
    fallback_label = gcfg.get("course_label", "Reading")
    all_day_time = gcfg.get("all_day_due_time", "23:59")
    wanted_ids = [str(c) for c in (gcfg.get("calendar_ids") or [])]
    link_map = _load_link_map(gcfg.get("links_file"), logger)

    svc = gauth.service("calendar", "v3", interactive, logger)

    # Resolve which calendars to read.
    calendars: list[dict] = []
    page = None
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        calendars.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    if wanted_ids:
        chosen = [
            c
            for c in calendars
            if c["id"] in wanted_ids
            or c.get("summary") in wanted_ids
            or ("primary" in wanted_ids and c.get("primary"))
        ]
        missing = [
            w
            for w in wanted_ids
            if w != "primary"
            and not any(c["id"] == w or c.get("summary") == w for c in calendars)
        ]
        for w in missing:
            logger.warning("Calendar: no calendar named/id %r", w)
    else:
        chosen = calendars

    logger.info("Calendar: reading %d calendar(s)", len(chosen))

    items: list[Item] = []
    linked = 0
    for cal in chosen:
        events: list[dict[str, Any]] = []
        page = None
        while True:
            try:
                resp = (
                    svc.events()
                    .list(
                        calendarId=cal["id"],
                        timeMin=lo.isoformat(),
                        timeMax=hi.isoformat(),
                        singleEvents=True,  # expand recurrence into instances
                        orderBy="startTime",
                        maxResults=2500,
                        pageToken=page,
                    )
                    .execute()
                )
            except HttpError as e:
                logger.warning("Calendar: cannot read %r (%s)", cal.get("summary"), e)
                break
            events.extend(resp.get("items", []))
            page = resp.get("nextPageToken")
            if not page:
                break

        kept = 0
        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            title = (ev.get("summary") or "").strip()
            if not title:
                continue

            haystack = f"{title}\n{ev.get('description') or ''}"
            if inc_re and not inc_re.search(haystack):
                continue
            if exc_re and exc_re.search(haystack):
                continue

            due = _event_due(ev, tz, all_day_time)
            if due is None or not (lo <= due <= hi):
                continue

            # A reading's own link is what you actually want to open; fall back
            # to the calendar entry only when the syllabus gave none.
            reading = _links_for(title, link_map)
            if reading:
                linked += 1

            items.append(
                Item(
                    uid=f"gcal:{ev['id']}",
                    source="gcal",
                    course=_course_from_title(title, fallback_label),
                    title=title,
                    due=due,
                    url=reading[0] if reading else ev.get("htmlLink"),
                    links=reading[1:],
                    # Calendar has no notion of done, so these arrive open and
                    # stay ticked once you tick them.
                    completed=False,
                    extra={"calendar": cal.get("summary")},
                )
            )
            kept += 1

        logger.info(
            "Calendar: %s -> %d matching event(s) of %d in window",
            cal.get("summary"),
            kept,
            len(events),
        )

    logger.info(
        "Calendar: %d event(s) to sync, %d with a reading link", len(items), linked
    )
    return items
