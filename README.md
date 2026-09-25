# canvas-achieve-sync

Continuously mirrors your Canvas (and optionally Macmillan Achieve) coursework
into a **Google Tasks** list called `Coursework`. Runs every 2 hours via Windows
Task Scheduler, and is safe to run repeatedly — it updates tasks in place
instead of piling up duplicates.

Behavior worth knowing:

- Submitting an assignment in Canvas ticks the task off automatically.
- Ticking a task off yourself is never undone by a later sync.
- If an assignment is deleted upstream, its **unfinished** task is removed;
  finished ones stay as a record.
- Google Tasks only stores a due *date*, so the exact due *time* goes in the
  task notes (`Due Fri Sep 18, 11:59 PM`) along with a direct link.

## Setup

Everything below is already installed (`.venv` with all dependencies, plus the
Playwright Chromium build). You need to supply two credentials.

### 1. Canvas access token — done, verified working

1. Open <https://northeastern.instructure.com/profile/settings>
2. **+ New Access Token** → purpose `todo sync` → leave expiry blank → **Generate**
3. Copy the token (it is shown only once).
4. `copy config.example.json config.json`, then paste it into `canvas.token`.

The API host is `northeastern.instructure.com`. Do **not** use
`canvas.northeastern.edu` — that's the knowledge-base site, and it drops API
connections without a response (`RemoteDisconnected`) rather than redirecting.

Course labels are derived from Canvas course codes with the section/term noise
stripped (`GE1501.MERGED.202710` → `GE 1501`). Override any of them with
`canvas.course_names` in `config.json`, keyed by course code, name, or id.

### 2. Google Tasks OAuth client

1. Go to <https://console.cloud.google.com/projectcreate> and make a project
   (e.g. `coursework-sync`).
2. **APIs & Services → Library →** search *Google Tasks API* → **Enable**.
3. **APIs & Services → OAuth consent screen**: External, add yourself as a
   Test user (this keeps the token from expiring every week once published, but
   Testing mode is fine).
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. **Download JSON** and save it in this folder as `credentials.json`.

### 3. First run

```powershell
cd C:\Users\JonahW\canvas-achieve-sync
.\.venv\Scripts\python.exe sync.py --list              # what Canvas sees (no Google needed)
.\.venv\Scripts\python.exe sync.py --login --dry-run   # browser consent, shows planned changes
.\.venv\Scripts\python.exe sync.py --login             # actually writes the tasks
```

`--list` checks the source side alone, so it works before Google is set up.
`--dry-run` reports what it would create/update without touching anything.
`--limit 1` writes only the soonest item — worth doing first against a new
account, because the app holds only the `tasks` scope and so can never report
*which* account you authorized. One task is a cheap way to confirm.

### Switching to a different Google account

```powershell
.\.venv\Scripts\python.exe switch_account.py --dry-run  # preview
.\.venv\Scripts\python.exe switch_account.py            # clean up + forget token
.\.venv\Scripts\python.exe sync.py --login --limit 1    # re-authorize, verify
.\.venv\Scripts\python.exe sync.py                      # full push
```

`switch_account.py` deletes the synced task list from the currently authorized
account and clears `token.json` + `state.json`; pass `--keep` to leave the
remote tasks alone. The new account must be added as a **Test user** on the
consent screen first, or Google returns `403 access_denied`.

### 4. Schedule it

```powershell
.\setup_schedule.ps1                  # every 2 hours + at logon
.\setup_schedule.ps1 -IntervalHours 1 # more often
.\setup_schedule.ps1 -Remove          # unregister
```

Because the job runs locally, it syncs whenever the PC is awake;
`StartWhenAvailable` makes it catch up after sleep or shutdown.

Registration details, since this machine's account is not an administrator:
the `Register-ScheduledTask` cmdlet returns **Access is denied** for a
non-elevated user, so the script falls back to `schtasks.exe`, which a standard
user may use for their own tasks. That fallback registers with `/IT`
("run only when this user is logged on"), which avoids storing a password —
the tradeoff is that the sync does not run at the login screen or while
signed out. Run the script from an elevated shell if you want it to sync
regardless of who is logged on.

## Achieve — enabled

Achieve has no public API and no calendar export, but its own front-end talks to
a JSON service, and that is what this uses. A saved browser profile
(`.achieve-profile`) holds the session cookies; requests then go straight to the
API through Playwright's request context, so there is no DOM scraping to break
when Macmillan restyles the UI.

Endpoints, confirmed against a live account:

