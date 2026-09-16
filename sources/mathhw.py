"""MATH 2321 homework: turn each posted sheet into a self-contained packet.

The professor posts a PDF to the course Modules page that only *names* the
textbook problems ("§1.2 # 41, 42, 43"), so on its own it is not something you
can sit down and work from. For each new sheet this:

  1. notices it on Canvas,
  2. builds a packet PDF - the named problems clipped out of the textbook,
     followed by his own additional problems (see packet.py),
  3. uploads that to Drive and puts the link on a calendar event, and
  4. emits a task, due the end of the week it was posted, since these sheets
     carry no due date of their own.

Each step is recorded in state.json, so a sheet is built and uploaded once and
later runs only re-check. Re-posting a sheet (Canvas bumps `updated_at`)
rebuilds it in place, keeping the same Drive link and calendar event.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

import gauth
from common import ROOT, Item
from packet import build_packet, parse_homework
from sources.canvas import Canvas

FOLDER_MIME = "application/vnd.google-apps.folder"


def _due_date(posted: date, weekday: int) -> date:
    """The next given weekday on or after the day it was posted (Mon=0)."""
    ahead = (weekday - posted.weekday()) % 7
    return posted + timedelta(days=ahead)


def _sheets(api: Canvas, course_id: int, pattern: re.Pattern[str], log) -> list[dict]:
    """Homework files on the course, preferring the Modules page ordering.

    Modules is where he actually posts, but a file can sit in Files without
    being linked into a module yet, so both are checked and merged by file id.
    """
    found: dict[int, dict] = {}

    try:
        for module in api._paged(f"courses/{course_id}/modules", include=["items"]):
            for item in module.get("items") or []:
                if item.get("type") != "File" or not pattern.search(item.get("title") or ""):
                    continue
                fid = item.get("content_id")
                if fid:
                    found[int(fid)] = {"id": int(fid), "module": module.get("name")}
    except Exception as e:  # noqa: BLE001 - modules are optional, Files is the fallback
        log.debug("Math: modules unreadable (%s)", e)

    for f in api._paged(f"courses/{course_id}/files"):
        name = f.get("display_name") or ""
        if not pattern.search(name):
            continue
        rec = found.setdefault(int(f["id"]), {"id": int(f["id"]), "module": None})
        rec.update(
            {
                "name": name,
                "url": f.get("url"),
                "updated_at": f.get("updated_at"),
                "created_at": f.get("created_at"),
            }
        )

    sheets = [s for s in found.values() if s.get("url")]
    sheets.sort(key=lambda s: (s.get("created_at") or "", s["id"]))
    return sheets


def _drive_folder(svc, name: str, state: dict, log) -> str | None:
    """Find or make the packet folder. Its id is cached; a deleted folder is remade."""
    folder_id = state.get("drive_folder_id")
    if folder_id:
        try:
            meta = svc.files().get(fileId=folder_id, fields="id,trashed").execute()
            if not meta.get("trashed"):
                return folder_id
        except HttpError:
            log.info("Math: cached Drive folder is gone, making a new one")

    try:
        created = (
            svc.files()
            .create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id")
            .execute()
        )
    except HttpError as e:
        log.warning("Math: could not create Drive folder (%s); uploading to My Drive", e)
        return None
    state["drive_folder_id"] = created["id"]
    log.info("Math: created Drive folder %r", name)
    return created["id"]


def _upload(svc, path: Path, folder: str | None, existing: str | None, log) -> dict[str, str]:
    """Upload the packet, replacing the previous revision so the link is stable."""
    media = MediaFileUpload(str(path), mimetype="application/pdf", resumable=False)
    if existing:
        try:
            f = (
                svc.files()
                .update(fileId=existing, media_body=media, fields="id,webViewLink")
                .execute()
            )
            return {"id": f["id"], "link": f["webViewLink"]}
        except HttpError as e:
            log.info("Math: previous Drive file unusable (%s); uploading a fresh one", e)

    body: dict[str, Any] = {"name": path.name}
    if folder:
        body["parents"] = [folder]
    f = svc.files().create(body=body, media_body=media, fields="id,webViewLink").execute()
    return {"id": f["id"], "link": f["webViewLink"]}


def _calendar_id(svc, wanted: str, log) -> str | None:
    page = None
    while True:
        resp = svc.calendarList().list(maxResults=250, pageToken=page).execute()
        for c in resp.get("items", []):
            if c["id"] == wanted or c.get("summary") == wanted:
                return c["id"]
        page = resp.get("nextPageToken")
        if not page:
            break
    log.warning("Math: no calendar named %r; skipping the calendar entry", wanted)
    return None


def _event(svc, cal_id: str, event_id: str | None, body: dict, log) -> str | None:
    """Create the event, or patch the one from last time if it still exists."""
    if event_id:
        try:
            svc.events().patch(calendarId=cal_id, eventId=event_id, body=body).execute()
            return event_id
        except HttpError as e:
            log.info("Math: calendar entry gone (%s); making a new one", e)
    try:
        return svc.events().insert(calendarId=cal_id, body=body).execute()["id"]
    except HttpError as e:
        log.warning("Math: could not create the calendar entry (%s)", e)
        return None


def fetch(cfg: dict, log: logging.Logger, state: dict, interactive: bool = False) -> list[Item]:
    mcfg = cfg.get("math_homework", {})
    ccfg = cfg.get("canvas", {})
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))

    textbook = Path(mcfg.get("textbook", ""))
    if not textbook.exists():
        log.warning("Math: textbook not found at %s; skipping", textbook)
        return []

    # Drive and calendar-write are newer than the stored token may be. Rather
    # than fail the run (which would also take Canvas, Achieve and Tasks down),
    # sit out this cycle and say what unblocks it - but never on an interactive
    # run, which is exactly the one that can ask for the missing consent.
    if not interactive and not gauth.authorized_for(gauth.SCOPES):
        log.warning(
            "Math: packets need Drive and calendar-write access, which has not "
            "been granted yet - run `python sync.py --login` once. Skipping."
        )
        return []

    course_id = int(mcfg["canvas_course_id"])
    pattern = re.compile(mcfg.get("file_pattern", r"^Homework\s*\d+"), re.I)
    label = mcfg.get("course", "MATH 2321")
    weekday = int(mcfg.get("due_weekday", 4))
    hh, _, mm = str(mcfg.get("due_time", "23:59")).partition(":")

    packet_dir = ROOT / mcfg.get("packet_dir", "packets")
    cache_dir = packet_dir / "source"
    cache_dir.mkdir(parents=True, exist_ok=True)

    api = Canvas(ccfg["base_url"], ccfg["token"], log)
    sheets = _sheets(api, course_id, pattern, log)
    log.info("Math: %d homework sheet(s) posted", len(sheets))
    if not sheets:
        return []

    mine: dict[str, Any] = state.setdefault("math_homework", {})
    drive = calendar = None
    cal_id: str | None = None
    items: list[Item] = []

    for sheet in sheets:
        key = str(sheet["id"])
        rec: dict[str, Any] = mine.setdefault(key, {})
        signature = f"{sheet['id']}:{sheet.get('updated_at')}"
        name = sheet.get("name") or f"Homework {sheet['id']}"
        out_pdf = packet_dir / f"{Path(name).stem} packet.pdf"

        if rec.get("signature") != signature or not out_pdf.exists():
            log.info("Math: building a packet for %s", name)
            src = cache_dir / name
            try:
                r = api.s.get(sheet["url"], timeout=60)
                r.raise_for_status()
                src.write_bytes(r.content)
            except Exception as e:  # noqa: BLE001
                log.warning("Math: could not download %s (%s)", name, e)
                continue

            try:
                spec = parse_homework(src, log)
                result = build_packet(spec, src, textbook, out_pdf, log, course=label)
            except Exception as e:  # noqa: BLE001 - a malformed sheet must not stop the sync
                log.error("Math: could not build a packet for %s (%s)", name, e)
                continue

            rec.update(
                {
                    "signature": signature,
                    "title": spec.title,
                    "problems": result["included"],
                    "missing": result["missing"],
                    "packet": str(out_pdf),
                    "built_at": datetime.now(tz).isoformat(timespec="seconds"),
                }
            )
            rec["uploaded"] = False

        posted_raw = sheet.get("created_at") or ""
        try:
            posted = datetime.fromisoformat(posted_raw.replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            posted = datetime.now(tz)
        due_day = _due_date(posted.date(), weekday)
        due = datetime.combine(due_day, time(int(hh), int(mm or 0)), tzinfo=tz)

        # Upload and put it on the calendar; both are skipped once done.
        if not rec.get("uploaded"):
            if drive is None:
                drive = gauth.service("drive", "v3", interactive, log, need=gauth.SCOPES)
            folder = _drive_folder(drive, mcfg.get("drive_folder", "Coursework packets"), state, log)
            try:
                up = _upload(drive, out_pdf, folder, rec.get("drive_id"), log)
            except HttpError as e:
                log.warning("Math: upload of %s failed (%s)", out_pdf.name, e)
                up = None
            if up:
                rec.update({"drive_id": up["id"], "link": up["link"], "uploaded": True})
                log.info("Math: uploaded %s", out_pdf.name)

                if calendar is None:
                    calendar = gauth.service(
                        "calendar", "v3", interactive, log, need=gauth.SCOPES
                    )
                    cal_id = _calendar_id(calendar, mcfg.get("calendar_id", "NEU"), log)
                if cal_id:
                    summary = f"{label} {rec.get('title', name)} — problem packet"
                    rec["event_id"] = _event(
                        calendar,
                        cal_id,
                        rec.get("event_id"),
                        {
                            "summary": summary,
                            "description": (
                                f"Problem packet (textbook problems + his additional ones):\n"
                                f"{rec['link']}\n\n"
                                f"Posted sheet: "
                                f"{ccfg['base_url']}/courses/{course_id}/files/{sheet['id']}\n\n"
                                "Built automatically; no due date was given, so this sits at "
                                "the end of the week it was posted."
                            ),
                            "start": {"date": due_day.isoformat()},
                            "end": {"date": (due_day + timedelta(days=1)).isoformat()},
                            "transparency": "transparent",
                        },
                        log,
                    )

        link = rec.get("link")
        canvas_url = f"{ccfg['base_url']}/courses/{course_id}/files/{sheet['id']}"
        title = f"{rec.get('title', name)} — problem packet"
        if rec.get("problems"):
            title += f" ({rec['problems']} problems)"

        items.append(
            Item(
                uid=f"mathhw:{sheet['id']}",
                source="mathhw",
                course=label,
                title=title,
                due=due,
                url=link or canvas_url,
                links=[canvas_url] if link else [],
                completed=False,
                extra={"missing": rec.get("missing") or []},
            )
        )

    log.info("Math: %d packet task(s)", len(items))
    return items
