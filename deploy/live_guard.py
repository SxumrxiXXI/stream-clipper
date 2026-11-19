#!/usr/bin/env python3
"""What should a run of the live workflow do right now?

Prints two lines, `mode=work|idle|none` and `seconds=N`, read from the private
settings (private/config/posting.yaml) with nothing but the standard library,
so the workflow can decide before it installs anything.

  work   a posting time's work starts within 75 minutes: set up and do it
  idle   live posting is on but the next posting time is hours away: sleep
         `seconds` (at most five hours), then start the next run. This is what
         carries the chain of runs through the day without relying on GitHub's
         timer, which has skipped whole days on this repository.
  none   switched off, or past the last day: do nothing and start nothing

    python3 deploy/live_guard.py [--now "2026-11-20 00:10"]     (the time is for tests)
"""
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
text = (ROOT / "private" / "config" / "posting.yaml").read_text()


def one(key: str, default: str = "") -> str:
    m = re.search(rf'^{key}:[ \t]*"?([^"#\n]*)"?', text, re.M)
    return m.group(1).strip() if m else default


now = dt.datetime.now()                    # the workflow sets TZ to the settings' zone
if "--now" in sys.argv:
    now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1])
line = re.search(r"^live_hours:(.*)$", text, re.M)
hours = re.findall(r"\d\d:\d\d", line.group(1).split("#")[0]) if line else []
lead = dt.timedelta(minutes=float(one("live_lead_minutes", "25") or 25))
start, until = one("live_start"), one("live_until")

begin = None                               # when the next posting time's work starts
for day in (now.date(), now.date() + dt.timedelta(days=1)):
    d = day.isoformat()
    if one("live") != "true" or (start and d < start) or (until and d > until):
        continue
    for h in sorted(hours):
        when = dt.datetime.combine(day, dt.time.fromisoformat(h))
        if when - now > dt.timedelta(minutes=8) and begin is None:
            begin = when - lead
if begin is None:
    print("mode=none\nseconds=0")
elif begin - now <= dt.timedelta(minutes=75):
    print("mode=work\nseconds=0")
else:
    wait = min((begin - now - dt.timedelta(minutes=45)).total_seconds(), 5 * 3600)
    print(f"mode=idle\nseconds={int(max(60, wait))}")