| Endpoint | Use |
| --- | --- |
| `GET /lms/courses` | enrolled courses (`id`, `short_name` → `CHEM 1151`) |
| `GET /api/v1/courses/{id}/assignments` | assignments; `assignment_due_at` is the deadline |
| both on `cw-services-live.macmillanlearning.com` | 401 signed out, 200 signed in |

Only rows with a non-null `assignment_due_at` and no `deleted_at` become tasks —
for CHEM 1151 that is 13 of 51 rows; the rest is undated course content.
Deliberately **not** used: `/api/v3/calendars/weeks/get`, which returns
`Week 1…17` with an `endDate` and would otherwise import 17 phantom
"assignments".

Re-authenticating, when Achieve eventually logs the profile out:

```powershell
.\.venv\Scripts\python.exe achieve_login.py
```

That opens a real Chrome window, clears the Usercentrics consent overlay (via
its `UC_UI` API, then button clicks, then removing the node — on a fresh profile
it covers the Sign In button), and waits for `/lms/courses` to return 200 before
saving. It polls the API instead of reading the page, because the signed-out
landing page has no password field and a clean URL — a page-shape guess
"succeeds" immediately and saves an anonymous session.

Two limitations:

- The assignments endpoint carries **no submission state**, so Achieve items
  always arrive unticked. Tick them off yourself — the sync never re-opens a
  task you completed. (Canvas items do auto-tick on submission.)
- Achieve has no stable per-assignment deep link, so task notes link to the
  course.

If an assignment appears in both Canvas and Achieve, the Canvas copy wins, since
it knows your submission status.

## Google Calendar — enabled

Work that only ever lived on a calendar (the PHIL 2390 reading/screening
schedule) is pulled from the **NEU** calendar. A calendar also holds classes,
office hours and personal life, so nothing is imported unless its title or
description matches `include_pattern`:

```json
"google_calendar": {
  "enabled": true,
  "calendar_ids": ["NEU"],
  "include_pattern": "^Cults\\b",
  "course_label": "PHIL 2390",
  "all_day_due_time": "23:59"
}
```

- `calendar_ids` accepts a calendar's **name** or its id, plus `"primary"`;
  empty means every calendar you can see. Renaming the calendar in Google breaks
  the name match — the run logs `no calendar named/id 'NEU'` rather than failing.
- `^Cults\b` catches both `Cults reading — …` and `Cults — Watch: …`: 27 of the
  272 events in the window. Widen it (e.g. `reading|Watch:|podcast`) to cover
  another course's schedule, or set `exclude_pattern` to carve items out.
- The course is read off the start of the title (`PHIL 2390 reading: Ch 4` →
  `PHIL 2390`); titles that do not start with a course code get `course_label`.
  Only the lead is searched, or the citation in
  `… 'The Prophet Who Failed' (Harper's, July 2024)` would read as course
  `JULY 2024`.
- All-day events are due at `all_day_due_time` on the day; timed events at their
  start. Recurring events expand to one task per occurrence.
- Calendar has no concept of done, so these always arrive unticked, and the sync
  never re-opens one you ticked. Deleting the event removes the task — see the
  note below on how the PHIL readings were detached from that rule.

### The PHIL readings are now standalone

Tasks show up in Google Calendar too, so importing the reading events produced
each reading twice in the calendar view — once as the event, once as its task.
The 27 events were therefore deleted and their tasks kept.

That needed both halves at once. A task is tied to its event by a `gcal:<id>`
entry in `state.json`, and `delete_disappeared` prunes any task whose source
item is gone — so deleting the events alone would have made the next sync
delete all 27 tasks. Releasing those state entries in the same pass leaves the
tasks in place and simply unmanaged.

Consequences worth knowing:

- Those 27 tasks are yours now. The sync will not update, re-link or delete
  them, and `reading_links.json` no longer reaches them.
- One task had already been ticked off when the reading links went in, so it
  had been skipped (the sync never patches a task you completed) and still held
  its calendar-event URL, which died with the event. It was repointed at its
  Canvas PDF by hand.
- The source stays **enabled**, which is deliberate: a *new* `Cults …` event on
  the NEU calendar still becomes a task. But re-creating the deleted events
  would hand out fresh event ids and so produce a second copy of each reading
  alongside the standalone task.

### Reading links

`links_file` points at `reading_links.json`, transcribed from the syllabus
(pages 16–21). Each key is a case-insensitive regex matched against the event
title; the value is one URL or a list of them:

```json
"Short Creek E4": ["https://podcasts.apple.com/...", "https://open.spotify.com/..."]
```

