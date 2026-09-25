"""Colour the NEU calendar by class, so a week reads at a glance.

Everything on that calendar arrives in the calendar's one default colour. This
sets each event's own colorId from `courses` in config: a colour per class and
a distinct one for office hours. Things belonging to no class are left alone.

Only events still in the calendar's default colour are touched, so a colour
picked by hand is never overwritten. Patches go out in batches
because a term is a few hundred events and one request each is slow enough to
run into rate limits.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from googleapiclient.errors import HttpError

import courses as courses_mod
import gauth
from common import now_utc

# Calendar rate-limits writes per user, and a batch is sent all at once - 50 at
# a time came back "Rate Limit Exceeded" for two thirds of them. Small batches,
# a breath between them, and a backoff retry for whatever still bounces.
BATCH = 10
PAUSE = 0.4
RETRIES = 5


def _patch_colors(
    svc, cal_id: str, pending: list[tuple[str, str]], log: logging.Logger
) -> tuple[int, list[tuple[str, str]]]:
    """Set each event's colour, retrying the rate-limited ones. Returns (done, failed)."""
    remaining = list(pending)
    done = 0
    delay = 1.0

    for attempt in range(RETRIES):
        if not remaining:
            break
        if attempt:
            log.info(
                "Colours: %d event(s) rate-limited, retrying in %.0fs", len(remaining), delay
            )
            time.sleep(delay)
            delay *= 2

        failed: list[tuple[str, str]] = []
        for start in range(0, len(remaining), BATCH):
            chunk = remaining[start : start + BATCH]
            errors: dict[str, Exception] = {}

            def _cb(request_id, _response, exception, _errors=errors):
                if exception is not None:
                    _errors[request_id] = exception

            batch = svc.new_batch_http_request(callback=_cb)
            for i, (event_id, want) in enumerate(chunk):
                batch.add(
                    svc.events().patch(
                        calendarId=cal_id, eventId=event_id, body={"colorId": want}
                    ),
                    request_id=str(i),
                )
            try:
                batch.execute()
            except HttpError as e:
                log.warning("Colours: a batch failed outright (%s)", e)
                failed.extend(chunk)
                continue

            for i, item in enumerate(chunk):
                if str(i) in errors:
                    failed.append(item)
                else:
                    done += 1
            time.sleep(PAUSE)

        remaining = failed

    return done, remaining


def wanted_color(ev: dict, courses: dict) -> str | None:
    # anything already coloured, by this or by hand, is left alone
    if ev.get("colorId"):
        return None
    return courses_mod.color((ev.get("summary") or "").strip(), courses)


def _calendars(svc, wanted: list[str], log: logging.Logger) -> list[dict]:
    page = None
    all_cals: list[dict] = []
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        all_cals.extend(resp.get("items", []))
        page = resp.get("nextPageToken")
        if not page:
            break

    if not wanted:
        return all_cals
    chosen = [c for c in all_cals if c["id"] in wanted or c.get("summary") in wanted]
    for w in wanted:
        if not any(c["id"] == w or c.get("summary") == w for c in all_cals):
            log.warning("Colours: no calendar named/id %r", w)
    return chosen


def apply_colors(cfg: dict, log: logging.Logger, interactive: bool = False) -> dict[str, int]:
    ccfg = cfg.get("calendar_colors", {})
    courses = courses_mod.load(cfg)
    if not courses:
        log.warning("Colours: no `courses` configured; nothing to do")
        return {"changed": 0, "checked": 0}

    # Writing an event needs the scope the packets introduced; if that has not
    # been granted yet, say so once and leave the calendar alone.
    if not interactive and not gauth.authorized_for(gauth.SCOPES):
        log.info("Colours: calendar-write not granted yet; skipping")
        return {"changed": 0, "checked": 0}

    window = cfg.get("window", {})
    lo = (now_utc() - timedelta(days=window.get("past_days", 3))).isoformat()
    hi = (now_utc() + timedelta(days=window.get("future_days", 120))).isoformat()

    svc = gauth.service("calendar", "v3", interactive, log, need=gauth.SCOPES)
    stats = {"changed": 0, "checked": 0}
    failures: list[str] = []

    for cal in _calendars(svc, [str(c) for c in (ccfg.get("calendar_ids") or [])], log):
        events: list[dict] = []
        page = None
        while True:
            try:
                resp = (
                    svc.events()
                    .list(
                        calendarId=cal["id"],
                        timeMin=lo,
                        timeMax=hi,
                        singleEvents=False,  # recurring series once, not per instance
                        maxResults=2500,
                        pageToken=page,
                    )
                    .execute()
                )
            except HttpError as e:
                log.warning("Colours: cannot read %r (%s)", cal.get("summary"), e)
                break
            events.extend(resp.get("items", []))
            page = resp.get("nextPageToken")
            if not page:
                break

        pending: list[tuple[str, str]] = []
        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            title = (ev.get("summary") or "").strip()
            if not title:
                continue
            stats["checked"] += 1
            want = wanted_color(ev, courses)
            if want:
                pending.append((ev["id"], want))

        done, failed = _patch_colors(svc, cal["id"], pending, log)
        stats["changed"] += done
        failures.extend(f"{cal.get('summary')}:{eid}" for eid, _ in failed)

        log.info(
            "Colours: %s -> %d of %d event(s) recoloured (%d already right)",
            cal.get("summary"),
            done,
            len(pending),
            stats["checked"] - len(pending),
        )

    if failures:
        log.warning(
            "Colours: %d event(s) would not take a colour; the next run retries them",
            len(failures),
        )

    return stats


if __name__ == "__main__":
    from common import load_config, setup_logging

    _log = setup_logging(True)
    print(apply_colors(load_config(), _log, interactive=True))
