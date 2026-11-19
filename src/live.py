#!/usr/bin/env python3
"""Post a clip from a stream that is live right now, at the top of each hour.

The daily run clips what happened yesterday. This one works through the night
(the hours in `live_hours`), and for each of those hours:

  1. about 25 minutes before the hour, asks Twitch who is live in the featured
     game, most viewers first
  2. reads the last hour or so of each stream's chat and viewers' own clips
  3. has the judge watch the strongest new moments, best first
  4. makes a clip from the best one the judge keeps and confirms as the game,
     and hands it to the posting service for the top of the hour

It reuses the scanner, the judge, the clipper and the queue as they are, with
every block they have (nudity, slurs, real-life footage, silent cuts). An hour
in which nothing passes gets no post. A live stream's chat and video can be
fetched about two minutes behind the broadcast.

    python src/live.py --hours 5            # work for five hours, then stop
    python src/live.py --should-run         # exit 0 if there is an hour to work on soon
    python src/live.py --force --once       # a trial: treat the next full hour as a live hour

Settings (config/posting.yaml): live, live_start, live_until, live_hours,
live_lead_minutes, live_watch, live_window_minutes, live_min_score,
live_min_rank, live_max_per_day, live_max_per_streamer, live_max_judged_per_day.
Nothing is sent unless auto_post is on; CLIP_DRY_RUN makes it a trial.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "moments"))
sys.path.insert(0, str(ROOT / "src" / "publish"))

os.environ["CLIP_LIVE"] = "1"        # clips made from here are numbered apart and marked live

import post   # noqa: E402
import scan   # noqa: E402

PUBLIC = sys.stdout                   # what the (public) workflow log may show: counts and times only
SOON = dt.timedelta(minutes=75)       # "there is an hour to work on soon"


def say(text: str) -> None:
    PUBLIC.write(f"[{dt.datetime.now():%H:%M:%S}] {text}\n")
    PUBLIC.flush()


def note(text: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {text}", flush=True)


def next_hour(cfg: dict, now: dt.datetime, force: bool = False) -> dt.datetime | None:
    """The next posting time that can still be worked on: a live hour, on a day
    the settings switch on, at least a few minutes away. With `force` (trials)
    every full hour counts."""
    tz = ZoneInfo(cfg["timezone"])
    now = now.astimezone(tz)
    if force:
        t = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        return t if t - now > dt.timedelta(minutes=8) else t + dt.timedelta(hours=1)
    times = sorted(dt.time.fromisoformat(str(x)) for x in cfg.get("live_hours") or [])
    for day in (now.date(), now.date() + dt.timedelta(days=1)):
        if not post.live_on(cfg, day):
            continue
        for t in times:
            when = dt.datetime.combine(day, t, tz)
            if when - now > dt.timedelta(minutes=8):
                return when
    return None


def sync(message: str) -> None:
    """Fetch what other runs saved and save what this one changed. Only on GitHub,
    where the records live in a second checkout; a no-op anywhere else."""
    script = os.environ.get("LIVE_SYNC")
    if not script:
        return
    done = subprocess.run(["bash", script, message], capture_output=True, text=True)
    if done.returncode:
        note(f"saving the records failed: {(done.stdout + done.stderr).strip()[-300:]}")
        say("could not save the records this time; carrying on")


def seen_before() -> list[tuple[str, float, float]]:
    """Every stretch already clipped or already shown to the judge, kept or not.
    (The daily run lets a kept moment be judged again another day. Here that
    would mean asking about the same moment every hour.)"""
    done = []
    for name in ("runs.jsonl", "judged.jsonl"):
        path = ROOT / "data" / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("start") is not None:
                done.append((r["source"], float(r["start"]), float(r.get("end") or r["start"] + 60)))
    return done


def judged_today(tz: ZoneInfo) -> int:
    path, today = ROOT / "data" / "judged.jsonl", dt.datetime.now(tz).date().isoformat()
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        n += 1 if r.get("live") and str(r.get("at", ""))[:10] == today else 0
    return n


def who_is_live(tw, cfg: dict, rules: dict, game: str) -> list[dict]:
    """The live streams of the game, most viewers first, as the scanner's streamer entries."""
    found = tw.game(game)
    if not found:
        note(f"Twitch has no category called {game!r}")
        return []
    listed = yaml.safe_load((ROOT / "config" / "streamers.yaml").read_text()) or {}
    declined = {str(s.get("twitch", "")).lower() for s in listed.get("streamers") or []
                if s.get("status") == "declined"}
    out = []
    for s in tw.live_streams(found["id"], cfg.get("live_language", "en")):
        login = str(s.get("user_login", "")).lower()
        if not login or login in rules["skip_channels"] or login in declined:
            continue
        if any(w in str(s.get("title", "")).lower() for w in rules["skip_title_words"]):
            continue
        out.append({"name": s.get("user_name") or login, "twitch": login, "status": "watching",
                    "games": [found["name"]], "viewers": s.get("viewer_count", 0),
                    "live_since": s.get("started_at")})
    return out