The first URL becomes the task's link and replaces the calendar-event link,
which is the more useful thing to click; any others are listed beneath it.
Readings with no entry keep the calendar link. 13 of the 27 events are covered —
the other 14 are book chapters and journal articles the syllabus gives no link
for, because those PDFs are posted on Canvas.

Keys must be narrow enough to hit exactly one event: `Jonestown` alone would
also match the Chidester article, and `Let the Fire Burn` matches both the film
and the PBS follow-up page, so those use `Jonestown: The Life and Death` and
`Watch: Let the Fire Burn`. A missing or malformed file warns and syncs without
links rather than failing. The links came out of the PDF's **link annotations**,
not its visible text — several are anchored on a title, and the Hail, Satan!
link only works through `ezproxy.neu.edu`, which the printed URL omits.

This needs the `calendar.readonly` scope, which is why `gauth.py` requests both
scopes in one consent. **Adding a scope invalidates the stored token** — a
refresh silently returns the old scopes — so `credentials()` verifies the token
actually covers `SCOPES` and forces a fresh `--login` when it does not.

A calendar entry that mirrors a real assignment loses to the Canvas or Achieve
copy in dedup (same title + due date).

## MATH 2321 homework packets — enabled

The professor posts a homework sheet to the course Modules page that only
*names* the textbook problems:

```
Book problems:
  • §1.2 # 41, 42, 43
  • §1.3 #1-5, 9-13, 18, 19, 33, 34, 35
Additional problems:
  (1) Let P = (1, 2, 3) and Q = (−1, 3, 4) ...
```

which is not something you can sit down and work from. For each sheet posted,
`sources/mathhw.py` builds a packet PDF — those problems lifted out of the
textbook, then his own — uploads it to Drive, puts the link on a calendar
event, and makes a task. The sheets carry no due date, so the task lands at
**Friday 11:59 PM of the week it was posted** (`due_weekday`, Mon=0).

It runs inside the normal sync, so a newly posted sheet is picked up within two
hours with nothing to do by hand. Everything is recorded in `state.json`: a
sheet is built and uploaded once, and re-posting it (Canvas bumps `updated_at`)
rebuilds it in place, keeping the same Drive link and calendar event.

### How the problems are extracted

Problems are **clipped out of the textbook as vector regions**, not re-typeset
from extracted text — calculus notation does not survive text extraction, as
the professor's own sheet demonstrates:

| in the PDF | what extraction gives |
| --- | --- |
| $\vec{a}$ | `⃗ a` |
| $\overrightarrow{PQ}$ | `− − →P Q` |
| $\langle\sqrt3, -1\rangle$ | `⟨` `√` `3,−1⟩` (on three lines) |

Three things about the book made this fiddly, all handled in `packet.py`:

- **Mirrored margins.** On a recto page problem numbers right-align at x=96 and
  the marginal notes sit beyond x=480; on a verso page the whole text block
  shifts 85pt right and the notes move to the left edge. Hardcoding either one
  silently drops every other page, so the column is measured per page off the
  problem numbers themselves.
- **Reading order, not y order.** A radical sign is drawn *above* its own
  baseline, so sorted by y it lands before the problem number it belongs to.
  That put the `√7` of #44 inside #43's clip, and #43's clip then stretched
  down across #44. Walking the page in block/line order keeps each fragment
  with its problem.
