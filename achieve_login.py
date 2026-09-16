"""Seeds the saved Achieve browser profile, or the password behind it.

    achieve_login.py                  open a Chrome window and sign in by hand
    achieve_login.py --save-password  store the password for unattended sign-in
    achieve_login.py --report         show how long the saved session has left

Achieve's session cookies last 30 minutes, so hand-seeding the profile cannot
keep a 2-hourly sync signed in - use --save-password once and the sync signs
itself in whenever it finds a 401. The password goes to Windows Credential
Manager; the browser profile only ever holds cookies.

It polls the Achieve API for a real 200 rather than guessing from the page, so
it works when launched by a tool with no stdin: log in, and it saves and exits
on its own. Closing the window also saves.
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import sys
import threading
import time
from pathlib import Path

from common import ROOT, load_config, setup_logging
from sources.achieve import (
    PROFILE_DIR,
    clear_signin_block,
    dismiss_consent,
    is_authenticated,
    save_password,
    session_report,
    sign_in,
)

WAIT_SECONDS = 600


def report(log) -> None:
    rows = session_report()
    if not rows:
        log.info("No session cookies saved yet - run this script with no arguments.")
        return
    for name, set_at, expires in rows:
        log.info("%-16s set %s  expires %s", name, set_at, expires)
    log.info("Achieve slides these forward 30 minutes on every request.")


PROMPT_TIMEOUT_SECONDS = 120


def read_password(from_stdin: bool, log) -> str:
    """The password, without putting it in argv - where a process listing would
    show it - or in a shell history line.

    A console read is the right way to do that, but it cannot be trusted to
    return: `sys.stdin.isatty()` reports True under some tool harnesses even
    when stdin is redirected and nothing can be typed, and Windows getpass()
    reads the console directly rather than stdin, so it blocks forever with an
    empty screen. Hence the deadline, and hence --password-stdin.
    """
    if from_stdin:
        return sys.stdin.read().strip()

    print(
        f"Achieve password (not echoed) - type it and press Enter "
        f"(giving up in {PROMPT_TIMEOUT_SECONDS}s):",
        flush=True,
    )
    box: dict[str, str] = {}

    def ask() -> None:
        try:
            box["pw"] = getpass.getpass("")
        except Exception as e:  # noqa: BLE001 - no console, or it went away
            box["err"] = str(e)

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    thread.join(PROMPT_TIMEOUT_SECONDS)

    if box.get("pw"):
        return box["pw"].strip()
    if box.get("err"):
        log.error("Could not read from the console (%s).", box["err"])
    else:
        log.error(
            "Nothing typed in %ds - this is probably running somewhere with no "
            "keyboard attached.",
            PROMPT_TIMEOUT_SECONDS,
        )
    log.error("Two ways round it:")
    log.error("  1. run the same command in a terminal window of your own, or")
    log.error("  2. add --password-stdin and pipe the password in.")
    return ""


def save_credentials(
    cfg: dict, base: str, log, email_arg: str | None = None, password_stdin: bool = False
) -> int:
    """Prompt for the Achieve login and keep the password in Credential Manager."""
    acfg = cfg.get("achieve", {})
    email = (email_arg or "").strip() or str(acfg.get("email") or "").strip()
    if not email_arg and sys.stdin.isatty():
        prompt = f"Achieve email [{email}]: " if email else "Achieve email: "
        email = input(prompt).strip() or email
    if not email:
        log.error("No email given.")
        return 1

    password = read_password(password_stdin, log)
    if not password:
        log.error("No password given - nothing was saved.")
        return 1

    # Test before storing. A password that only ever gets tried at 2am, in a
    # hidden window, on a 2-hourly schedule, is how an account gets locked out.
    from playwright.sync_api import sync_playwright

    log.info("Checking that email and password against Achieve...")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ok = sign_in(page, base, email, password, log)
        finally:
            ctx.close()

    if not ok:
        log.error("Achieve rejected that login - nothing was saved.")
        log.error("If the reason above is 'email or password incorrect', check which")
        log.error("address the account is actually under before retrying - a run of")
        log.error("refusals is what gets an account locked.")
        return 1

    save_password(email, password)
    log.info("Password stored in Windows Credential Manager.")
    clear_signin_block()

    # Persist the email so unattended runs know which credential to ask for.
    path = Path(ROOT) / "config.json"
    raw = json.load(io.open(path, encoding="utf-8"))
    if str(raw.get("achieve", {}).get("email") or "") != email:
        raw.setdefault("achieve", {})["email"] = email
        io.open(path, "w", encoding="utf-8", newline=chr(10)).write(
            json.dumps(raw, indent=2, ensure_ascii=False) + chr(10)
        )
        log.info("config.json: achieve.email = %s", email)

    log.info("Signed in. The sync will now recover on its own whenever it sees a 401.")
    report(log)
    return 0



def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed the Achieve session, or the password behind it."
    )
    ap.add_argument(
        "--save-password",
        action="store_true",
        help="store the Achieve password in Windows Credential Manager and test it",
    )
    ap.add_argument(
        "--report", action="store_true", help="show the saved session's cookie lifetimes"
    )
    ap.add_argument(
        "--email",
        default=None,
        help="Achieve login address, so --save-password only has to prompt for the password",
    )
    ap.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting (for pipes and wrappers)",
    )
    args = ap.parse_args()

    log = setup_logging()
    cfg = load_config()
    base = cfg.get("achieve", {}).get("base_url", "https://achieve.macmillanlearning.com")

    if args.report:
        report(log)
        return 0
    if args.save_password:
        return save_credentials(cfg, base, log, args.email, args.password_stdin)

    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1400, "height": 1000},
        )
        closed = {"flag": False}
        ctx.on("close", lambda _=None: closed.__setitem__("flag", True))

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(base, wait_until="domcontentloaded")
        dismiss_consent(page, log)

        if is_authenticated(page):
            log.info("This profile is already signed in to Achieve.")
            ctx.close()
            return 0

        log.info("A Chrome window is open. Sign in to Achieve.")
        log.info("Waiting for the Achieve API to accept the session (up to %ds)...", WAIT_SECONDS)

        deadline = time.time() + WAIT_SECONDS
        saved = False
        announced = False

        while time.time() < deadline and not closed["flag"]:
            try:
                if is_authenticated(page):
                    saved = True
                    break
                # The overlay can come back on the sign-in host, so clear it again.
                if page.query_selector('[id*="usercentrics"]'):
                    dismiss_consent(page, log)
                if not announced and page.query_selector("input[type=password]"):
                    log.info("Sign-in form reached; finish logging in.")
                    announced = True
            except PWError:
                break  # window went away; the profile is already on disk
            time.sleep(3)

        if not closed["flag"]:
            try:
                ctx.close()  # flushes cookies to the profile directory
            except PWError:
                pass

    if saved:
        log.info("Signed in - the Achieve API accepted the session.")
        log.info("Session saved to %s", PROFILE_DIR)
        report(log)
        log.info("That expiry is why --save-password exists: 30 minutes from now")
        log.info("the scheduled sync will be back to a 401 unless it can sign in.")
        return 0

    reason = "the window was closed" if closed["flag"] else f"timed out after {WAIT_SECONDS}s"
    log.warning("Could not confirm a signed-in session (%s).", reason)
    log.warning("Re-run this script and complete the login before syncing.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