def hour(cfg: dict, tw, when: dt.datetime) -> int:
    """Make and place the clip for one posting time. Returns how many were placed."""
    tz = ZoneInfo(cfg["timezone"])
    label = f"{when:%H:%M}"
    items = post.load()
    free, why = post.live_room(cfg, items, when)
    if not free:
        note(f"{label}: no clip, {why}")
        say(f"{label}: skipped (that time is taken or the day's limit is reached)")
        return 0
    rules = scan.load_rules()
    game = rules["focus_game"] or str(cfg.get("focus_game", "")).lower()
    if not game:
        note("no featured game today; nothing to post")
        return 0
    # Spread the night over several streamers.
    day, had = when.date().isoformat(), {}
    for i in items:
        if i.get("live") and i["status"] in ("scheduled", "posted") and str(i.get("scheduled_for", ""))[:10] == day:
            had[str(i.get("streamer", "")).lower()] = had.get(str(i.get("streamer", "")).lower(), 0) + 1
    most = int(cfg.get("live_max_per_streamer", 2))
    streamers = [s for s in who_is_live(tw, cfg, rules, game) if had.get(s["name"].lower(), 0) < most]
    streamers = streamers[: int(cfg.get("live_watch", 8))]
    if not streamers:
        note(f"{label}: nobody is live in the game right now")
        say(f"{label}: no stream to clip")
        return 0
    note(f"{label}: looking at {', '.join(s['name'] + ' (' + format(s['viewers'], ',') + ')' for s in streamers)}")

    window = float(cfg.get("live_window_minutes", 65))
    a = argparse.Namespace(days=2, vods=1, per_vod=5, max_hours=8, since_hours=window / 60)
    found = scan.find_moments(streamers, a, rules, tw)
    done = seen_before()
    floor = float(cfg.get("live_min_score", 40))
    fresh = []
    for m in found:
        if m.get("game", "").lower() != game or m["score"] < floor:
            continue
        if any(src == m["vod_url"] and scan.overlap(m["start"], m["end"], s0 - 30, s1 + 30) for src, s0, s1 in done):
            continue
        # The recording trails the broadcast: a moment from the last two minutes
        # cannot be downloaded whole yet.
        if float(m.get("peak") or m["burst_start"]) + 40 > float(m.get("stream_seconds") or 0) - 60:
            continue
        fresh.append(m)
    top = ", ".join(f"{m['streamer']} {m['score']}" for m in fresh[:6]) or "none"
    note(f"{label}: {len(found)} moments in the last {window:g} minutes, {len(fresh)} usable (score {floor:g}+). Strongest: {top}")
    budget = int(cfg.get("live_max_judged_per_day", 60)) - judged_today(tz)
    if not fresh or budget <= 0:
        say(f"{label}: " + ("nothing worth judging this hour" if not fresh else "the day's judging budget is used up"))
        return 0
    live_rules = {**rules, "focus_game": game, "focus_phase": "all", "focus_min_clips": 99,
                  "compare_runs": 0, "only_featured": True, "max_per_streamer": 2,
                  "min_rank": float(cfg.get("live_min_rank", 8.0))}
    made = scan.make_clips(fresh[: min(8, budget)], live_rules, argparse.Namespace(make=1, allow_none=True))
    if not made:
        say(f"{label}: nothing passed the judge this hour")
        sync(f"Live {label}: no clip")
        return 0
    # Another run may have changed the queue while the clip was being made.
    sent = post.post_live(cfg, post.load(), really=bool(cfg.get("auto_post")), when=when)
    say(f"{label}: a clip was made" + (" and handed to the posting service" if sent and cfg.get("auto_post") else " (trial or refused: not posted)"))
    sync(f"Live clip for {label}")
    return sent


