#!/usr/bin/env python3
"""Watch the featured game's live streams and post its strongest moments while
the stream is still going.

The daily run clips what happened yesterday. This keeps looking, every few
minutes, at the last half hour of the biggest live streams of the featured game:

  1. who is live in the game right now (Twitch), most viewers first
  2. chat bursts and viewer clips in the last `live_window_minutes` of each stream
  3. only moments strong enough to be worth an unplanned post go to the judge
  4. a clip the judge keeps, confirms as the game and rates highly is made and
     published at once, and the next scheduled clip is withdrawn in its place

It reuses the scanner, the judge, the clipper and the queue as they are. A live
stream's chat and video can be fetched about two minutes behind live, so a clip
goes out roughly 10 to 25 minutes after the moment.

    python src/live.py --hours 5            # watch for five hours
    python src/live.py --once               # one look, then stop
    python src/live.py --should-run         # exit 0 if the settings switch it on today

Settings (config/posting.yaml): live, live_start, live_until, live_watch,
live_every_minutes, live_window_minutes, live_min_score, live_min_clip_views,
live_min_rank, live_max_per_day, live_gap_minutes, live_max_per_streamer,
live_max_judged_per_day. Nothing is sent unless auto_post is on; CLIP_DRY_RUN
makes it a trial.
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


def say(text: str) -> None:
    PUBLIC.write(f"[{dt.datetime.now():%H:%M:%S}] {text}\n")
    PUBLIC.flush()


def note(text: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {text}", flush=True)


def settings() -> dict:
    return post.config()


def switched_on(cfg: dict, today: dt.date | None = None) -> tuple[bool, str]:
    """Is the watcher meant to run today? The owner's off switch is `live: false`."""
    if not cfg.get("live"):
        return False, "live is off in config/posting.yaml"
    today = today or dt.datetime.now(ZoneInfo(cfg["timezone"])).date()
    start, until = str(cfg.get("live_start") or ""), str(cfg.get("live_until") or "")
    if start and today.isoformat() < start:
        return False, f"live watching starts on {start}"
    if until and today.isoformat() > until:
        return False, f"live watching ended on {until}"
    return True, ""


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
    would mean asking about the same moment every few minutes.)"""
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
    """The biggest live streams of the game, as the scanner's streamer entries."""
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
        if len(out) >= int(cfg.get("live_watch", 8)):
            break
    return out


