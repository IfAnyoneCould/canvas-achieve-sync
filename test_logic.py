"""Offline harness: exercises the Google Tasks upsert logic with a fake API."""
import sys, json
from datetime import datetime, timedelta, timezone
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from common import Item, setup_logging
import targets.google_tasks as gt
import sources.achieve as ach

log = setup_logging(False)
log.handlers = [h for h in log.handlers if not hasattr(h, "baseFilename")]

CFG = {"timezone": "America/New_York",
       "google_tasks": {"list_name": "Coursework", "delete_disappeared": True}}

# ---- fake Google Tasks service -------------------------------------------
class FakeReq:
    def __init__(self, fn): self.fn = fn
    def execute(self): return self.fn()

class FakeTasks:
    def __init__(self, store): self.store = store; self.n = 0
    def list(self, tasklist, maxResults=None, showCompleted=None, showHidden=None, pageToken=None):
        return FakeReq(lambda: {"items": list(self.store.values())})
    def insert(self, tasklist, body):
        def go():
            self.n += 1
            t = dict(body); t["id"] = f"t{self.n}"
            self.store[t["id"]] = t
            return t
        return FakeReq(go)
    def patch(self, tasklist, task, body):
        def go():
            self.store[task].update(body); return self.store[task]
        return FakeReq(go)
    def delete(self, tasklist, task):
        def go(): self.store.pop(task, None); return {}
        return FakeReq(go)

class FakeSvc:
    def __init__(self, store): self._t = FakeTasks(store)
    def tasks(self): return self._t
    def tasklists(self): raise AssertionError("_ensure_list should be patched")

store = {}
svc = FakeSvc(store)
gt._service = lambda interactive, logger: svc
gt._ensure_list = lambda s, name, logger, dry_run=False: "L1"

BASE = datetime(2026, 9, 14, 17, 0, tzinfo=timezone.utc)  # fixed clock, stable fingerprints

def item(uid, title, days, done=False, course="CS 3500"):
    return Item(uid=uid, source="canvas", course=course, title=title,
                due=BASE + timedelta(days=days),
                url=f"https://canvas.northeastern.edu/a/{uid}", completed=done)

