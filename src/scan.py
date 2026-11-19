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


def featured_today(posting: dict, today: dt.date | None = None) -> tuple[str, int, str]:
    """Which game is featured today and how strongly: (game in lower case, clips kept
    for it, phase).

    Phases, from the settings: nothing before `focus_start`; for `focus_full_days`
    days from then every clip of the day is the featured game ("all"); after that
    `focus_min_clips` of them ("share"). Whatever the phase, a slot the featured
    game cannot fill with a moment the judge kept goes to the best other clip.
    """
    game = str(posting.get("focus_game") or "").strip().lower()
    # Rehearsals only: stand in a game that is out today for one that is not yet,
    # so the whole schedule can be tried on real streams. Ignored when posting is on.
    if os.environ.get("CLIP_DRY_RUN") and os.environ.get("CLIP_PRETEND_GAME"):
        game = os.environ["CLIP_PRETEND_GAME"].strip().lower()
    if not game:
        return "", 0, "off"
    pretend = os.environ.get("CLIP_PRETEND_DATE", "")           # rehearsals only
    today = today or (dt.date.fromisoformat(pretend) if pretend else dt.date.today())
    start = posting.get("focus_start")
    if start:
        start = start if isinstance(start, dt.date) else dt.date.fromisoformat(str(start))
        if today < start:
            return "", 0, "before"
        if today < start + dt.timedelta(days=int(posting.get("focus_full_days", 0))):
            return game, 99, "all"
    share = int(posting.get("focus_min_clips", 0))
    return (game, share, "share") if share > 0 else ("", 0, "off")


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


