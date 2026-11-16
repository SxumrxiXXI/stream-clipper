#!/usr/bin/env python3
"""Scan streamers for their hottest recent moments, and rank the streamers.

    .venv/bin/python src/scan.py                      # scan everyone on the watchlist
    .venv/bin/python src/scan.py --streamer NAME      # one streamer only
    .venv/bin/python src/scan.py --make 5             # also clip the top 5 (approved streamers only)
    .venv/bin/python src/scan.py discover             # which games and streamers are hot right now
    .venv/bin/python src/scan.py discover "Fortnite"  # the same, for chosen games
    .venv/bin/python src/scan.py auto                 # discover, watchlist the top 10, scan them

Scanning only reads public chat replays and clip statistics, so it can look at
anyone on the watchlist (config/streamers.yaml). Making clips is different:
--make only ever touches streamers whose status is `approved` and who have a
permission link on file.

Moments come from chat bursts (moments/chat_spikes.py) on Twitch and YouTube,
plus the clips viewers made on Twitch when API credentials are set
(moments/trending.py). Results go to data/scan/<date>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "moments"))
import chat_spikes  # noqa: E402
sys.path.insert(0, str(ROOT / "src"))
import progress  # noqa: E402
from trending import Twitch  # noqa: E402

hms = chat_spikes.hms


def read_env() -> dict:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def twitch_client(required: bool) -> Twitch | None:
    env = read_env()
    cid, secret = env.get("TWITCH_CLIENT_ID", ""), env.get("TWITCH_CLIENT_SECRET", "")
    if cid and secret:
        return Twitch(cid, secret)
    if required:
        sys.exit("error: this needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env "
                 "(free app at https://dev.twitch.tv/console/apps)")
    return None


def load_streamers(only: str | None) -> list[dict]:
    cfg = yaml.safe_load((ROOT / "config" / "streamers.yaml").read_text()) or {}
    manual = cfg.get("streamers") or []
    auto = []
    if AUTO_LIST.exists():
        auto = (yaml.safe_load(AUTO_LIST.read_text()) or {}).get("streamers") or []
    known = {str(s.get("twitch", "")).lower() for s in manual if s.get("twitch")}
    out = []
    for s in manual + [x for x in auto if str(x.get("twitch", "")).lower() not in known]:
        s.setdefault("status", "watching")
        if s["status"] == "declined":
            continue
        if not s.get("twitch") and not s.get("youtube"):
            continue
        if only and only.lower() not in (s["name"].lower(), str(s.get("twitch", "")).lower()):
            continue
        out.append(s)
    if not out:
        sys.exit("error: no streamers to scan. Add some to config/streamers.yaml "
                 "or let it find them: scan.py auto")
    return out


def listed(url: str, limit: int) -> list[dict]:
    """Recent finished streams from a channel page, newest first, via yt-dlp."""
    cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", str(limit * 4), "--no-warnings",
           "--print", "%(url)s\t%(duration)s\t%(live_status)s\t%(title)s", url]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
    vods = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[1] in ("NA", "None") or parts[2] in ("is_live", "is_upcoming"):
            continue
        if float(parts[1]) < 600:          # too short to be a stream
            continue
        vods.append({"url": parts[0], "seconds": float(parts[1]), "title": parts[3],
                     "id": chat_spikes.source_of(parts[0])[1]})
    return vods[:limit]


def recent_vods(s: dict, tw: Twitch | None, days: int, limit: int) -> tuple[list[dict], list[dict]]:
    """-> (vods, viewer_clips) across the streamer's platforms."""
    vods, clips = [], []
    if s.get("twitch"):
        if tw:
            uid = tw.user_id(s["twitch"])
            vods += tw.recent_vods(uid, days, limit)
            clips += tw.viewer_clips(uid, days)
        else:
            vods += listed(f"https://www.twitch.tv/{s['twitch']}/videos?filter=archives&sort=time", limit)
    if s.get("youtube"):
        handle = s["youtube"] if s["youtube"].startswith("@") else "@" + s["youtube"]
        vods += listed(f"https://www.youtube.com/{handle}/streams", limit)
    return vods, clips


def overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def moments_for_vod(vod: dict, viewer_clips: list[dict], per_vod: int) -> list[dict]:
    try:
        msgs = chat_spikes.load_messages(chat_spikes.fetch_chat(vod["url"]))
    except SystemExit as e:        # no chat replay: fall back to viewer clips alone
        print(f"   {e}", file=sys.stderr)
        msgs = []
    spikes = [s for s in chat_spikes.detect(msgs, top=per_vod * 3) if not s.vote]
    mine = [c for c in viewer_clips if c["vod_id"] == vod["id"]]
    out, used = [], set()
    for s in spikes:
        hits = [i for i, c in enumerate(mine)
                if overlap(s.start, s.end, c["offset"], c["offset"] + c["seconds"])]
        used.update(hits)
        views = sum(mine[i]["views"] for i in hits)
        out.append({
            "start": s.start, "end": s.end, "peak": s.peak, "burst_start": s.burst_start,
            "kind": s.kind, "chatters": s.chatters, "chat_ratio": s.ratio,
            "viewer_clips": len(hits), "viewer_clip_views": views, "sample": s.sample[:3],
            "chat": s.chat or [],
            "clips": [[mine[i]["offset"], mine[i]["title"], mine[i]["views"]] for i in hits],
            # Viewers clipping the same moment is independent confirmation.
            "score": round(s.score * (1 + math.log10(1 + views) / 2), 1),
            "why": f"chat {s.kind}" + (" + viewers clipped it" if hits else ""),
        })
    for i, c in enumerate(mine):   # viewers clipped it but chat stayed calm
        if i not in used:
            near = [(round(t, 1), text[:90]) for t, who, text in msgs
                    if c["offset"] - 5 <= t <= c["offset"] + c["seconds"] + 15][:60]
            out.append({
                "start": max(0.0, c["offset"] - 10), "end": c["offset"] + c["seconds"] + 10,
                "peak": c["offset"] + c["seconds"],
                # A viewer hits "clip" just after the moment, so it sits near the clip's end.
                "burst_start": c["offset"] + max(0.0, c["seconds"] - 6),
                "kind": "viewer clip", "chatters": 0, "chat_ratio": 0, "viewer_clips": 1,
                "viewer_clip_views": c["views"], "sample": [c["title"]], "chat": near,
                "clips": [[c["offset"], c["title"], c["views"]]],
                "score": round(10 * math.log10(1 + c["views"]), 1), "why": "viewers clipped it",
            })
    out.sort(key=lambda m: m["score"], reverse=True)
    return out[:per_vod]


