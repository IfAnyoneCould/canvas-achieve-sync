"""Macmillan Achieve source.

Achieve has no public student API and no ICS export, but its own front-end talks
to a JSON service (`cw-services-live.macmillanlearning.com`). This uses a saved
browser profile purely to hold the session cookies, then calls that service
directly through Playwright's request context - no DOM scraping, so a UI
redesign does not break it.

Endpoints (confirmed against a live account):
  GET /lms/courses                        -> enrolled courses
  GET /api/v1/courses/{course_id}/assignments -> assignments, incl. due dates

The profile is seeded once by `achieve_login.py`; after that, runs are headless
and unattended.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from datetime import timedelta
from typing import Any

from dateutil import parser as dateparser

from common import ROOT, Item, now_utc

PROFILE_DIR = ROOT / ".achieve-profile"
DEBUG_DIR = ROOT / "debug"

# The Achieve SPA talks to this service. Signed out it answers 401, signed in
# 200, which makes it a precise auth probe - far better than guessing from the
# page, since the landing page has no password field and a clean URL.
API_BASE = "https://cw-services-live.macmillanlearning.com"
COURSES_URL = f"{API_BASE}/lms/courses?offset=0&limit=101&options=%7B%7D"
ASSIGNMENTS_URL = API_BASE + "/api/v1/courses/{course_id}/assignments"

_SUBJECT_NUM = re.compile(r"^([A-Za-z]{2,6})[ _-]?(\d{3,4})")


class AchieveNeedsLogin(RuntimeError):
    """The saved browser profile is no longer authenticated."""


def is_authenticated(page) -> bool:
    """True when the browser's cookies are good for the Achieve API."""
    try:
        return page.request.get(COURSES_URL, timeout=20_000).status == 200
    except Exception:  # noqa: BLE001 - offline / navigating / window closed
        return False


# Macmillan fronts the site with the Usercentrics consent manager. On a fresh
# profile its overlay covers the page - including the Sign In button - and it
# lives in a shadow root, so it has to be dealt with before anything else.
_UC_API_JS = """
(async () => {
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    const ui = window.UC_UI;
    if (ui && (!ui.isInitialized || ui.isInitialized())) {
      try { await ui.acceptAllConsents(); } catch (e) {}
      try { await ui.closeCMP(); } catch (e) {}
      return 'uc-api';
    }
    await new Promise(r => setTimeout(r, 250));
  }
  return 'no-uc-api';
})()
"""

_UC_NUKE_JS = """
(() => {
  const el = document.getElementById('usercentrics-root')
          || document.querySelector('[id*="usercentrics"], [class*="usercentrics"]');
  if (el) { el.remove(); return 'removed'; }
  return 'nothing-to-remove';
})()
"""

_ACCEPT_SELECTORS = (
    '[data-testid="uc-accept-all-button"]',
    "#uc-center-container button.uc-accept-all-button",
    'button:has-text("Accept All")',
    'button:has-text("Accept all")',
    'button:has-text("Agree and continue")',
    'button:has-text("Accept")',
)


def dismiss_consent(page, logger: logging.Logger) -> None:
    """Accept/close the consent overlay so the page becomes clickable."""
    try:
        logger.debug("consent: UC_UI -> %s", page.evaluate(_UC_API_JS))
    except Exception as e:  # noqa: BLE001
        logger.debug("consent: UC_UI call failed (%s)", e)

    for sel in _ACCEPT_SELECTORS:
        try:
            btn = page.query_selector(sel)  # Playwright pierces open shadow roots
            if btn and btn.is_visible():
                btn.click(timeout=5_000)
                logger.debug("consent: clicked %s", sel)
                page.wait_for_timeout(700)
                break
        except Exception:  # noqa: BLE001 - selector unsupported or detached
            continue

    # Last resort: if the overlay is still there, take it out of the DOM so it
    # cannot intercept clicks on the real page.
    try:
        if page.query_selector('[id*="usercentrics"]') is not None:
            logger.debug("consent: nuke -> %s", page.evaluate(_UC_NUKE_JS))
    except Exception:  # noqa: BLE001
        pass


# --- Signing itself back in ---------------------------------------------------
#
# The cookies Achieve issues (`ayo_achieve`, `ayo_Courseware`) live **30
# minutes** and slide forward on each request. A profile seeded by hand is
# therefore always dead by the time the next 2-hourly run starts - there was
# never a "log in once a day" version of this. Macmillan's IAM is a plain
# email/password form (no SSO redirect, no captcha, and no "keep me signed in"
# to tick), so the sync signs itself back in instead of asking.
#
# The password lives in Windows Credential Manager via keyring - not in
# config.json, not in the browser profile. Seed it once with:
#     .venv/Scripts/python.exe achieve_login.py --save-password