state = {}
fails = []
def check(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  <- {extra}"))
    if not cond: fails.append(label)

# 1. first run creates
s = gt.sync([item("canvas:1", "HW 5", 3), item("canvas:2", "Quiz 2", 5)], CFG, state, log)
check("creates both tasks", s["created"] == 2 and len(store) == 2, s)
check("title format", any(t["title"] == "[CS 3500] HW 5" for t in store.values()),
      [t["title"] for t in store.values()])
check("due is date-anchored UTC midnight",
      all(t["due"].endswith("T00:00:00Z") for t in store.values()),
      [t.get("due") for t in store.values()])
check("notes carry real due time + url",
      all("Due " in t["notes"] and "canvas.northeastern.edu" in t["notes"] for t in store.values()))

# 2. re-run with no changes => no duplicates
s = gt.sync([item("canvas:1", "HW 5", 3), item("canvas:2", "Quiz 2", 5)], CFG, state, log)
check("idempotent re-run", s == {"created": 0, "updated": 0, "completed": 0, "removed": 0, "unchanged": 2}, s)
check("still 2 tasks", len(store) == 2, len(store))

# 3. due date changes upstream => patch, not duplicate
s = gt.sync([item("canvas:1", "HW 5", 10), item("canvas:2", "Quiz 2", 5)], CFG, state, log)
check("due-date change patches in place", s["updated"] == 1 and len(store) == 2, s)

# 4. submitted upstream => task completed
s = gt.sync([item("canvas:1", "HW 5", 10, done=True), item("canvas:2", "Quiz 2", 5)], CFG, state, log)
t1 = store[state["google_tasks"]["canvas:1"]["task_id"]]
check("submission marks task completed", t1["status"] == "completed", t1["status"])

# 5. user ticks a box manually => sync must not re-open it
t2id = state["google_tasks"]["canvas:2"]["task_id"]
store[t2id]["status"] = "completed"
s = gt.sync([item("canvas:1", "HW 5", 10, done=True), item("canvas:2", "Quiz 2", 5)], CFG, state, log)
check("manual completion is respected", store[t2id]["status"] == "completed", store[t2id]["status"])

# 6. assignment disappears upstream => live task pruned, finished one kept
store["t9"] = {"id": "t9", "title": "[CS 3500] Dropped HW", "status": "needsAction"}
state["google_tasks"]["canvas:9"] = {"task_id": "t9", "fingerprint": "x"}
s = gt.sync([item("canvas:1", "HW 5", 10, done=True)], CFG, state, log)
check("vanished live task deleted", "t9" not in store, list(store))
check("completed task kept as record", t2id in store, list(store))
check("state pruned", "canvas:9" not in state["google_tasks"], state["google_tasks"].keys())

# 7. achieve payload mapping (real /api/v1/courses/{id}/assignments schema)
LO = datetime(2026, 9, 11, tzinfo=timezone.utc)
HI = datetime(2027, 1, 12, tzinfo=timezone.utc)
rows = [
    {"id": "a7f3", "name": "HW 5", "tool": "assessment", "assigned_points": 100,
     "deleted_at": None, "assignment_due_at": "2026-10-17T03:59:00.000Z",
     "visibility_until_date": "2026-12-21T04:59:00.000Z"},
    {"id": "f742", "name": "HW 6", "tool": "assessment", "deleted_at": None,
     "assignment_due_at": "2026-10-28T03:59:00.000Z"},
    # undated course content: the majority of rows, and not a todo
    {"id": "read1", "name": "Chapter 3 Reading", "deleted_at": None,
     "assignment_due_at": None, "visibility_until_date": "2026-12-21T04:59:00.000Z"},
    # deleted upstream
    {"id": "gone", "name": "Old HW", "deleted_at": "2026-09-01T00:00:00.000Z",
     "assignment_due_at": "2026-10-01T03:59:00.000Z"},
    # outside the sync window
    {"id": "far", "name": "Next Term HW", "deleted_at": None,
     "assignment_due_at": "2027-06-01T03:59:00.000Z"},
]
got = ach.parse_assignments(rows, "CHEM 1151", "https://achieve.macmillanlearning.com", "c1", LO, HI)
titles = sorted(g.title for g in got)
check("achieve keeps only dated, live, in-window work", titles == ["HW 5", "HW 6"], titles)
check("achieve uid is source-prefixed", all(g.uid.startswith("achieve:") for g in got),
      [g.uid for g in got])
check("achieve due parsed as tz-aware UTC",
      all(g.due and g.due.tzinfo is not None for g in got), [g.due for g in got])
check("achieve items arrive open (endpoint has no submission state)",
      all(not g.completed for g in got))
check("achieve links to its course", all(g.url.endswith("/courses/c1") for g in got),
      [g.url for g in got])
check("achieve ignores the visibility_until_date decoy",
      all(g.due.month == 10 for g in got), [g.due for g in got])
check("achieve tolerates a non-list payload", ach.parse_assignments(None, "X", "b", "c", LO, HI) == [])

# course label: 'CHEM 1151 Fall 26 - General Chemistry for Engineers-2' -> 'CHEM 1151'
check("achieve course label prefers short_name",
      ach._course_label({"id": "c1", "short_name": "CHEM 1151",
                         "name": "CHEM 1151 Fall 26 - General Chemistry for Engineers-2"}, {})
      == "CHEM 1151")
check("achieve course label falls back to parsing the long name",
      ach._course_label({"id": "c1", "name": "CHEM 1151 Fall 26 - General Chemistry"}, {})
      == "CHEM 1151")
check("achieve course label honors overrides",
      ach._course_label({"id": "c1", "short_name": "CHEM 1151"}, {"CHEM 1151": "Chem"}) == "Chem")
check("achieve rejects junk dates", ach._as_dt("not a date") is None and ach._as_dt(None) is None)

# 8. dedupe across sources (sync.py logic)
import sync as syncmod
dup = [Item(uid="canvas:1", source="canvas", course="CHEM", title="Ch 5 Homework",
            due=datetime(2026, 9, 20, 3, 59, tzinfo=timezone.utc)),
       Item(uid="achieve:a78", source="achieve", course="CHEM 1211", title="Ch 5 Homework",
            due=datetime(2026, 9, 20, 3, 59, tzinfo=timezone.utc))]
seen, out = {}, []
for it in sorted(dup, key=lambda i: 0 if i.source == "canvas" else 1):
    k = (it.title.lower().strip(), it.due.date().isoformat())
    if k in seen: continue
    seen[k] = it; out.append(it)
check("cross-source duplicate collapses to canvas", len(out) == 1 and out[0].source == "canvas", out)

# 9. calendar source
import sources.gcal as gcal
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
check("gcal reads a course code off the title",
      gcal._course_from_title("PHIL 2390 reading: Ch 4", "Reading") == "PHIL 2390")
check("gcal falls back to the configured label",
      gcal._course_from_title("Cults reading — Orsi, 'Snakes Alive'", "PHIL 2390") == "PHIL 2390")
check("gcal ignores a year in the citation tail",
      gcal._course_from_title(
          "Cults reading — Harnett, 'The Prophet Who Failed' (Harper's, July 2024)",
          "PHIL 2390") == "PHIL 2390")
check("gcal all-day event is due at the configured time",
      gcal._event_due({"start": {"date": "2026-09-21"}}, NY, "23:59")
      == datetime(2026, 9, 21, 23, 59, tzinfo=NY))
check("gcal timed event is due at its start",
      gcal._event_due({"start": {"dateTime": "2026-09-21T13:35:00-04:00"}}, NY, "23:59")
      == datetime(2026, 9, 21, 17, 35, tzinfo=timezone.utc))
check("gcal rejects an event with no start",
      gcal._event_due({}, NY, "23:59") is None
      and gcal._event_due({"start": {"date": "nonsense"}}, NY, "23:59") is None)

# a calendar entry mirroring a real assignment loses to it
dup3 = [Item(uid="gcal:e1", source="gcal", course="PHIL 2390", title="Ch 5 Homework",
             due=datetime(2026, 9, 20, 3, 59, tzinfo=timezone.utc))] + dup
seen, out = {}, []
for it in sorted(dup3, key=lambda i: {"canvas": 0, "achieve": 1, "gcal": 2}.get(i.source, 9)):
    k = (it.title.lower().strip(), it.due.date().isoformat())
    if k in seen: continue
    seen[k] = it; out.append(it)
check("calendar duplicate loses to a real assignment",
      len(out) == 1 and out[0].source == "canvas", out)

# 10. reading links
lm = gcal._load_link_map("reading_links.json", log)
check("link map loads and skips _comment", len(lm) == 27, len(lm))
check("link map matches a screening",
      gcal._links_for("Cults — Watch: Blessed Child (116 min, Kanopy)", lm)
      == ["https://www.kanopy.com/en/product/blessed-child?vp=northeastern"])
check("link map returns every platform for one reading",
      len(gcal._links_for("Cults — Short Creek E4 'The Kingdom of Heaven or Nothing'", lm)) == 2)
check("an event nothing matches gets no link",
      gcal._links_for("PHIL 2390 Cults & Sects", lm) == [])
check("the Chidester reading gets its own Canvas pdf, not the Jonestown film",
      gcal._links_for(
          "Cults reading — Chidester, 'Rituals of Exclusion and the Jonestown Dead'", lm)
      == ["https://northeastern.instructure.com/courses/266503/files/43322023"])
check("the film and the PBS follow-up page get different links",
      gcal._links_for("Cults — Watch: Let the Fire Burn (2013, 95 min)", lm)
      != gcal._links_for("Cults — PBS Independent Lens: 'Let the Fire Burn' fallout page", lm))
check("a missing link file degrades to no links",
      gcal._load_link_map("no_such_file.json", log) == []
      and gcal._load_link_map(None, log) == [])

# extra links land in the notes, under the main url
body = gt._body(Item(uid="gcal:x", source="gcal", course="PHIL 2390", title="Short Creek E4",
                     due=BASE, url="https://apple.example/e4",
                     links=["https://spotify.example/e4", "https://apple.example/e4"]),
                CFG["google_tasks"], ZoneInfo("America/New_York"))
check("notes list the main url then the alternates, no repeat",
      body["notes"].count("apple.example") == 1 and "spotify.example" in body["notes"],
      body["notes"])

# 11. math homework packets
import tempfile
from datetime import date
from pathlib import Path

import packet as pk
from sources.mathhw import _due_date

check("problem ranges expand in order",
      pk._expand("1-5, 9-13, 18, 19") == [1, 2, 3, 4, 5, 9, 10, 11, 12, 13, 18, 19])
check("expansion drops junk and repeats", pk._expand("3, 3, x, 7") == [3, 7])
check("expansion refuses an absurd range", pk._expand("1-9999") == [])
check("homework posted midweek is due that Friday",
      _due_date(date(2026, 9, 14), 4) == date(2026, 9, 18))
check("homework posted on Friday is due the same day",
      _due_date(date(2026, 9, 18), 4) == date(2026, 9, 18))
check("homework posted at the weekend rolls to the next Friday",
      _due_date(date(2026, 9, 19), 4) == date(2026, 9, 25)
      and _due_date(date(2026, 9, 20), 4) == date(2026, 9, 25))

import pymupdf

with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp) / "Homework 7.pdf"
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate([
        "Math 2321: Calculus 3, Fall 2026",
        "Homework 7",
        "Book problems:",
        "§2.1 # 4, 5, 6",
        "§2.3 #1-3, 10",
        "Additional problems:",
        "(1) Compute something with (7) in it.",
    ]):
        page.insert_text((72, 100 + i * 20), line)
    d.save(fake)
    d.close()

    spec = pk.parse_homework(fake, log)
    check("sheet number is read off the title", spec.number == 7, spec.number)
    check("book problems parse per section",
          spec.book == {"2.1": [4, 5, 6], "2.3": [1, 2, 3, 10]}, spec.book)
    check("the extras are located", spec.extra_page == 0 and spec.extra_y is not None)
    check("a number inside the extras is not read as a book problem",
          7 not in spec.book.get("2.3", []), spec.book)