def look(cfg: dict, tw, n: int, only_look: bool = False) -> int:
    """One look at the live streams. Returns how many clips went out."""
    tz = ZoneInfo(cfg["timezone"])
    now = dt.datetime.now(tz)
    items = post.load()
    ok, why = post.live_room(cfg, items, now)
    if not ok:
        note(f"look {n}: not looking, {why}")
        say(f"look {n}: waiting (the day's or the hour's limit)")
        return 0
    rules = scan.load_rules()
    game = rules["focus_game"] or str(cfg.get("focus_game", "")).lower()
    if not game:
        note("no featured game today; nothing to watch")
        return 0
    streamers = who_is_live(tw, cfg, rules, game)
    # Not a third clip from the same streamer on one day.
    today = now.date().isoformat()
    had: dict[str, int] = {}
    for i in items:
        if i.get("live") and i["status"] in ("scheduled", "posted") and str(i.get("scheduled_for", ""))[:10] == today:
            had[str(i.get("streamer", "")).lower()] = had.get(str(i.get("streamer", "")).lower(), 0) + 1
    streamers = [s for s in streamers if had.get(s["name"].lower(), 0) < int(cfg.get("live_max_per_streamer", 1))]
    if not streamers:
        note(f"look {n}: nobody to watch right now")
        say(f"look {n}: no stream to watch")
        return 0
    note(f"look {n}: watching {', '.join(s['name'] + ' (' + format(s['viewers'], ',') + ')' for s in streamers)}")

    window = float(cfg.get("live_window_minutes", 30))
    a = argparse.Namespace(days=2, vods=1, per_vod=5, max_hours=8, since_hours=window / 60)
    found = scan.find_moments(streamers, a, rules, tw)
    done = seen_before()
    floor, views = float(cfg.get("live_min_score", 100)), int(cfg.get("live_min_clip_views", 1000))
    if os.environ.get("CLIP_DRY_RUN") and os.environ.get("CLIP_LIVE_MIN_SCORE"):
        floor = float(os.environ["CLIP_LIVE_MIN_SCORE"])       # a trial may lower the bar to exercise every step
    fresh, strong = [], []
    for m in found:
        if m.get("game", "").lower() != game:
            continue
        if any(src == m["vod_url"] and scan.overlap(m["start"], m["end"], s0 - 30, s1 + 30) for src, s0, s1 in done):
            continue
        # The recording trails the broadcast: a moment from the last two minutes
        # cannot be downloaded whole yet. It is still there at the next look.
        if float(m.get("peak") or m["burst_start"]) + 40 > float(m.get("stream_seconds") or 0) - 60:
            continue
        fresh.append(m)
        if m["score"] >= floor or m.get("viewer_clip_views", 0) >= views:
            strong.append(m)
    top = ", ".join(f"{m['streamer']} {m['score']}" + (f" ({m['viewer_clip_views']:,} clip views)" if m.get("viewer_clip_views") else "")
                    for m in fresh[:6]) or "none"
    note(f"look {n}: {len(found)} moments in the last {window:g} minutes, {len(fresh)} new, "
         f"{len(strong)} strong enough (score {floor:g} or {views:,} clip views). Strongest new: {top}")
    if not strong or only_look:
        say(f"look {n}: {len(streamers)} streams, {len(strong)} strong moments")
        return 0
    budget = int(cfg.get("live_max_judged_per_day", 30)) - judged_today(tz)
    if budget <= 0:
        note(f"look {n}: the day's judging budget is used up")
        say(f"look {n}: waiting (judging budget)")
        return 0
    strong = strong[: min(3, budget)]
    live_rules = {**rules, "focus_game": game, "focus_phase": "all", "focus_min_clips": 99,
                  "compare_runs": 0, "only_featured": True, "max_per_streamer": 1,
                  "min_rank": float(cfg.get("live_min_rank", 8.5))}
    made = scan.make_clips(strong, live_rules, argparse.Namespace(make=1, allow_none=True))
    if not made:
        say(f"look {n}: {len(strong)} moments judged, none good enough")
        sync(f"Live look {dt.datetime.now():%F %H:%M}")
        return 0
    # Another run may have changed the queue while the clip was being made.
    sent = post.post_live(cfg, post.load(), really=bool(cfg.get("auto_post")))
    say(f"look {n}: a clip was made and {'posted' if sent and cfg.get('auto_post') else 'not posted (trial or refused)'}")
    sync(f"Live clip {dt.datetime.now():%F %H:%M}")
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
    ap.add_argument("--hours", type=float, default=5.0, help="how long to keep watching (default 5)")
    ap.add_argument("--once", action="store_true", help="look once and stop")
    ap.add_argument("--quiet", action="store_true", help="details go to data/logs/live-<date>.log, not the screen")
    ap.add_argument("--should-run", action="store_true", help="exit 0 if the settings switch the watcher on today")
    ap.add_argument("--look-only", action="store_true", help="find the moments but judge and make nothing")
    ap.add_argument("--force", action="store_true", help="watch even if the settings say it is off (for trials)")
    a = ap.parse_args()

    cfg = settings()
    on, why = switched_on(cfg)
    if a.should_run:
        print("on" if on else why)
        sys.exit(0 if on else 1)
    if not on and not a.force:
        print(f"not watching: {why}")
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
    every = float(cfg.get("live_every_minutes", 5)) * 60
    until = time.time() + a.hours * 3600
    note(f"===== live watcher started, for {a.hours:g} hours" + (" (trial: nothing is sent)" if not cfg.get("auto_post") else "") + " =====")
    say(f"watching for {a.hours:g} hours")
    n = sent = 0
    while True:
        began = time.time()
        n += 1
        try:
            if n % 6 == 1:                      # every half hour or so: what did other runs save?
                sync(f"Live watcher {dt.datetime.now():%F %H:%M}")
            cfg = settings()
            on, why = switched_on(cfg)
            if not on and not a.force:
                note(f"stopping: {why}")
                say("switched off in the settings; stopping")
                break
            sent += look(cfg, tw, n, a.look_only)
        except SystemExit as e:               # the scanner exits on some errors; one bad look is not the end
            note(f"look {n} ended early: {e}")
            say(f"look {n}: a problem, carrying on")
        except Exception as e:
            note(f"look {n} failed: {type(e).__name__}: {str(e)[:300]}")
            say(f"look {n}: a problem, carrying on")
        tidy()
        if a.once or time.time() + 60 > until:
            break
        time.sleep(max(20.0, min(every - (time.time() - began), until - time.time() - 30)))
    note(f"===== live watcher finished: {n} looks, {sent} clips posted =====")
    say(f"finished: {n} looks, {sent} clips posted")


if __name__ == "__main__":
    main()