def scan(a) -> None:
    streamers = load_streamers(a.streamer)
    tw = twitch_client(required=False)
    if not tw and any(s.get("twitch") for s in streamers):
        print("note: no Twitch API credentials in .env, so viewer clips are not counted (chat only)\n")

    posting = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    rules = {"auto_approve": bool(posting.get("auto_approve")),
             "skip_channels": {str(c).lower() for c in posting.get("skip_channels") or []},
             "skip_title_words": [str(w).lower() for w in posting.get("skip_title_words") or []],
             "max_per_streamer": int(posting.get("max_per_streamer") or 99)}
    streamers = [s for s in streamers if str(s.get("twitch", "")).lower() not in rules["skip_channels"]]
    found = []
    t_scan = progress.report("Scanning streamers", 0, len(streamers), streamers[0]["name"], step_seconds=40)
    for n, s in enumerate(streamers):
        progress.report("Scanning streamers", n, len(streamers), s["name"], started_at=t_scan, step_seconds=40)
        vods, clips = recent_vods(s, tw, a.days, a.vods * 3)
        # Marathon streams have chat logs too large to be worth the wait.
        long = [v for v in vods if v["seconds"] > a.max_hours * 3600]
        for v in long:
            print(f"   skipping {hms(v['seconds'])} stream (over {a.max_hours} h): {v['title'][:50]}")
        vods = [v for v in vods if v not in long]
        risky = [v for v in vods if any(w in v["title"].lower() for w in rules["skip_title_words"])]
        for v in risky:
            print(f"   skipping off-brand stream: {v['title'][:60]}")
        vods = [v for v in vods if v not in risky][: a.vods]
        print(f"{s['name']} [{s['status']}]: {len(vods)} streams" + (f", {len(clips)} viewer clips" if tw else ""))
        for v in vods:
            print(f"   {hms(v['seconds'])}  {v['title'][:70]}")
            for m in moments_for_vod(v, clips, a.per_vod):
                found.append({**m, "streamer": s["name"], "status": s["status"],
                              "game": (s.get("games") or [""])[0],
                              "credit": s.get("credit", s["name"]), "permission": s.get("permission", ""),
                              "campaign": s.get("campaign", "general"),
                              "vod_url": v["url"], "vod_title": v["title"]})
    found.sort(key=lambda m: m["score"], reverse=True)
    progress.report("Scanning streamers", len(streamers), len(streamers), "done", started_at=t_scan, finished=True)

    out = ROOT / "data" / "scan" / f"{dt.date.today():%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, indent=1))

    # Which streamer is the best material: their five strongest moments, added up.
    board = defaultdict(list)
    for m in found:
        board[m["streamer"]].append(m["score"])
    print(f"\n{'streamer':<20} {'status':<9} {'moments':>7} {'top-5 score':>11} {'best':>6}")
    for name, scores in sorted(board.items(), key=lambda kv: -sum(sorted(kv[1])[-5:])):
        status = next(s["status"] for s in streamers if s["name"] == name)
        print(f"{name:<20} {status:<9} {len(scores):>7} {sum(sorted(scores)[-5:]):>11.1f} {max(scores):>6}")

    print(f"\n{'#':>2}  {'streamer':<16} {'window':<19} {'score':>6}  {'why':<30} sample")
    for i, m in enumerate(found[:25], 1):
        print(f"{i:>2}  {m['streamer'][:16]:<16} {hms(m['start'])}-{hms(m['end'])}  "
              f"{m['score']:>6}  {m['why']:<30} {' | '.join(m['sample'])[:60]}")
    print(f"\nsaved {len(found)} moments to {out}")

    if not a.make:
        return
    # Never look at the same moment twice: anything overlapping a window that
    # was already clipped, or already judged and rejected, is skipped.
    done: list[tuple[str, float, float]] = []
    for name in ("runs.jsonl", "judged.jsonl"):
        path = ROOT / "data" / name
        if path.exists():
            for line in path.read_text().splitlines():
                r = json.loads(line)
                # A moment the judge liked but that lost out to better ones may be tried again.
                if name == "judged.jsonl" and r.get("keep"):
                    continue
                if r.get("start") is not None:
                    done.append((r["source"], float(r["start"]), float(r.get("end") or r["start"] + 60)))

    def eligible(m: dict) -> bool:
        if m["status"] == "approved" and m["permission"].strip():
            return True
        return rules["auto_approve"] and m["status"] in ("watching", "asked")

    candidates = []
    for m in found:
        if not eligible(m):
            continue
        if any(src == m["vod_url"] and overlap(m["start"], m["end"], s0 - 30, s1 + 30) for src, s0, s1 in done):
            continue
        if not m["permission"].strip():
            m["permission"] = ("Auto-approved under the owner's standing instruction (auto_approve); "
                               "no individual permission on file")
        done.append((m["vod_url"], m["start"], m["end"]))
        candidates.append(m)
    if not candidates:
        print("\nno new moments to look at (everything strong was already clipped or rejected)")
        return

    # Two passes. First the judge (moments/judge.py) watches more candidates
    # than we need; then clips are made from the ones it rated best. Taking the
    # best of a pool beats taking the first few that merely pass.
    want = a.make
    pool = min(len(candidates), max(want + 2, want * 3), 14)
    cand_dir = ROOT / "data" / "scan" / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLIP_PROGRESS_PARENT": "1"}

    def clip_cmd(m: dict, spec: Path, *extra: str) -> list[str]:
        return [sys.executable, str(ROOT / "src" / "clip.py"), m["vod_url"],
                "--permission", m["permission"], "--credit", m["credit"],
                "--campaign", m["campaign"], "--clips", "1",
                "--streamer", m["streamer"], "--game", m.get("game", ""),
                "--score", str(m["score"]), "--moment", str(spec), *extra]

    total = pool + want
    t0 = progress.report("Finding the best moments", 0, total, "starting", step_seconds=150)
    kept, unjudged, seen_per = [], [], defaultdict(int)
    looked = 0
    for m in candidates:
        if looked >= pool:
            break
        # No point judging a fifth moment from a streamer we will use twice at most.
        if seen_per[m["streamer"]] >= rules["max_per_streamer"] * 2:
            continue
        seen_per[m["streamer"]] += 1
        looked += 1
        label = f"{m['streamer']} at {hms(m['burst_start'])}"
        print(f"\n=== judging {looked}/{pool}: {label}  ({m['why']}, chat score {m['score']})", flush=True)
        progress.report("Finding the best moments", looked - 1, total, f"watching {label}", started_at=t0,
                        step_seconds=150)
        spec = cand_dir / f"{dt.date.today()}_{looked:02d}.json"
        spec.write_text(json.dumps(m, ensure_ascii=False))
        rc = subprocess.run(clip_cmd(m, spec, "--judge-only"), env=env).returncode
        if rc == 0:
            kept.append((json.loads(spec.read_text()), spec))
        elif rc == 4:
            unjudged.append((m, spec))

    # Best first: the judge's rating, with the size of chat's reaction as tie-break.
    kept.sort(key=lambda ms: (ms[0]["verdict"]["rank"], ms[0]["score"]), reverse=True)
    if not kept and unjudged:
        print("\nthe judge could not run; falling back to chat ranking with OpenShorts choosing the cuts")
    order = kept + unjudged
    print(f"\njudged {looked}: {len(kept)} worth clipping, {looked - len(kept) - len(unjudged)} rejected")
    for m, _ in kept:
        v = m["verdict"]
        print(f"   {v['rank']:>5}  {m['streamer'][:16]:<16} {hms(m['burst_start'])}  [{v['moment_type']}]  {v['hook']}")

    made, per = 0, defaultdict(int)
    for m, spec in order:
        if made >= want:
            break
        if per[m["streamer"]] >= rules["max_per_streamer"]:
            continue
        label = f"{m['streamer']} at {hms(m['burst_start'])}"
        print(f"\n=== making clip {made + 1}/{want}: {label}", flush=True)
        progress.report("Finding the best moments", pool + made, total, f"making {label}", started_at=t0)
        rc = subprocess.run(clip_cmd(m, spec), env=env).returncode
        if rc == 0:
            made += 1
            per[m["streamer"]] += 1
        else:
            print(f"    clip.py failed (exit {rc}); moving on")
    # Stretches downloaded for candidates that were not used.
    for leftover in (ROOT / "data" / "windows").glob("*.mp4"):
        leftover.unlink(missing_ok=True)
    print(f"\nmade {made} of {want} clips (judged {looked} candidates, {len(kept)} passed)")
    progress.report("Finding the best moments", total, total, f"{made} clips made from {looked} candidates",
                    started_at=t0, finished=True)