# The textbook is the one input the harness cannot synthesize; skip if absent.
_cfg_path = Path(__file__).with_name("config.json")
_book = ""
if _cfg_path.exists():
    _book = json.loads(_cfg_path.read_text(encoding="utf-8")).get(
        "math_homework", {}).get("textbook", "")
if _book and Path(_book).exists():
    bdoc = pymupdf.open(_book)
    secs = pk._section_pages(bdoc)
    check("section 1.2's exercises are located", secs.get("1.2") == range(38, 42), secs.get("1.2"))
    check("front matter does not shift the chapter numbering",
          "1.1" in secs and "2.1" in secs, sorted(secs)[:4])

    lines = pk._classify(bdoc, secs["1.2"])
    found = {ln.number for ln in lines if ln.kind == "number"}
    check("problems are found on both recto and verso pages",
          {41, 42, 43}.issubset(found) and len({ln.cx0 for ln in lines}) == 2,
          sorted(found))

    # §1.3 is the awkward shape: its exercises open at the foot of a page that
    # is otherwise prose, so that page carries an instruction but no problem
    # number to measure the column off, and the prose sits at the very same
    # left edge as the instruction.
    lines = pk._classify(bdoc, secs["1.3"])
    opening = [ln for ln in lines if ln.page == secs["1.3"].start]
    text = " ".join(
        " ".join(bdoc[ln.page].get_textbox(
            pymupdf.Rect(ln.cx0, ln.y0, ln.cx1, ln.y1)).split())
        for ln in opening
    )
    check("an instruction on a page with no problem numbers is still read as one",
          bool(opening) and all(ln.kind == "instruction" for ln in opening),
          [ln.kind for ln in opening])
    check("the prose above the exercises heading is left out",
          "dot product" in text and "transpose" not in text, text[:60])
    check("the first problem under it is found, and keeps that instruction",
          1 in {ln.number for ln in lines if ln.kind == "number"})
    bdoc.close()
