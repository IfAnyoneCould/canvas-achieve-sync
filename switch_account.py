"""Un-links the currently authorized Google account so you can sync to another.

  python switch_account.py            # remove the synced list from the current
                                      # account, then forget its token + state
  python switch_account.py --keep     # leave the remote tasks alone, just forget
                                      # the local token + state
  python switch_account.py --dry-run  # show what would happen

Afterwards run `python sync.py --login` and pick the correct account. Remember
that the new account must also be listed as a Test user on the OAuth consent
screen, or Google returns 403 access_denied.
"""

from __future__ import annotations

import argparse

from common import STATE_PATH, load_config, setup_logging
from targets import google_tasks as gt


def main() -> int:
    ap = argparse.ArgumentParser(description="Unlink the current Google account")
    ap.add_argument("--keep", action="store_true", help="do not delete the remote task list")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log = setup_logging()
    cfg = load_config()
    list_name = cfg.get("google_tasks", {}).get("list_name", "Coursework")

    if not gt.TOKEN_PATH.exists():
        log.info("No saved Google token; nothing to unlink.")
    elif args.keep:
        log.info("Leaving remote tasks in place (--keep).")
    else:
        # Delete the whole list: this app created it, and it holds only synced
        # coursework, so removing it takes the 41-odd tasks with it.
        try:
            svc = gt._service(interactive=False, logger=log)
            target_id = None
            page = None
            while True:
                resp = svc.tasklists().list(maxResults=100, pageToken=page).execute()
                for tl in resp.get("items", []):
                    if tl["title"] == list_name:
                        target_id = tl["id"]
                page = resp.get("nextPageToken")
                if not page:
                    break

            if target_id is None:
                log.info("No list named %r in the authorized account.", list_name)
            elif args.dry_run:
                log.info("[dry-run] would delete task list %r (%s)", list_name, target_id)
            else:
                svc.tasklists().delete(tasklist=target_id).execute()
                log.info("Deleted task list %r from the authorized account.", list_name)
        except Exception as e:  # noqa: BLE001 - token may be revoked already
            log.warning("Could not clean up the remote list (%s).", e)
            log.warning("Delete the %r list by hand in Google Tasks if it lingers.", list_name)

    for path, label in ((gt.TOKEN_PATH, "Google token"), (STATE_PATH, "sync state")):
        if not path.exists():
            continue
        if args.dry_run:
            log.info("[dry-run] would delete %s (%s)", label, path.name)
        else:
            path.unlink()
            log.info("Removed %s (%s)", label, path.name)

    if not args.dry_run:
        log.info("Next: python sync.py --login   (pick the correct account)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