- **The instruction paragraph.** "41. v = (3, 4)" alone does not say what to do
  with it; the instruction sits above the group ("determine the magnitude and
  direction"). Each problem remembers the paragraph governing it, printed once
  per group.

The exercise pages for a section come from the TOC, which nests chapter >
section > "Exercises" and numbers none of them. Position gives the number,
except front matter ("Preface") is also a top-level entry and would shift every
chapter by one — so only top-level entries that actually contain sections
count. When the page carries its own `1.2.1 Exercises` heading, that wins.

Anything not found is logged and listed in `state.json` under `missing` rather
than silently dropped.

### Permissions

This needs two scopes beyond the original `tasks` + `calendar.readonly`:

| scope | for |
| --- | --- |
| `drive.file` | storing the packet. Grants access **only to files this app creates**, not the rest of your Drive. |
| `calendar.events` | putting the packet link on the NEU calendar |

Adding a scope invalidates the stored token, so these are kept in
`gauth.EXTRA_SCOPES` and checked separately from `BASE_SCOPES`. A token granted
before they existed still runs Canvas, Achieve, Calendar and Tasks normally;
only the packets wait, logging what unblocks them. Grant them with:

```powershell
.\.venv\Scripts\python.exe sync.py --login
```

### Another course, or a different textbook

Point `math_homework` at it — `canvas_course_id`, `textbook`, and
`file_pattern` (which matches the posted file's name). The extraction assumes
the Massey layout: numbered problems in a single text column with the number
right-aligned in the margin. A two-column book would need `_classify` taught
about columns.

## Colour coding

One `courses` block in `config.json` is the single source of class identity,
used for both the calendar colour and the task marker so the two can never
disagree:

| class | calendar | hue | task |
| --- | --- | --- | --- |
| CHEM 1151 (incl. 1153 Recitation) | Tomato, `colorId` 11 | 358° | 🟥 |
| Office hours | Banana, `5` | 47° | *(no tasks)* |
| GE 1501 (incl. "Cornerstone …") | Basil, `10` | 116° | 🟩 |
| MATH 2321 | Blueberry, `9` | 221° | 🟦 |
| PHIL 2390 | Grape, `3` | 274° | 🟪 |
| anything else | left alone | — | ⬜ |

These are picked for **hue separation, not taste**: the closest pair is 48°
apart. Two of Google's eleven are deliberately unused — **Tangerine** (28°)
sits only 30° from Tomato and the two were genuinely hard to tell apart, and
**Peacock** (182°) is 39° from Blueberry. `test_logic.py` enforces a 40°
minimum against the live config, so a close pair cannot creep back in.

`match` is a regex tried against a calendar event title or a task's course
label. **Order matters — the first hit wins**, which is why office hours come
first: "Dr. R-S Office Hours" would otherwise be claimed by PHIL, since Dr. R-S
teaches it. Office hours are a category of their own and produce no tasks.

`colorize.py` runs at the end of every sync and sets each event's `colorId`,
touching only the ones still in the calendar's default colour, so a colour
picked by hand is never overwritten. It lists with `singleEvents=False`, colouring a
recurring series once instead of 35 identical instances.

Calendar **rate-limits writes per user**, and a batch is sent all at once: 50
at a time came back `rateLimitExceeded` for two thirds of them. Batches are 10,
with a pause between and a backoff retry, and anything still failing is left
for the next run rather than reported as done.

### Tasks cannot actually be coloured

The Google Tasks API has **no colour field** — not on a task, not on a task
list. So the colour lives in the title via `title_format`:

```json
"title_format": "{marker} [{course}] {title}"
```

Two consequences worth knowing:

- Changing `title_format` or a marker alters how a task is *rendered*, which
  the stored fingerprint does not cover — it hashes the source item, not the
  result. The target therefore also compares the live task's title and notes
  against what it would write, so a rendering change reaches tasks that already
  exist. This also restores a managed task that was edited by hand.
- Tasks the sync cannot reach keep whatever title they had: the 27 detached
  PHIL readings, and anything already ticked off (never patched, by design).
  Those were marked once by hand.

## Files

| Path | Role |
| --- | --- |
| `sync.py` | entry point; collects, dedupes, pushes |
| `sources/canvas.py` | Canvas REST API pull (paginated, submission-aware) |
| `sources/achieve.py` | Playwright session scraper for Achieve |
| `sources/gcal.py` | Google Calendar pull, filtered by title pattern |
| `reading_links.json` | syllabus reading URLs, keyed by event-title regex |
| `sources/mathhw.py` | MATH 2321: find sheets, upload packets, make the event |
| `packet.py` | clips textbook problems into a packet PDF (offline, runnable alone) |
| `packets/` | built packets; `packets/source/` caches the posted sheets |
| `courses.py` | decides which class a title or label belongs to |
| `colorize.py` | sets calendar event colours from that decision |
| `gauth.py` | shared Google OAuth (Tasks + Calendar) in one token |
| `targets/google_tasks.py` | idempotent Google Tasks upsert |
| `common.py` | config, state, normalized `Item` |
| `state.json` | assignment → task-id map (delete to force a clean rebuild) |
| `sync.log` | rolling log of every run |
| `test_logic.py` | offline tests for the sync logic (no credentials needed) |

## Troubleshooting

```powershell
Get-ScheduledTaskInfo -TaskName CanvasAchieveSync   # last run time + result
Get-Content .\sync.log -Tail 30
Start-ScheduledTask -TaskName CanvasAchieveSync     # force a run now
.\.venv\Scripts\python.exe test_logic.py            # verify logic still sound
```

- **401 from Canvas** — token revoked or expired; regenerate (step 1).
- **"non-interactive run" error** — Google refresh token was revoked; run
  `sync.py --login` once.
- **Achieve keeps asking for login** — SSO session expired; re-run
  `achieve_login.py`.
- **Wrong due dates** — check `timezone` in `config.json`.