KEYRING_SERVICE = "canvas-achieve-sync:achieve"
IAM_LOGIN_URL = "https://iam.macmillanlearning.com/uam/login?retURL={base}/courses"
SIGNIN_WAIT_SECONDS = 25

# A rejected password must stop the sync trying again. Unattended runs happen
# every 2 hours, and a login endpoint answering "incorrect" 12 times a day is
# how an account gets locked. This file is the breaker: written when a sign-in
# is refused, cleared only by a --save-password that actually worked.
SIGNIN_BLOCK = ROOT / ".achieve-signin-blocked"


def block_signin(email: str, reason: str) -> None:
    stamp = f"{now_utc():%Y-%m-%d %H:%M:%S} UTC"
    SIGNIN_BLOCK.write_text(" | ".join([stamp, email, reason]), encoding="utf-8")


def clear_signin_block() -> None:
    SIGNIN_BLOCK.unlink(missing_ok=True)


def signin_blocked() -> str:
    """The recorded refusal, or '' when sign-in is allowed to run."""
    try:
        return SIGNIN_BLOCK.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def stored_password(email: str) -> str | None:
    """The saved Achieve password, or None if keyring has nothing for it."""
    if not email:
        return None
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, email)
    except Exception as e:  # noqa: BLE001 - a locked or missing vault
        logging.getLogger(__name__).debug("keyring lookup failed (%s)", e)
        return None


def save_password(email: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, email, password)


def sign_in(page, base: str, email: str, password: str, logger: logging.Logger) -> bool:
    """Drive the IAM form. True once the API accepts the resulting session."""
    page.goto(IAM_LOGIN_URL.format(base=base), wait_until="domcontentloaded", timeout=60_000)
    dismiss_consent(page, logger)
    try:
        # Both inputs are name="mlinput"; the ids are what distinguish them.
        page.fill("#email", email, timeout=15_000)
        page.fill("#password", password, timeout=15_000)
        page.click("#signin", timeout=15_000)
    except Exception as e:  # noqa: BLE001 - form moved or never rendered
        logger.warning("Achieve: could not drive the sign-in form (%s)", e)
        return False

    # The form posts and the SPA routes on its own; the API probe is the only
    # trustworthy answer, the same way achieve_login.py decides.
    for _ in range(SIGNIN_WAIT_SECONDS):
        page.wait_for_timeout(1_000)
        if is_authenticated(page):
            return True

    # Macmillan renders the reason on the page ("The email or password you
    # entered is incorrect"), which distinguishes a bad password from a form
    # that has changed or a bot check. Worth having in the log.
    said = ""
    for sel in ('[role="alert"]', '[class*="rror"]', '[id*="rror"]'):
        try:
            el = page.query_selector(sel)
            if el:
                said = (el.inner_text() or "").strip()
                if said:
                    break
        except Exception:  # noqa: BLE001 - detached node
            continue
    if said:
        logger.warning("Achieve: sign-in refused - the page says: %s", said[:160])
    else:
        logger.warning("Achieve: still 401 after submitting the form (at %s)", page.url[:90])
    return False


def session_report() -> list[tuple[str, str, str]]:
    """(cookie, set_at_utc, expires_utc) for the profile's Achieve cookies.

    Read from Chromium's cookie DB, which is the only place the real session
    lifetime is visible. Copied first: the live file is locked while a browser
    holds the profile.
    """
    db = PROFILE_DIR / "Default" / "Network" / "Cookies"
    if not db.exists():
        return []
    import shutil
    import sqlite3
    import tempfile
    from datetime import datetime

    tmp = Path(tempfile.gettempdir()) / "achieve-cookies-report.sqlite"
    shutil.copy2(db, tmp)

    def when(v: int) -> str:
        if not v:
            return "session"
        return (datetime(1601, 1, 1) + timedelta(microseconds=v)).strftime("%Y-%m-%d %H:%M UTC")

    try:
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "select name, last_update_utc, expires_utc from cookies "
            "where host_key like '%macmillan%' order by expires_utc desc"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    # The _ga* pair is analytics with a year on it and says nothing about auth.
    return [(n, when(u), when(e)) for n, u, e in rows if not n.startswith("_ga")]


def _get_json(page, url: str, logger: logging.Logger) -> Any:
    try:
        r = page.request.get(url, timeout=30_000)
    except Exception as e:  # noqa: BLE001
        logger.warning("Achieve: request failed %s (%s)", url[:80], e)
        return None
    if r.status != 200:
        logger.warning("Achieve: HTTP %d for %s", r.status, url[:80])
        return None
    try:
        return r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("Achieve: non-JSON body from %s (%s)", url[:80], e)
        return None


def _course_label(course: dict, overrides: dict[str, str]) -> str:
    """'CHEM 1151 Fall 26 - General Chemistry...' -> 'CHEM 1151'."""
    name = (course.get("name") or "").strip()
    short = (course.get("short_name") or "").strip()

    for key in (str(course.get("id")), short, name):
        if key and key in overrides:
            return overrides[key]

    if short:
        return short[:24]
    m = _SUBJECT_NUM.match(name)
    if m:
        return f"{m.group(1).upper()} {m.group(2)}"
    return (name or f"achieve-{course.get('id')}")[:24]