else:
    print("SKIP  textbook checks (textbook pdf not on this machine)")

# 12. class identity: colours and markers
import courses as cm

COURSES = {
    "Office hours": {"match": "office hours", "calendar_color": "7", "marker": ""},
    "CHEM 1151": {"match": r"CHEM\s*115[13]", "calendar_color": "11", "marker": "R"},
    "MATH 2321": {"match": r"MATH\s*2321", "calendar_color": "9", "marker": "B"},
    "PHIL 2390": {"match": r"PHIL\s*2390|^Cults\b", "calendar_color": "3", "marker": "P"},
    "GE 1501": {"match": r"GE\s*1501|Cornerstone", "calendar_color": "6", "marker": "O"},
    "_default": {"calendar_color": "8", "marker": "W"},
}

check("a course label matches itself", cm.identify("MATH 2321", COURSES) == "MATH 2321")
check("an event title matches its course",
      cm.identify("CHEM 1151 HW 3 due", COURSES) == "CHEM 1151")
check("the recitation counts as its parent course",
      cm.identify("CHEM 1153 Recitation (Gen Chem)", COURSES) == "CHEM 1151")
check("cornerstone belongs to GE 1501",
      cm.identify("Cornerstone HW due (weekly)", COURSES) == "GE 1501")
check("a reading title belongs to PHIL",
      cm.identify("Cults — Watch: Hail, Satan!", COURSES) == "PHIL 2390")