def NOW() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def started_at(vod: dict) -> dt.datetime | None:
    """When the stream began (Twitch gives it; a YouTube listing does not)."""
    try:
        return dt.datetime.fromisoformat(str(vod["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def moments_for_vod(vod: dict, viewer_clips: list[dict], per_vod: int) -> list[dict]:
    since = vod.get("scan_from", 0)    # a marathon stream: only its last hours of chat
    try:
        msgs = chat_spikes.load_messages(chat_spikes.fetch_chat(vod["url"], since))
    except SystemExit as e:        # no chat replay: fall back to viewer clips alone
        print(f"   {e}", file=sys.stderr)
        msgs = []
    every = [s for s in chat_spikes.detect(msgs, since=since) if not s.vote]
    spikes = every[: per_vod * 3]
    # The reaction more people joined than any other in the stream: a fact the
    # page can state. Not claimed when only part of the stream's chat was read.
    most = max((s.chatters for s in every), default=0) if not since else 0
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
            "stream_peak": bool(most) and s.chatters == most,
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
    # The strongest chat moments, plus the most-viewed clips viewers made while
    # chat stayed calm. Those score low on the chat scale, so without their own
    # places they would always be cut here and never be looked at.
    by_chat = sorted((m for m in out if m["chatters"]), key=lambda m: m["score"], reverse=True)[:per_vod]
    by_view = sorted((m for m in out if not m["chatters"]), key=lambda m: m["viewer_clip_views"], reverse=True)[:3]
    return sorted(by_chat + by_view, key=lambda m: m["score"], reverse=True)


def scan(a) -> None:
    streamers = load_streamers(a.streamer)
    tw = twitch_client(required=False)
    if not tw and any(s.get("twitch") for s in streamers):
        print("note: no Twitch API credentials in .env, so viewer clips are not counted (chat only)\n")

    posting = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    rules = {"auto_approve": bool(posting.get("auto_approve")),
             "skip_channels": {str(c).lower() for c in posting.get("skip_channels") or []},
             "skip_title_words": [str(w).lower() for w in posting.get("skip_title_words") or []],
             "max_per_streamer": int(posting.get("max_per_streamer") or 99),
             "tournament_words": [str(w).lower() for w in posting.get("tournament_words") or []],
             "max_tournament_clips": int(posting.get("max_tournament_clips", 99)),
             "viewer_clip_every": int(posting.get("viewer_clip_every", 3)),
             "viewer_clip_min_views": int(posting.get("viewer_clip_min_views", 300)),
             "compare_runs": int(posting.get("compare_runs", 3)),
             "same_stream_gap": 60 * float(posting.get("same_stream_gap_minutes", 10)),
             "focus_game": featured_today(posting)[0],
             "focus_min_clips": featured_today(posting)[1],
             "focus_phase": featured_today(posting)[2]}
    streamers = [s for s in streamers if str(s.get("twitch", "")).lower() not in rules["skip_channels"]]
    found = []
    t_scan = progress.report("Scanning streamers", 0, len(streamers), streamers[0]["name"], step_seconds=40)
    for n, s in enumerate(streamers):
        progress.report("Scanning streamers", n, len(streamers), s["name"], started_at=t_scan, step_seconds=40)
        vods, clips = recent_vods(s, tw, a.days, a.vods * 3)
        # A marathon stream's whole chat log is too large to be worth the wait, and
        # launch days are mostly marathons: read its last hours, the freshest part.
        # Clips viewers made earlier in it still count as moments of their own.
        for v in vods:
            if v["seconds"] > a.max_hours * 3600:
                v["scan_from"] = v["seconds"] - a.max_hours * 3600
        # A round between daily runs only wants what happened in the last hours:
        # streams that ended before that are left out, and of the others only the
        # recent part is read (plus ten minutes for chat's baseline to settle).
        if a.since_hours:
            fresh = []
            for v in vods:
                born = started_at(v)
                if born is None:
                    continue                   # no start time (YouTube listing): cannot place it in time
                first = (NOW() - dt.timedelta(hours=a.since_hours) - born).total_seconds()
                if first >= v["seconds"]:
                    continue                   # it ended before the window opened
                if first > 0:
                    v["moments_from"] = first
                    v["scan_from"] = max(v.get("scan_from", 0), first - 600)
                fresh.append(v)
            vods = fresh
        risky = [v for v in vods if any(w in v["title"].lower() for w in rules["skip_title_words"])]
        for v in risky:
            print(f"   skipping off-brand stream: {v['title'][:60]}")
        vods = [v for v in vods if v not in risky][: a.vods]
        print(f"{s['name']} [{s['status']}]: {len(vods)} streams" + (f", {len(clips)} viewer clips" if tw else ""))
        for v in vods:
            print(f"   {hms(v['seconds'])}  {v['title'][:70]}"
                  + (f"   (chat read from {hms(v['scan_from'])} on)" if v.get("scan_from") else ""))
            # Which game was on screen at that point of the stream. Twitch labels a
            # whole stream with one category, but every viewer clip carries the
            # category of the moment it was cut from: the nearest clip, within 45
            # minutes, tells us. Otherwise the streamer's usual game is the guess.
            here = [c for c in clips if c["vod_id"] == v["id"] and c.get("game_id")]
            names = tw.game_names([c["game_id"] for c in here]) if tw and here else {}
            born = started_at(v)
            for m in moments_for_vod(v, clips, a.per_vod):
                if m["burst_start"] < v.get("moments_from", 0):
                    continue
                near = min(here, key=lambda c: abs(c["offset"] - m["burst_start"]), default=None)
                seen = names.get(near["game_id"], "") if near and abs(near["offset"] - m["burst_start"]) <= 2700 else ""
                found.append({**m, "streamer": s["name"], "status": s["status"],
                              "stream_seconds": v["seconds"],
                              # When it happened, so a post's age can be compared with its views.
                              "happened": (born + dt.timedelta(seconds=m["burst_start"])).isoformat(timespec="seconds")
                                          if born else None,
                              "game": seen or (s.get("games") or [""])[0],
                              "game_from": "a viewer clip nearby" if seen else "the streamer's usual game",
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

    if a.make:
        make_clips(found, rules, a)


def make_clips(found: list[dict], rules: dict, a) -> None:
    """Judge the best of `found` and make `a.make` clips from what passes."""
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
        # What viewers called their own clips of the moment is a cheap first
        # filter for adult and gambling material, before the judge is asked.
        named = " ".join(str(c[1]) for c in m.get("clips") or []).lower()
        if any(w in named for w in rules["skip_title_words"]):
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

    # Chat is not the only witness. On ten streams chat bursts caught 37% of the
    # moments viewers clipped (64% by clip views; study of 2026-11-16). So moments
    # viewers clipped while chat stayed calm get every third place in the pool,
    # biggest clip first, provided the clip has a real audience.
    every = rules["viewer_clip_every"]
    by_chat = [m for m in candidates if m.get("chatters")]
    by_view = sorted((m for m in candidates if not m.get("chatters")
                      and m.get("viewer_clip_views", 0) >= rules["viewer_clip_min_views"]),
                     key=lambda m: -m["viewer_clip_views"])
    if every > 1:
        candidates = []
        while by_chat or by_view:
            candidates += [by_chat.pop(0) for _ in range(min(every - 1, len(by_chat)))]
            if by_view:
                candidates.append(by_view.pop(0))
    else:
        candidates = by_chat
    # A game the owner wants featured gets every other place in the pool, so the
    # judge sees enough of it to fill its share of the day's clips.
    focus = rules["focus_game"]
    def featured(m: dict) -> bool:
        return bool(focus) and m.get("game", "").lower() == focus
    if focus and rules["focus_min_clips"] > 0:
        ours, rest = [m for m in candidates if featured(m)], [m for m in candidates if not featured(m)]
        if rules["focus_phase"] == "all":
            # Every clip of the day should be the featured game. Its moments come
            # first, one streamer after another (each streamer's best, then each
            # one's second best ...), so one long stream cannot fill the pool. The
            # others follow and are only looked at if too few of ours pass (below).
            by_streamer: dict[str, list[dict]] = defaultdict(list)
            for m in ours:
                by_streamer[m["streamer"]].append(m)
            ours = []
            while any(by_streamer.values()):
                for name in list(by_streamer):
                    if by_streamer[name]:
                        ours.append(by_streamer[name].pop(0))
            candidates = ours + rest
        else:
            candidates = []
            while ours or rest:
                if ours:
                    candidates.append(ours.pop(0))
                if rest:
                    candidates.append(rest.pop(0))

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

    # Launch week: the judge turns down a good share of the featured game's
    # moments (on launch day, three of the first seven for nudity) and some
    # "moments" in a game stream are not the game at all. So keep looking at the
    # featured game's moments until enough passed AND are really the game, up to
    # `ours_limit`; only then, if still short, look at up to `cover` others.
    all_ours = bool(focus) and rules["focus_phase"] == "all" and rules["focus_min_clips"] > 0
    ours_limit = min(sum(1 for m in candidates if featured(m)), 12 if want > 1 else 5) if all_ours else 0
    enough = want + 2 if want > 1 else 2
    cover = 2 if want > 1 else 1
    if all_ours:
        pool = min(len(candidates), ours_limit + cover)

    def is_ours(m: dict) -> bool:
        return featured(m) and (m.get("verdict") or {}).get("game_label_ok", True)

    total = pool + want
    t0 = progress.report("Finding the best moments", 0, total, "starting", step_seconds=150)
    kept, unjudged, seen_per = [], [], defaultdict(int)
    looked = looked_ours = looked_rest = 0
    for m in candidates:
        if looked >= pool:
            break
        if all_ours:
            good = sum(1 for k, _ in kept if is_ours(k))
            if featured(m):
                if looked_ours >= ours_limit or good >= enough:
                    continue
            elif good >= want or looked_rest >= cover:
                continue
        # No point judging a fifth moment from a streamer we will use twice at most.
        if seen_per[m["streamer"]] >= rules["max_per_streamer"] * 2:
            continue
        looked_ours += 1 if featured(m) else 0
        looked_rest += 0 if featured(m) else 1
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
    # The judge's own scores bunch at 8 and 9, so they barely order anything. The
    # candidates it kept are therefore compared with each other, several times in
    # different orders, and the average place decides. This only orders them: it
    # never rejects one.
    if len(kept) > want and rules["compare_runs"] > 0:
        try:
            import judge
            entries = [{"id": str(spec), "video": Path(m["window"]), "streamer": m["streamer"],
                        "game": m.get("game", "")} for m, spec in kept if m.get("window") and Path(m["window"]).exists()]
            if len(entries) == len(kept):
                table, cost = judge.compare(entries, runs=rules["compare_runs"])
                for m, spec in kept:
                    m["compare"] = {**table[str(spec)], "of": len(kept)}
                kept.sort(key=lambda ms: (ms[0]["compare"]["mean"], -ms[0]["verdict"]["rank"]))
                print(f"\ncompared {len(kept)} candidates with each other, {len(next(iter(table.values()))['ranks'])} "
                      f"valid runs, ${cost:.3f}:")
                for m, _ in kept:
                    c = m["compare"]
                    print(f"   avg place {c['mean']:>4.1f}  {str(c['ranks']):<14} {m['streamer'][:16]:<16} "
                          f"{hms(m['burst_start'])}  {m['verdict']['hook'][:60]}")
            else:
                print("\nsome stretches are no longer on disk: keeping the judge's own order")
        except Exception as e:
            print(f"\nthe comparison could not run ({str(e)[:140]}): keeping the judge's own order")
    if not kept and unjudged:
        print("\nthe judge could not run; falling back to chat ranking with OpenShorts choosing the cuts")
    order = kept + unjudged
    print(f"\njudged {looked}: {len(kept)} worth clipping, {looked - len(kept) - len(unjudged)} rejected")
    for m, _ in kept:
        v = m["verdict"]
        print(f"   {v['rank']:>5}  {m['streamer'][:16]:<16} {hms(m['burst_start'])}  [{v['moment_type']}]  {v['hook']}")

    # The featured game's share: that many of the day's clips, as long as the judge
    # kept that many of its moments and confirmed the game is what is on screen.
    def confirmed(m: dict) -> bool:
        return featured(m) and (m.get("verdict") or {}).get("game_label_ok", True)
    waiting = sum(1 for m, _ in order if confirmed(m))
    target = min(rules["focus_min_clips"], waiting, want) if focus else 0
    if focus:
        print(f"\n{rules['focus_game']} ({'launch week: every clip' if rules['focus_phase'] == 'all' else 'its share'}): "
              f"{waiting} of the kept candidates are it; aiming for {target} of today's {want} clips")
    made, per, tournament, ours_made, others_made = 0, defaultdict(int), 0, 0, 0
    made_at: list[tuple[str, float]] = []      # (stream, seconds in) of today's clips
    tried: set[int] = set()
    while made < want:
        # The best candidate not yet tried that the day's limits still allow. It is
        # looked for from the top each time, so when a clip fails to come out, a
        # candidate passed over earlier gets its chance back.
        pick = None
        for idx, (m, spec) in enumerate(order):
            if idx in tried or per[m["streamer"]] >= rules["max_per_streamer"]:
                continue
            # Two clips a few minutes apart in one stream are two cuts of one event.
            if any(u == m["vod_url"] and abs(t - m["burst_start"]) < rules["same_stream_gap"] for u, t in made_at):
                continue
            feed = any(w in m["streamer"].lower() for w in rules["tournament_words"])
            if feed and tournament >= rules["max_tournament_clips"]:
                continue                       # enough from tournament broadcasts today
            if focus and not confirmed(m):
                # Leave room for the featured clips still to come.
                still = sum(1 for j, (x, _) in enumerate(order) if j not in tried and confirmed(x)
                            and per[x["streamer"]] < rules["max_per_streamer"])
                if others_made >= want - min(target, ours_made + still):
                    continue
            pick = idx
            break
        if pick is None:
            break
        tried.add(pick)
        m, spec = order[pick]
        feed = any(w in m["streamer"].lower() for w in rules["tournament_words"])
        label = f"{m['streamer']} at {hms(m['burst_start'])}"
        print(f"\n=== making clip {made + 1}/{want}: {label}", flush=True)
        m["pick"] = made + 1                       # which of the day's choices this was
        spec.write_text(json.dumps(m, ensure_ascii=False))
        progress.report("Finding the best moments", pool + made, total, f"making {label}", started_at=t0)
        rc = subprocess.run(clip_cmd(m, spec), env=env).returncode
        if rc == 0:
            made += 1
            per[m["streamer"]] += 1
            made_at.append((m["vod_url"], m["burst_start"]))
            tournament += 1 if feed else 0
            ours_made += 1 if confirmed(m) else 0
            others_made += 0 if confirmed(m) else 1
        elif rc == 3:
            print("    turned down on a closer look; moving on to the next candidate")
        else:
            print(f"    clip.py failed (exit {rc}); moving on")
    # Stretches downloaded for candidates that were not used.
    for leftover in (ROOT / "data" / "windows").glob("*.mp4"):
        leftover.unlink(missing_ok=True)
    print(f"\nmade {made} of {want} clips (judged {looked} candidates, {len(kept)} passed)")
    progress.report("Finding the best moments", total, total, f"{made} clips made from {looked} candidates",
                    started_at=t0, finished=True)
    if looked and not made and a.allow_none:
        print("no clip came out of this round; the clips already scheduled stay as they are")
    elif looked and not made:
        # Candidates existed and not one clip came out: something is broken (a
        # key, the clipper), not just a quiet day. Fail loudly so the run is
        # marked failed and its owner hears about it.
        sys.exit("error: there were candidates but no clip could be made")


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
        # A game the owner wants featured is always covered, however it ranks today.
        want_game = featured_today(yaml.safe_load((ROOT / "config" / "posting.yaml").read_text()) or {})[0]
        if want_game and want_game not in {g["name"].lower() for g in games}:
            g = tw.game(want_game)
            if g:
                games.append(g)

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
    ranked = sorted(people.values(), key=lambda p: -p["score"])
    if not a.game:
        # Half the watchlist goes to the featured game's streamers, best first; the
        # rest to the best of everything else.
        fg, _, phase = featured_today(yaml.safe_load((ROOT / "config" / "posting.yaml").read_text()) or {})
        if fg:
            n = max(a.top, a.add)
            # ...but only streamers whose clips are actually being watched: before the
            # game is out its category is full of channels with a handful of views.
            ours = [p for p in ranked if fg in {x.lower() for x in p["games"]} and p["clip_views"] >= 300][: n * 4 // 5 if phase == "all" else n // 2]
            ranked = sorted(ours + [p for p in ranked if p not in ours][: n - len(ours)], key=lambda p: -p["score"])
    ranked = ranked[: max(a.top, a.add)]
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
    ap.add_argument("--max-hours", type=float, default=8, help="of a longer stream, read only this many hours of chat, the last ones (default 8)")
    ap.add_argument("--make", type=int, default=0, help="clip the top N moments (approved streamers only)")
    ap.add_argument("--since-hours", type=float, default=0,
                    help="only moments that happened in the last N hours (Twitch only)")
    ap.add_argument("--allow-none", action="store_true",
                    help="with --make: making no clip is not an error (a round between daily runs)")
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
