#!/usr/bin/env python3
"""The whole pipeline, once a day.

    .venv/bin/python src/daily.py            # run it now
    .venv/bin/python src/daily.py --no-discover   # skip the hunt for new streamers

1. Discover: which categories and streamers are hot on Twitch right now. New
   names land on the automatic watchlist as `watching` (never clipped until you
   mark them approved in config/streamers.yaml).
2. Scan + make: rank the best moments of every streamer on the watchlist and
   turn the top ones from APPROVED streamers into clips, with captions and
   hashtags, added to the posting queue.
3. Plan: give the new clips their time slots.
4. Send: schedule them on the platforms, only if auto_post is true in
   config/posting.yaml. Otherwise it stops here and tells you what is waiting.
5. Stats: pull views for what was posted earlier.
6. Clean up: delete clip folders that are posted and live, and old working
   files (src/cleanup.py).

Everything is logged to data/logs/daily-<date>.log.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
sys.path.insert(0, str(ROOT / "src"))
import progress  # noqa: E402


QUIET = False      # --quiet: details go to the log file only (the console may be public)


def step(log, title: str, cmd: list[str]) -> int:
    started = dt.datetime.now()
    line = f"\n===== {title} ({started:%H:%M:%S}) ====="
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for out in p.stdout:
        log.write(out)
        if not QUIET and "it/s]" not in out:
            print(out, end="", flush=True)
    rc = p.wait()
    took = f"      took {(dt.datetime.now() - started).total_seconds() / 60:.1f} min, exit {rc}"
    log.write(took + "\n")
    if QUIET:
        print(took, flush=True)
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-discover", action="store_true")
    ap.add_argument("--clips", type=int, help="clips to make (default: clips_per_day in config/posting.yaml)")
    ap.add_argument("--force", action="store_true", help="run even if today's run already finished")
    ap.add_argument("--quiet", action="store_true",
                    help="print only step names, times and exit codes; everything else goes to the log file")
    a = ap.parse_args()
    global QUIET
    QUIET = a.quiet

    # One run at a time, and one finished run a day: a manual run and the 10:00
    # timer must never work on the queue together or double the day's clips.
    # A held file lock, not a process search: a search also matches the
    # keep-awake wrapper and any shell whose command line mentions this script.
    (ROOT / "data").mkdir(exist_ok=True)
    lock = open(ROOT / "data" / "daily.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("another daily run is in progress; not starting a second one")
    today_log = ROOT / "data" / "logs" / f"daily-{dt.date.today()}.log"
    if not a.force and today_log.exists() and "daily run finished" in today_log.read_text():
        sys.exit("today's run already finished (use --force to run again)")

    cfg = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    if os.environ.get("CLIP_DRY_RUN"):       # a trial: make and plan, send nothing
        cfg["auto_post"] = False
    n = a.clips or cfg.get("clips_per_day", 4)
    logs = ROOT / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    scan, post = str(ROOT / "src" / "scan.py"), str(ROOT / "src" / "publish" / "post.py")

    with open(logs / f"daily-{dt.date.today()}.log", "a") as log:
        failed = []
        if not a.no_discover:
            if step(log, "1/5 discover", [PY, "-u", scan, "discover", "--add", "10"]):
                failed.append("discover")
        if step(log, "2/5 scan and make clips", [PY, "-u", scan, "--vods", "1", "--make", str(n)]):
            failed.append("scan")
        step(log, "3/5 plan the queue", [PY, post, "plan"])
        if cfg.get("auto_post"):
            if step(log, "4/5 send to the platforms", [PY, post, "send"]):
                failed.append("send")
            step(log, "5/5 views so far", [PY, post, "stats"])
            step(log, "what is working", [PY, post, "report"])
            step(log, "followers", [PY, post, "growth"])
        else:
            step(log, "4/5 send (auto_post is off: showing what would go out)", [PY, post, "send"])
        step(log, "free disk space", [PY, str(ROOT / "src" / "cleanup.py")])
        summary = "finished" + (f" with problems in: {', '.join(failed)}" if failed else "")
        print(f"\ndaily run {summary}")
        log.write(f"\ndaily run {summary}\n")
        t = progress.report("Daily run", 1, 1, summary, finished=True)
        del t
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