def tidy() -> None:
    """Chat downloads are only good for one look."""
    old = time.time() - 1800
    for f in (ROOT / "data" / "chat").glob("*_from*.json"):
        try:
            if f.stat().st_mtime < old:
                f.unlink()
        except OSError:
            pass


def main() -> None:
    global PUBLIC
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=5.0, help="how long to keep working (default 5)")
    ap.add_argument("--once", action="store_true", help="do one posting time and stop")
    ap.add_argument("--quiet", action="store_true", help="details go to data/logs/live-<date>.log, not the screen")
    ap.add_argument("--should-run", action="store_true", help="exit 0 if there is a posting time to work on soon")
    ap.add_argument("--force", action="store_true", help="a trial: every full hour counts as a live hour")
    a = ap.parse_args()

    cfg = post.config()
    tz = ZoneInfo(cfg["timezone"])
    lead = dt.timedelta(minutes=float(cfg.get("live_lead_minutes", 25)))
    nxt = next_hour(cfg, dt.datetime.now(tz), a.force)
    soon = nxt is not None and nxt - lead - dt.datetime.now(tz) <= SOON
    if a.should_run:
        print(f"next posting time {nxt:%a %H:%M}" if soon else "no posting time to work on soon")
        sys.exit(0 if soon else 1)
    if not soon:
        print("no posting time to work on soon; not starting")
        return

    if a.quiet:
        logs = ROOT / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        PUBLIC = os.fdopen(os.dup(1), "w")
        log = open(logs / f"live-{dt.date.today()}.log", "a", buffering=1)
        os.dup2(log.fileno(), 1)          # the scanner, the judge and the clipper inherit these
        os.dup2(log.fileno(), 2)
        sys.stdout = sys.stderr = log
    tw = scan.twitch_client(required=True)
    until = time.time() + a.hours * 3600
    note(f"===== live posting started, for up to {a.hours:g} hours"
         + (" (trial: nothing is sent)" if not cfg.get("auto_post") else "") + " =====")
    say(f"working for up to {a.hours:g} hours")
    n = sent = 0
    while True:
        sync(f"Live {dt.datetime.now():%F %H:%M}")           # what did other runs save? is it still switched on?
        cfg = post.config()
        now = dt.datetime.now(tz)
        when = next_hour(cfg, now, a.force)
        if when is None or when - lead - now > SOON:
            note("no posting time to work on soon; stopping")
            say("nothing more to do for now")
            break
        begin = (when - lead - now).total_seconds()
        # Not enough of this run left to finish that hour's clip: the next run takes it.
        if time.time() + max(0.0, begin) + 40 * 60 > until:
            note(f"not enough time left in this run for {when:%H:%M}; stopping so the next run takes it")
            say("out of time; the next run carries on")
            break
        if begin > 0:
            say(f"waiting for {when:%H:%M}'s turn")
            time.sleep(begin)
        n += 1
        try:
            sent += hour(post.config(), tw, when)
        except SystemExit as e:               # the scanner exits on some errors; one bad hour is not the end
            note(f"{when:%H:%M} ended early: {e}")
            say(f"{when:%H:%M}: a problem, carrying on")
        except Exception as e:
            note(f"{when:%H:%M} failed: {type(e).__name__}: {str(e)[:300]}")
            say(f"{when:%H:%M}: a problem, carrying on")
        tidy()
        if a.once:
            break
        # Never work on the same posting time twice.
        rest = (when - dt.datetime.now(tz)).total_seconds() - 8 * 60 + 5
        if rest > 0:
            time.sleep(rest)
    note(f"===== live posting finished: {n} posting times worked on, {sent} clips placed =====")
    say(f"finished: {n} posting times, {sent} clips placed")


if __name__ == "__main__":
    main()
