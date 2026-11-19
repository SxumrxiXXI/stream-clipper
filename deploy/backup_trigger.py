#!/usr/bin/env python3
"""A backup for GitHub's timer, run on the owner's own machine every half hour.

GitHub starts scheduled workflows on a best-effort basis: a start can be late
or never come. This script presses the button when that happens. The work
still runs on GitHub; this machine only has to be awake for a few seconds.

It starts
- the daily run, if it is past 10:25 and today's run has neither finished nor
  is under way,
- a round (the short run between daily runs), on the days the settings switch
  rounds on, if its time passed 5 to 60 minutes ago and GitHub has not started
  one since,
- the live watcher, on the days the settings switch it on, if none is running
  (each of its runs starts the next; this only mends a broken chain), and
- the post check, if a clip's slot passed more than ten minutes ago, nothing
  has confirmed it yet, and no check has started in the last 40 minutes.

The workflows protect themselves against a second start, so pressing twice
is harmless. Needs the GitHub CLI (`gh`), signed in. `--dry-run` only prints.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DRY = "--dry-run" in sys.argv or "--at" in sys.argv
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60).stdout


def say(text: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M}  {text}", flush=True)


def private_file(repo: str, path: str) -> str | None:
    out = gh("api", f"repos/{repo}/contents/{path}", "--jq", ".content")
    try:
        return base64.b64decode(out).decode("utf-8", "replace") if out.strip() else None
    except ValueError:
        return None


def runs(repo: str, workflow: str) -> list[dict]:
    try:
        return json.loads(gh("run", "list", "-R", repo, "--workflow", workflow, "--limit", "6",
                             "--json", "status,conclusion,createdAt,headBranch,event") or "[]")
    except ValueError:
        return []


def main() -> None:
    owner = gh("api", "user", "--jq", ".login").strip()
    if not owner:
        say("not signed in to GitHub (or offline); nothing done")
        return
    public, private = f"{owner}/stream-clipper", f"{owner}/stream-clipper-private"
    cfg = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    if "--at" in sys.argv:                     # for testing: pretend it is this time
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--at") + 1]).replace(tzinfo=tz)

    # ---- the daily run ------------------------------------------------------
    daily = runs(public, "daily.yml")
    under_way = any(r["status"] in ACTIVE and r.get("headBranch") == "main" for r in daily)
    log = private_file(private, f"data/logs/daily-{now:%Y-%m-%d}.log") or ""
    finished = "daily run finished" in log.splitlines()
    if finished:
        pass
    elif under_way:
        say("daily run is under way on GitHub")
    elif now.time() < dt.time(10, 25):
        pass                                   # give GitHub's own timer its chance first
    else:
        # At most three starts a day, so a run that keeps failing is not restarted for ever.
        stamp = Path.home() / "Library" / "Caches" / "stream-clipper-backup" / f"daily-{now:%Y-%m-%d}"
        tries = int(stamp.read_text()) if stamp.exists() else 0
        if tries >= 3:
            say("today's run was already started three times from here; leaving it")
        else:
            say("GitHub has not run today: starting the daily run" + (" (dry run, not started)" if DRY else ""))
            if not DRY:
                gh("workflow", "run", "daily.yml", "-R", public, "--ref", "main",
                   "-f", "dry_run=false", "-f", "force=false", "-f", "state_branch=main")
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(str(tries + 1))

    # ---- rounds --------------------------------------------------------------
    start, until = str(cfg.get("rounds_start") or ""), str(cfg.get("rounds_until") or "")
    if start and start <= f"{now:%Y-%m-%d}" <= (until or start):
        for text in cfg.get("round_times") or []:
            due_at = dt.datetime.combine(now.date(), dt.time.fromisoformat(str(text)), tz)
            if not dt.timedelta(minutes=5) <= now - due_at < dt.timedelta(minutes=60):
                continue
            # Any run of the workflow on main since shortly before the round's time
            # counts: GitHub's own start, or one made from here earlier.
            since = any(r.get("headBranch") == "main" and
                        dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) > due_at - dt.timedelta(minutes=10)
                        for r in daily)
            stamp = Path.home() / "Library" / "Caches" / "stream-clipper-backup" / f"round-{due_at:%Y-%m-%d-%H%M}"
            if since or under_way or stamp.exists():
                continue
            say(f"GitHub has not started the {due_at:%H:%M} round: starting it" + (" (dry run, not started)" if DRY else ""))
            if not DRY:
                gh("workflow", "run", "daily.yml", "-R", public, "--ref", "main",
                   "-f", "round=true", "-f", "dry_run=false", "-f", "force=false", "-f", "state_branch=main")
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text("started")

    # ---- the live watcher ---------------------------------------------------
    start, until = str(cfg.get("live_start") or ""), str(cfg.get("live_until") or "")
    today = f"{now:%Y-%m-%d}"
    if cfg.get("live") and (not start or start <= today) and (not until or today <= until):
        watchers = [r for r in runs(public, "live.yml") if r.get("headBranch") == "main"]
        going = any(r["status"] in ACTIVE for r in watchers)
        just = any(dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) > now - dt.timedelta(minutes=20)
                   for r in watchers)
        if not going and not just:
            say("the live watcher is not running: starting it" + (" (dry run, not started)" if DRY else ""))
            if not DRY:
                gh("workflow", "run", "live.yml", "-R", public, "--ref", "main",
                   "-f", "dry_run=false", "-f", "chain=true", "-f", "hours=5", "-f", "state_branch=main")

    # ---- the post check -----------------------------------------------------
    try:
        queue = json.loads(private_file(private, "data/queue.json") or "[]")
    except ValueError:
        queue = []
    due = [i["id"] for i in queue if i.get("status") == "scheduled" and i.get("scheduled_for")
           and i.get("checked") not in ("ok", "partial")
           and dt.datetime.fromisoformat(i["scheduled_for"]) < now - dt.timedelta(minutes=10)]
    if due:
        checks = runs(public, "check.yml")
        recent = any(r["status"] in ACTIVE or
                     dt.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) > now - dt.timedelta(minutes=40)
                     for r in checks)
        if not recent and not under_way:
            say(f"{len(due)} post(s) past their slot and not confirmed: starting the check"
                + (" (dry run, not started)" if DRY else ""))
            if not DRY:
                gh("workflow", "run", "check.yml", "-R", public, "--ref", "main")


if __name__ == "__main__":
    main()