check("office hours are their own category, not the professor's class",
      cm.identify("Dr. R-S Office Hours", COURSES) == "Office hours"
      and cm.color("Dr. R-S Office Hours", COURSES) == "7")
check("an unclaimed event falls to the default",
      cm.identify("Last day to DROP with a W", COURSES) == "_default"
      and cm.color("Last day to DROP with a W", COURSES) == "8")
check("every class has a distinct colour",
      len({s["calendar_color"] for s in COURSES.values()}) == len(COURSES))
check("markers come from the same decision as colours",
      cm.marker("MATH 2321", COURSES) == "B" and cm.marker("nothing", COURSES) == "W")
check("comment keys are not treated as classes",
      "_comment" not in cm.load({"courses": {"_comment": "hi", "X": {"marker": "x"}}}))

# the colourer only fills in events still in the calendar default
from colorize import wanted_color

check("an uncoloured class event gets its class colour",
      wanted_color({"summary": "CHEM 1151 HW 3 due"}, COURSES) == "11")
check("a hand-picked colour on a class event is kept",
      wanted_color({"summary": "GE1501 minigolf group", "colorId": "2"}, COURSES) is None)
check("a hand-picked colour on a non-class event is kept",
      wanted_color({"summary": "Rev Kick Off", "colorId": "4"}, COURSES) is None)
check("a default with no calendar colour leaves non-class events alone",
      wanted_color({"summary": "Rev Kick Off"},
                   {**COURSES, "_default": {"marker": "W"}}) is None)

# the marker reaches the task title, and is absent when no classes are configured
tz_ny = ZoneInfo("America/New_York")
it = item("canvas:m1", "HW 5", 3, course="MATH 2321")
check("marker prefixes the task title",
      gt._body(it, {"title_format": "{marker} [{course}] {title}"}, tz_ny, COURSES)["title"]
      == "B [MATH 2321] HW 5")
check("no classes configured leaves the title clean",
      gt._body(it, {"title_format": "{marker} [{course}] {title}"}, tz_ny, None)["title"]
      == "[MATH 2321] HW 5")

# 13. the real palette stays legible
import colorsys

# Google's fixed event palette; colorId -> hex.
GOOGLE_EVENT_COLORS = {
    "1": "#a4bdfc", "2": "#7ae7bf", "3": "#dbadff", "4": "#ff887c",
    "5": "#fbd75b", "6": "#ffb878", "7": "#46d6db", "8": "#e1e1e1",
    "9": "#5484ed", "10": "#51b749", "11": "#dc2127",
}


def _hue(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360


def _gap(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


if _cfg_path.exists():
    real = cm.load(json.loads(_cfg_path.read_text(encoding="utf-8")))
    used = {k: v.get("calendar_color") for k, v in real.items() if v.get("calendar_color")}
    check("every class has its own calendar colour",
          len(set(used.values())) == len(used), used)
    check("every colour is a real Google event colour",
          all(c in GOOGLE_EVENT_COLORS for c in used.values()), used)

    # Tomato and Tangerine are 30 degrees apart and were mistaken for each
    # other; nothing in use should be that close again.
    hues = {k: _hue(GOOGLE_EVENT_COLORS[c]) for k, c in used.items()
            if c != "8"}  # grey has no meaningful hue
    worst = min(
        ((_gap(h1, h2), a, b) for a, h1 in hues.items() for b, h2 in hues.items() if a < b),
        default=(999, "", ""),
    )
    check(f"closest pair of colours is {worst[0]:.0f} degrees apart ({worst[1]}/{worst[2]})",
          worst[0] >= 40, worst)
    check("green is in use", "10" in used.values(), used)
    check("events belonging to no class are never greyed",
          "_default" not in used, used)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)