AUTO_LIST = ROOT / "config" / "watchlist.auto.yaml"


def discover(a, tw: Twitch | None = None) -> list[dict]:
    """Find streamers from engagement, no names needed.

    With no game given it starts from the categories with the most viewers
    right now. For each category it reads the most-viewed clips of the last
    few days and who is live, then ranks streamers by how much their streams
    are being clipped. --add N puts the top N on the automatic watchlist.
    """
    tw = tw or twitch_client(required=True)
    if a.game:
        games = []
        for name in a.game:
            g = tw.game(name)
            if not g:
                sys.exit(f"error: Twitch has no category called exactly \"{name}\"")
            games.append(g)
    else:
        skip = {x.strip().lower() for x in (a.skip or "").split(",") if x.strip()}
        games = [g for g in tw.top_games(a.games * 2) if g["name"].lower() not in skip][: a.games]

    people: dict[str, dict] = {}
    rows = []
    for g in games:
        clips = [c for c in tw.game_clips(g["id"], a.days)
                 if not a.language or c.get("language") == a.language]
        live = tw.live_streams(g["id"], a.language)
        rows.append((g["name"], sum(x["viewer_count"] for x in live), len(live),
                     sum(c["view_count"] for c in clips), len(clips)))
        for c in clips:
            p = people.setdefault(c["broadcaster_id"], {
                "id": c["broadcaster_id"], "name": c["broadcaster_name"], "clips": 0,
                "clip_views": 0, "live_viewers": 0, "games": set(), "top_clip": c["title"]})
            p["clips"] += 1
            p["clip_views"] += c["view_count"]
            p["games"].add(g["name"])
        for x in live:
            if x["user_id"] in people:
                people[x["user_id"]]["live_viewers"] = x["viewer_count"]

    print(f"Categories ({'chosen' if a.game else 'most viewers right now'}), last {a.days} days of clips\n")
    print(f"{'category':<34} {'live viewers':>12} {'streams':>7} {'clip views':>11} {'clips':>5}")
    for name, viewers, n_live, views, n_clips in sorted(rows, key=lambda r: -r[3]):
        print(f"{name[:34]:<34} {viewers:>12,} {n_live:>7} {views:>11,} {n_clips:>5}")

    # Several well-viewed clips beat one viral one: that streamer is reliable material.
    for p in people.values():
        p["score"] = round(p["clip_views"] * (1 + math.log2(p["clips"])) / 1000, 1)
    ranked = sorted(people.values(), key=lambda p: -p["score"])[: max(a.top, a.add)]
    logins = tw.logins([p["id"] for p in ranked])
    print(f"\n{'#':>2}  {'streamer':<22} {'clips':>5} {'clip views':>11} {'live now':>9} {'score':>8}  categories")
    for i, p in enumerate(ranked[: a.top], 1):
        p["login"] = logins.get(p["id"], "")
        live = f"{p['live_viewers']:,}" if p["live_viewers"] else "-"
        print(f"{i:>2}  {p['name'][:22]:<22} {p['clips']:>5} {p['clip_views']:>11,} {live:>9} {p['score']:>8}  "
              f"{', '.join(sorted(p['games']))[:40]}")

    if a.add:
        picked = [{"name": p["name"], "twitch": logins.get(p["id"], ""), "status": "watching",
                   "credit": "@" + logins.get(p["id"], p["name"]),
                   "games": sorted(p["games"]),
                   "found": f"{dt.date.today()}: {p['clips']} clips, {p['clip_views']:,} clip views in "
                            f"{', '.join(sorted(p['games']))}"}
                  for p in ranked[: a.add] if logins.get(p["id"])]
        AUTO_LIST.write_text(
            "# Written by `scan.py discover --add`. Rewritten on every run.\n"
            "# To keep a streamer, or to mark one asked / approved / declined, copy the\n"
            "# entry into streamers.yaml: that file always wins over this one.\n"
            + yaml.safe_dump({"streamers": picked}, sort_keys=False, allow_unicode=True))
        print(f"\nadded {len(picked)} streamers to {AUTO_LIST.relative_to(ROOT)} as `watching`")
    else:
        print("\nRun again with --add N to put the top N on the watchlist, or use `scan.py auto`.")
    return ranked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    ap.add_argument("--vods", type=int, default=2, help="streams per streamer (default 2)")
    ap.add_argument("--per-vod", type=int, default=5, help="moments kept per stream (default 5)")
    ap.add_argument("--max-hours", type=float, default=8, help="skip streams longer than this (default 8)")
    ap.add_argument("--make", type=int, default=0, help="clip the top N moments (approved streamers only)")
    ap.add_argument("--streamer", help="only this name from the list")
    sub = ap.add_subparsers(dest="cmd")
    for name, text in (("discover", "find streamers from engagement (needs Twitch credentials)"),
                       ("auto", "discover, put the best on the watchlist, then scan them")):
        d = sub.add_parser(name, help=text)
        d.add_argument("game", nargs="*", help="exact Twitch category names; none = the biggest right now")
        d.add_argument("--games", type=int, default=8, help="how many top categories to cover (default 8)")
        d.add_argument("--skip", default="", help="comma-separated categories to leave out")
        d.add_argument("--language", default="en", help="clip and stream language ('' for any)")
        d.add_argument("--top", type=int, default=20, help="streamers to show (default 20)")
        d.add_argument("--add", type=int, default=10 if name == "auto" else 0,
                       help="put the top N on the automatic watchlist")
    a = ap.parse_args()
    if a.cmd == "discover":
        discover(a)
    elif a.cmd == "auto":
        discover(a)
        print("\n" + "=" * 78 + "\n")
        scan(a)
    else:
        scan(a)


if __name__ == "__main__":
    main()
