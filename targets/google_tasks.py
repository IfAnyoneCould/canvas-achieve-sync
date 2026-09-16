"""Google Tasks target: idempotent upsert of coursework into one task list."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

import courses as courses_mod
import gauth
from common import Item

# Kept as module attributes for switch_account.py and older call sites.
TOKEN_PATH = gauth.TOKEN_PATH
CLIENT_SECRET_PATH = gauth.CLIENT_SECRET_PATH


def _service(interactive: bool, logger: logging.Logger):
    return gauth.service("tasks", "v1", interactive, logger)


def _ensure_list(svc, name: str, logger: logging.Logger, dry_run: bool = False) -> str | None:
    page = None
    while True:
        resp = svc.tasklists().list(maxResults=100, pageToken=page).execute()
        for tl in resp.get("items", []):
            if tl["title"] == name:
                return tl["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    if dry_run:
        logger.info("[dry-run] would create task list %r", name)
        return None
    logger.info("creating task list %r", name)
    return svc.tasklists().insert(body={"title": name}).execute()["id"]


def _all_tasks(svc, list_id: str) -> dict[str, dict]:
    tasks: dict[str, dict] = {}
    page = None
    while True:
        resp = (
            svc.tasks()
            .list(
                tasklist=list_id,
                maxResults=100,
                showCompleted=True,
                showHidden=True,
                pageToken=page,
            )
            .execute()
        )
        for t in resp.get("items", []):
            tasks[t["id"]] = t
        page = resp.get("nextPageToken")
        if not page:
            break
    return tasks


def _body(
    item: Item, cfg: dict, tz: ZoneInfo, courses: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Google Tasks has no colour field of any kind, so the per-class colour has
    # to live in the text. The square matches the class's calendar colour.
    marker = courses_mod.marker(item.course, courses) if courses else ""
    title = cfg.get("title_format", "[{course}] {title}").format(
        course=item.course, title=item.title, marker=marker
    )
    title = title.lstrip()

    notes = []
    if item.due:
        local = item.due.astimezone(tz)
        # Google Tasks stores date only, so keep the real time where it stays visible.
        notes.append(f"Due {local.strftime('%a %b %d, %I:%M %p').replace(' 0', ' ')}")
    if item.url:
        notes.append(item.url)
    for link in item.links:
        if link != item.url:
            notes.append(link)
    notes.append(f"source: {item.source}")

    body: dict[str, Any] = {
        "title": title[:1024],
        "notes": "\n".join(notes)[:8192],
        "status": "completed" if item.completed else "needsAction",
    }
    if item.due:
        # Anchor to the local calendar date, then send as UTC midnight (API quirk).
        d = item.due.astimezone(tz).date()
        body["due"] = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    if not item.completed:
        body["completed"] = None
    return body


def sync(
    items: list[Item],
    cfg: dict,
    state: dict,
    logger: logging.Logger,
    interactive: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    gcfg = cfg.get("google_tasks", {})
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    courses = courses_mod.load(cfg)
    stats = {"created": 0, "updated": 0, "completed": 0, "removed": 0, "unchanged": 0}

    svc = _service(interactive, logger)
    list_id = _ensure_list(svc, gcfg.get("list_name", "Coursework"), logger, dry_run)
    existing = _all_tasks(svc, list_id) if list_id else {}
    mapping: dict[str, dict] = state.setdefault("google_tasks", {})

    seen: set[str] = set()

    for item in sorted(items, key=lambda i: (i.due or datetime.max.replace(tzinfo=timezone.utc))):
        seen.add(item.uid)
        body = _body(item, gcfg, tz, courses)
        fp = item.fingerprint()
        rec = mapping.get(item.uid)
        task = existing.get(rec["task_id"]) if rec else None

        if task is None:
            if dry_run:
                logger.info("[dry-run] create %s", body["title"])
            else:
                created = svc.tasks().insert(tasklist=list_id, body=body).execute()
                mapping[item.uid] = {"task_id": created["id"], "fingerprint": fp}
            stats["created"] += 1
            continue

        # Respect a box you ticked yourself: never re-open it from the source side.
        user_completed = task.get("status") == "completed" and not item.completed
        if user_completed:
            mapping[item.uid]["fingerprint"] = fp
            stats["unchanged"] += 1
            continue

        # The fingerprint only covers the *source* item, so a change in how a
        # task is rendered - the title format, a class marker, an added link -
        # would otherwise never reach the tasks already out there. Comparing
        # what is actually on the task closes that gap, and restores a managed
        # task that got edited by hand.
        needs_patch = (
            rec.get("fingerprint") != fp
            or task.get("status") != body["status"]
            or (task.get("title") or "").strip() != body["title"].strip()
            or (task.get("notes") or "").strip() != body["notes"].strip()
        )
        if not needs_patch:
            stats["unchanged"] += 1
            continue

        if dry_run:
            logger.info("[dry-run] update %s", body["title"])
        else:
            try:
                svc.tasks().patch(tasklist=list_id, task=task["id"], body=body).execute()
            except HttpError as e:
                logger.warning("could not update %r: %s", body["title"], e)
                continue
            mapping[item.uid]["fingerprint"] = fp
        stats["completed" if item.completed else "updated"] += 1

    if gcfg.get("delete_disappeared", True):
        for uid in [u for u in mapping if u not in seen]:
            rec = mapping[uid]
            task = existing.get(rec["task_id"])
            # Leave finished work in place as a record; only prune live tasks
            # whose source assignment is gone (deleted or aged out of the window).
            if task and task.get("status") != "completed":
                if dry_run:
                    logger.info("[dry-run] delete %s", task.get("title"))
                else:
                    try:
                        svc.tasks().delete(tasklist=list_id, task=task["id"]).execute()
                    except HttpError as e:
                        logger.warning("could not delete %s: %s", task.get("title"), e)
                stats["removed"] += 1
            if not dry_run:
                mapping.pop(uid, None)

    return stats