def _as_dt(value: Any):
    if not value or not isinstance(value, str):
        return None
    try:
        return dateparser.isoparse(value)
    except (ValueError, OverflowError, TypeError):
        return None


def parse_assignments(rows, label, base, cid, lo, hi) -> list[Item]:
    """Map the /assignments payload to items, dropping what is not coursework."""
    out: list[Item] = []
    if not isinstance(rows, list):
        return out

    for a in rows:
        if not isinstance(a, dict) or a.get("deleted_at"):
            continue
        # Only ~a quarter of rows are graded work with a deadline; the rest is
        # undated course content, which is not a todo.
        due = _as_dt(a.get("assignment_due_at"))
        if due is None or not (lo <= due <= hi):
            continue

        out.append(
            Item(
                uid=f"achieve:{a.get('id')}",
                source="achieve",
                course=label,
                title=(a.get("name") or "Untitled assignment").strip(),
                due=due,
                # Achieve has no stable per-assignment deep link, so point at
                # the course and let the app route.
                url=f"{base}/courses/{cid}",
                # This endpoint carries no submission state, so items arrive
                # open; ticking one off by hand sticks, because the target never
                # re-opens a user-completed task.
                completed=False,
                extra={"points": a.get("assigned_points"), "tool": a.get("tool")},
            )
        )
    return out


def fetch(cfg: dict, logger: logging.Logger) -> list[Item]:
    from playwright.sync_api import sync_playwright

    acfg = cfg.get("achieve", {})
    base = acfg.get("base_url", "https://achieve.macmillanlearning.com").rstrip("/")
    if not PROFILE_DIR.exists():
        raise AchieveNeedsLogin("No saved Achieve session. Run: python achieve_login.py")

    window = cfg.get("window", {})
    lo = now_utc() - timedelta(days=window.get("past_days", 3))
    hi = now_utc() + timedelta(days=window.get("future_days", 120))
    overrides = {str(k): str(v) for k, v in (acfg.get("course_names") or {}).items()}

    items: list[Item] = []
    dump: dict[str, Any] = {}

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=acfg.get("headless", True),
            viewport={"width": 1400, "height": 1000},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(base, wait_until="domcontentloaded", timeout=60_000)
            dismiss_consent(page, logger)

            if not is_authenticated(page):
                email = str(acfg.get("email") or "").strip()
                password = stored_password(email)
                if not password:
                    raise AchieveNeedsLogin(
                        "Achieve session expired (API returned 401) and no password is "
                        "saved for unattended sign-in. Run: "
                        "achieve_login.py --save-password"
                    )
                blocked = signin_blocked()
                if blocked:
                    raise AchieveNeedsLogin(
                        "Achieve refused the saved password once already, so this is "
                        f"not retrying (that is how accounts get locked): {blocked}. "
                        "Fix it with: achieve_login.py --save-password"
                    )
                logger.info("Achieve: session expired; signing in as %s", email)
                if not sign_in(page, base, email, password, logger):
                    block_signin(email, "sign-in refused during a scheduled run")
                    raise AchieveNeedsLogin(
                        "Achieve sign-in with the saved password failed - password "
                        "changed, or the form did. Not retrying until it is re-saved. "
                        "Run: achieve_login.py --save-password"
                    )
                clear_signin_block()
                logger.info("Achieve: signed back in unattended")

            courses = _get_json(page, COURSES_URL, logger) or []
            if not isinstance(courses, list):
                courses = []
            dump["courses"] = courses

            active = [
                c
                for c in courses
                if isinstance(c, dict) and not c.get("deleted_at") and c.get("status") != "inactive"
            ]
            logger.info("Achieve: %d active course(s)", len(active))

            for course in active:
                cid = course.get("id")
                label = _course_label(course, overrides)
                rows = _get_json(page, ASSIGNMENTS_URL.format(course_id=cid), logger) or []
                dump[f"assignments:{label}"] = rows
                if not isinstance(rows, list):
                    continue

                found = parse_assignments(rows, label, base, cid, lo, hi)
                items.extend(found)
                logger.info(
                    "Achieve: %s -> %d dated assignment(s) of %d row(s)",
                    label,
                    len(found),
                    len(rows) if isinstance(rows, list) else 0,
                )
        finally:
            ctx.close()

    if acfg.get("debug_dump", False) and dump:
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / f"achieve-{now_utc():%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(dump, indent=2, default=str)[:8_000_000], encoding="utf-8")
        logger.debug("Achieve: raw payloads -> %s", path.name)

    logger.info("Achieve: %d assignment(s) in window", len(items))
    return items
