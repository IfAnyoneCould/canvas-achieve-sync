"""Entry point: pull coursework from Canvas + Achieve, push into Google Tasks.

  python sync.py              # normal (scheduled) run
  python sync.py --login      # interactive: authorize Google, then sync
  python sync.py --dry-run    # show what would change, touch nothing
  python sync.py --verbose    # debug logging
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from common import Item, load_config, load_state, save_state, setup_logging
from sources import canvas as canvas_src
from targets import google_tasks


def collect(
    cfg: dict, log, state: dict | None = None, interactive: bool = False
) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    problems: list[str] = []
    state = {} if state is None else state

    if cfg.get("canvas", {}).get("enabled", True):
        try:
            items += canvas_src.fetch(cfg, log)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
            problems.append(f"canvas: {e}")
            log.error("Canvas source failed: %s", e)

    if cfg.get("google_calendar", {}).get("enabled", False):
        from sources import gcal as gcal_src

        try:
            items += gcal_src.fetch(cfg, log, interactive=interactive)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            problems.append(f"calendar: {e}")
            log.error("Calendar source failed: %s", e)

    if cfg.get("math_homework", {}).get("enabled", False):
        from sources import mathhw as mathhw_src

        try:
            items += mathhw_src.fetch(cfg, log, state, interactive=interactive)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            problems.append(f"math homework: {e}")
            log.error("Math homework source failed: %s", e)

    if cfg.get("achieve", {}).get("enabled", False):
        from sources import achieve as achieve_src

        try:
            items += achieve_src.fetch(cfg, log)
        except achieve_src.AchieveNeedsLogin as e:
            problems.append(f"achieve: {e}")
            log.warning("Achieve needs a fresh login: %s", e)
        except Exception as e:  # noqa: BLE001
            problems.append(f"achieve: {e}")
            log.error("Achieve source failed: %s", e)

    return items, problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync Canvas + Achieve coursework to Google Tasks")
    ap.add_argument("--login", action="store_true", help="allow interactive OAuth prompts")
    ap.add_argument("--dry-run", action="store_true", help="report changes without applying")
    ap.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="print the coursework found and exit (no Google credentials needed)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="sync only the N soonest items (smoke-test a new account before a full push)",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    log = setup_logging(args.verbose)
    cfg = load_config()
    state = load_state()

    items, problems = collect(cfg, log, state, interactive=args.login)

    # Deduplicate: the same assignment can surface from more than one source
    # when a course pipes Achieve deadlines into Canvas, or when a calendar
    # entry mirrors an assignment. Canvas wins because it knows submission
    # status; a hand-made calendar entry loses to a real assignment record.
    priority = {"canvas": 0, "achieve": 1, "gcal": 2}
    by_key: dict[tuple[str, str], Item] = {}
    deduped: list[Item] = []
    for item in sorted(items, key=lambda i: priority.get(i.source, 9)):
        key = (item.title.lower().strip(), item.due.date().isoformat() if item.due else "")
        if key in by_key:
            log.debug("dropping duplicate %s (%s)", item.title, item.source)
            continue
        by_key[key] = item
        deduped.append(item)

    if not deduped:
        log.warning("No coursework found. Nothing to sync.")
        if problems:
            return 1
        return 0

    if args.limit > 0:
        deduped = sorted(
            deduped, key=lambda i: i.due or datetime.max.replace(tzinfo=timezone.utc)
        )[: args.limit]
        # Pruning is suppressed: everything else is still live upstream, it is
        # just being held back from this run.
        cfg.setdefault("google_tasks", {})["delete_disappeared"] = False
        log.info("--limit %d: syncing only the soonest item(s)", args.limit)

    log.info("%d item(s) to sync", len(deduped))

    if args.list_only:
        tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
        for it in sorted(deduped, key=lambda i: i.due or datetime.max.replace(tzinfo=timezone.utc)):
            when = it.due.astimezone(tz).strftime("%a %b %d  %I:%M %p") if it.due else "no due date"
            mark = "x" if it.completed else " "
            print(f"  [{mark}] {when}  {it.course:<12} {it.title}")
        return 1 if problems else 0

    if cfg.get("google_tasks", {}).get("enabled", True):
        stats = google_tasks.sync(
            deduped, cfg, state, log, interactive=args.login, dry_run=args.dry_run
        )
        log.info(
            "Google Tasks: %d created, %d updated, %d completed, %d removed, %d unchanged",
            stats["created"],
            stats["updated"],
            stats["completed"],
            stats["removed"],
            stats["unchanged"],
        )

    # Last, and never fatal: the tasks are the point of the run, and a calendar
    # the colourer could not reach is only cosmetic.
    if cfg.get("calendar_colors", {}).get("enabled", False) and not args.dry_run:
        from colorize import apply_colors

        try:
            apply_colors(cfg, log, interactive=args.login)
        except Exception as e:  # noqa: BLE001
            problems.append(f"calendar colours: {e}")
            log.error("Calendar colouring failed: %s", e)

    if not args.dry_run:
        save_state(state)

    if problems:
        log.warning("finished with %d problem(s): %s", len(problems), "; ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
