#!/usr/bin/env python3
"""The posting queue: give finished clips a time slot, send them, read their views.

    .venv/bin/python src/publish/post.py list            # what is queued
    .venv/bin/python src/publish/post.py plan            # give ready clips their time slots
    .venv/bin/python src/publish/post.py send            # schedule them on the platforms
    .venv/bin/python src/publish/post.py skip <id>       # drop a clip from the queue
    .venv/bin/python src/publish/post.py stats           # pull views for posted clips
    .venv/bin/python src/publish/post.py report          # which captions, games, slots work best
    .venv/bin/python src/publish/post.py growth          # followers per platform, day by day

Posting goes through Upload-Post (official platform APIs). `send` only prints
what it would do unless config/posting.yaml has auto_post: true, or you pass
--yes for that one run. The queue lives in data/queue.json; clip.py adds to it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
QUEUE = ROOT / "data" / "queue.json"
TRACKER = ROOT / "tracker.csv"


# --------------------------------------------------------------------------- #
def config() -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    if os.environ.get("CLIP_DRY_RUN"):       # a trial: nothing is sent, whatever the file says
        cfg["auto_post"] = False
    return cfg


def load() -> list[dict]:
    return json.loads(QUEUE.read_text()) if QUEUE.exists() else []


def save(items: list[dict]) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps([{k: v for k, v in i.items() if k != "_held"} for i in items],
                              indent=1, ensure_ascii=False))
    tmp.replace(QUEUE)


def add(item: dict) -> None:
    """Called by clip.py for every finished clip."""
    items = load()
    if any(i["video"] == item["video"] for i in items):
        return
    item = {"id": f"c{len(items) + 1:04d}", "status": "ready", "scheduled_for": None,
            "request_id": None, "views": {}, "created": dt.datetime.now().isoformat(timespec="seconds"),
            **item}
    items.append(item)
    save(items)


def api_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("UPLOAD_POST_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def client():
    key = api_key()
    if not key:
        sys.exit("error: UPLOAD_POST_API_KEY is empty in .env (needs an Upload-Post plan that includes TikTok)")
    try:
        from upload_post import UploadPostClient
    except ImportError:
        sys.exit("error: run  .venv/bin/pip install upload-post")
    return UploadPostClient(key)


# --------------------------------------------------------------------------- #
def plan(cfg: dict, items: list[dict], now: dt.datetime | None = None) -> list[dict]:
    """Give every ready clip the next free slot. Returns the clips it placed."""
    tz = ZoneInfo(cfg["timezone"])
    now = now or dt.datetime.now(tz)
    taken = {i["scheduled_for"] for i in items if i["scheduled_for"] and i["status"] != "skipped"}
    ready = [i for i in items if i["status"] == "ready" and not i["scheduled_for"]]
    if cfg.get("alternate_streamers"):
        # Round-robin across streamers, best score first within each.
        by: dict[str, list] = {}
        for i in sorted(ready, key=lambda i: -float(i.get("score") or 0)):
            by.setdefault(i.get("streamer", ""), []).append(i)
        ready = []
        while any(by.values()):
            for name in list(by):
                if by[name]:
                    ready.append(by[name].pop(0))
    placed, day = [], now.date()
    slots = [dt.time.fromisoformat(s) for s in cfg["slots"]]
    while ready:
        for t in slots:
            when = dt.datetime.combine(day, t, tz)
            # Leave half an hour so an upload is never scheduled in the past.
            if when < now + dt.timedelta(minutes=30) or when.isoformat() in taken or not ready:
                continue
            item = ready.pop(0)
            item["scheduled_for"] = when.isoformat()
            if not item.get("style"):
                styled = sum(1 for i in items if i.get("style"))
                item["style"] = "long" if styled % 2 == 0 else "short"
            if "ig_tags" not in item:
                tagged = sum(1 for i in items if "ig_tags" in i)
                h = yaml.safe_load((ROOT / "config" / "hashtags.yaml").read_text())
                item["ig_tags"] = not (h.get("instagram_no_tags_test") and tagged % 2)
            taken.add(when.isoformat())
            placed.append(item)
        day += dt.timedelta(days=1)
    return placed


def hashtags(item: dict, platform: str) -> list[str]:
    """Game tags, then the streamer, cut to the platform's count (general tags only when
    the game is unknown)."""
    h = yaml.safe_load((ROOT / "config" / "hashtags.yaml").read_text())
    game = item.get("game") or ""
    game_tags = h["games"].get(game) or (["#" + re.sub(r"\W+", "", game).lower()] if game else [])
    streamer = "#" + re.sub(r"\W+", "", item.get("streamer", "")).lower()
    if platform == "instagram" and item.get("ig_tags") is False:
        return []                                      # the no-hashtag test arm
    tags = [t for t in dict.fromkeys(game_tags[:2] + [streamer] + game_tags[2:] + ([] if game_tags else h["general"]))
            if len(t) > 1 and t not in h.get("banned", [])]
    tags = tags[: h["count"].get(platform, 3)]
    if platform == "youtube" and h.get("youtube_shorts_tag", False):
        tags.append("#shorts")
    return tags


def caption(item: dict, platform: str) -> str:
    """The text for one platform, in the clip's caption style, with credit and tags.

    Styles are compared against each other in `post.py report`:
      long  - the AI's descriptive caption
      short - just the hook line, the way most clip pages write
    """
    cap = item["captions"]
    base = cap["instagram"] if platform in ("instagram", "facebook") else cap["tiktok"]
    # Strip the credit that clip.py appended; it is rebuilt below in one format.
    base = re.sub(r"\s*\(🎥[^)]*\)?\s*$", "", base).strip()
    if item.get("style") == "short":
        base = item["hook"].rstrip(".") + ("" if item["hook"].rstrip().endswith("?") else "")
    # Name the channel in words: a Twitch login is not always the same handle
    # on Instagram or X, and an @ there could tag a stranger.
    # The streamer and the game are named in plain words on every platform: that
    # is what search and the recommenders read, more than the hashtags.
    game = item.get("game") or ""
    credit = f"🎥 {item['streamer']} on Twitch · {game}" if game else f"🎥 {item['streamer']} on Twitch"
    tags = " ".join(hashtags(item, platform))
    # X ranks on replies far above likes, so its post carries the judge's question.
    ask = item.get("question", "").strip() if platform == "x" else ""
    room = (270 if platform == "x" else 2000) - len(credit) - len(tags) - len(ask) - 6
    if len(base) > room:
        base = base[: room - 1].rstrip() + "…"
    return "\n".join(x for x in (base, ask, credit, tags) if x)


def youtube_title(item: dict) -> str:
    title = item["captions"]["youtube_title"].strip()
    game = item.get("game") or ""
    short = {"Counter-Strike": "CS2", "World of Warcraft": "WoW", "League of Legends": "LoL",
             "Grand Theft Auto V": "GTA"}.get(game, game)
    # People search Shorts by game: make sure the title names it.
    named = any(w.lower() in title.lower() for w in (game, short) if w)
    if game and not named and len(title) + len(short) + 3 <= 95:
        title = f"{title} | {short}"
    return title[:95]


def upload_args(cfg: dict, item: dict) -> dict:
    """Exactly what is handed to Upload-Post for one clip."""
    args = {
        "video_path": item["video"],
        "user": cfg["profile"],
        "platforms": list(cfg["platforms"]),
        "title": caption(item, "tiktok"),
        "tiktok_title": caption(item, "tiktok"),
        "instagram_title": caption(item, "instagram"),
        "x_title": caption(item, "x"),
        "facebook_title": caption(item, "facebook"),
        "youtube_title": youtube_title(item),
        "youtube_description": caption(item, "youtube"),
        "tags": [t.lstrip("#") for t in hashtags(item, "youtube")],
        "media_type": "REELS",
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
        "scheduled_date": dt.datetime.fromisoformat(item["scheduled_for"])
                            .astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timezone": cfg["timezone"],
    }
    if "facebook" in cfg["platforms"]:
        args["facebook_page_id"] = cfg.get("facebook_page_id", "")
        args["facebook_media_type"] = "REELS"
    if cfg.get("tiktok_mode") == "draft":
        args["post_mode"] = "MEDIA_UPLOAD"
    return args


def send(cfg: dict, items: list[dict], really: bool, verbose: bool = False) -> int:
    """Hand the planned clips to Upload-Post. Returns how many could not be handed over."""
    todo = [i for i in items if i["status"] == "ready" and i["scheduled_for"] and not i.get("_held")]
    todo.sort(key=lambda i: i["scheduled_for"])
    if not todo:
        print("nothing to send (run `post.py plan` first, or the queue is empty)")
        return 0
    if really and not cfg.get("profile"):
        sys.exit("error: set `profile` in config/posting.yaml to your Upload-Post profile name")
    if really and "facebook" in cfg["platforms"] and not cfg.get("facebook_page_id"):
        sys.exit("error: facebook is in `platforms` but facebook_page_id is empty")
    api = client() if really else None
    for i in todo:
        args = upload_args(cfg, i)
        when = dt.datetime.fromisoformat(i["scheduled_for"]).strftime("%a %d %b %H:%M")
        print(f"{'SEND' if really else 'would send'}  {i['id']}  {when}  -> {', '.join(args['platforms'])}")
        print(f"      {Path(i['video']).name}   [caption style: {i.get('style', 'long')}]")
        if verbose:
            for plat in args["platforms"]:
                text = args["youtube_title"] + "\n" + args["youtube_description"] if plat == "youtube" else args[f"{plat}_title"]
                print(f"      --- {plat}")
                for line in text.splitlines():
                    print(f"          {line}")
        if not really:
            continue
        if not Path(i["video"]).exists():
            i["status"], i["error"] = "failed", "video file is gone"
            continue
        try:
            video = args.pop("video_path")
            res = api.upload_video(video, **args)
            i["request_id"] = res.get("request_id") or res.get("job_id")
            i["status"] = "scheduled"
            i["response"] = {k: res[k] for k in ("success", "message", "job_id", "request_id") if k in res}
        except Exception as e:           # one bad clip must not stop the rest
            i["status"], i["error"] = "failed", str(e)[:300]
            print(f"      FAILED: {i['error']}")
        save(items)
    if not really:
        print("\nNothing was sent. Set auto_post: true in config/posting.yaml, or add --yes for this run.")
    return sum(1 for i in todo if i["status"] == "failed")


def post_now(cfg: dict, items: list[dict], ids: list[str]) -> None:
    """Publish these clips immediately on every platform in the config.

    A clip that already has a slot in Upload-Post is withdrawn there first, so it
    cannot go out a second time when its slot comes round.
    """
    api = client()
    tz = ZoneInfo(cfg["timezone"])
    for cid in ids:
        hit = [i for i in items if i["id"] == cid]
        if not hit:
            sys.exit(f"error: no clip with id {cid}")
        i = hit[0]
        if i["status"] not in ("ready", "scheduled"):
            sys.exit(f"error: {cid} is {i['status']}; only ready or scheduled clips can be posted")
        if not Path(i["video"]).exists():
            sys.exit(f"error: the video file for {cid} is gone")
    for cid in ids:
        i = next(i for i in items if i["id"] == cid)
        old_job = (i.get("response") or {}).get("job_id")
        if i["status"] == "scheduled" and old_job:
            res = api.cancel_scheduled(old_job)
            print(f"{cid}: slot withdrawn ({res.get('message') or res})")
        i["scheduled_for"] = dt.datetime.now(tz).isoformat(timespec="seconds")
        i["status"] = "ready"
        save(items)
        args = upload_args(cfg, i)
        args.pop("scheduled_date"), args.pop("timezone")
        video = args.pop("video_path")
        print(f"POST NOW  {cid}  -> {', '.join(args['platforms'])}   {Path(video).name}")
        try:
            res = api.upload_video(video, **args)
            i["request_id"] = res.get("request_id") or res.get("job_id")
            i["status"] = "scheduled"          # `stats` turns this into posted once views come back
            i["response"] = {k: res[k] for k in ("success", "message", "job_id", "request_id") if k in res}
            i["platforms"] = list(args["platforms"])
            print(f"      accepted: request {i['request_id']}")
        except Exception as e:
            i["status"], i["error"] = "failed", str(e)[:300]
            print(f"      FAILED: {i['error']}")
        save(items)


def resend(cfg: dict, items: list[dict], ids: list[str]) -> None:
    """Withdraw scheduled clips and schedule them again at the same slot, with the
    platforms and text the config gives today (used after adding a platform)."""
    api = client()
    now = dt.datetime.now(dt.timezone.utc)
    todo = []
    for cid in ids:
        i = next((i for i in items if i["id"] == cid), None)
        if not i:
            sys.exit(f"error: no clip with id {cid}")
        if i["status"] != "scheduled" or dt.datetime.fromisoformat(i["scheduled_for"]) <= now:
            print(f"{cid}: left alone (not waiting for a future slot)")
            continue
        if not Path(i["video"]).exists():
            print(f"{cid}: left alone (its video file is gone, so it cannot be sent again)")
            continue
        todo.append(i)
    for i in todo:
        job = (i.get("response") or {}).get("job_id")
        if job:
            res = api.cancel_scheduled(job)
            print(f"{i['id']}: withdrawn ({res.get('message') or res})")
        i["status"] = "ready"
        save(items)
        for other in items:
            other["_held"] = other is not i
        send(cfg, items, really=True)
    for other in items:
        other.pop("_held", None)
    save(items)


PLATFORMS = ("tiktok", "instagram", "youtube", "x", "facebook")
VIEW_KEYS = ("views", "view_count", "video_views", "plays", "play_count", "impressions", "reach")


def _rows(res) -> dict[str, dict]:
    """Find each platform's row in a post-analytics answer, whatever its nesting."""
    found: dict[str, dict] = {}

    def walk(o, hint=None):
        if isinstance(o, dict):
            plat = o.get("platform") if isinstance(o.get("platform"), str) else hint
            if plat in PLATFORMS and ("post_metrics" in o or "post_url" in o or any(k in o for k in VIEW_KEYS)):
                found.setdefault(plat, o)
            for k, v in o.items():
                walk(v, k if k in PLATFORMS else hint)
        elif isinstance(o, list):
            for v in o:
                walk(v, hint)

    walk(res)
    return found


GRACE_MINUTES = 20        # how long after its slot a post may still be on its way


def outcome(res: dict, minutes_late: float) -> tuple[str, dict]:
    """Read Upload-Post's answer about one upload.

    Returns (state, per-platform results). state is `ok` (live everywhere),
    `partial` (live on some platforms, failed on others), `failed` (live
    nowhere), `late` (still not published well after its slot) or `wait`.
    """
    rows = {}
    for r in res.get("results") or []:
        if r.get("platform"):
            rows[r["platform"]] = {
                "ok": bool(r.get("success")),
                "url": r.get("post_url") or r.get("url") or "",
                "error": str(r.get("error_message") or r.get("error") or r.get("message") or "")[:200]}
    total = int(res.get("total") or len(rows) or 0)
    finished = (str(res.get("status", "")).lower() in ("completed", "finished", "failed", "error")
                or (total and int(res.get("completed") or 0) >= total))
    if not finished:
        return ("late" if minutes_late > 90 else "wait"), rows
    good = [p for p, r in rows.items() if r["ok"]]
    if rows and len(good) == len(rows):
        return "ok", rows
    return ("partial" if good else "failed"), rows


def check(cfg: dict, items: list[dict]) -> int:
    """Ask Upload-Post how every post whose slot has passed actually went.

    A clip is marked posted only when at least one platform confirms it. Each
    problem is reported once. Returns how many new problems were found, so the
    caller can fail loudly (the daily run and the after-slot check both do).
    """
    api = client()
    now = dt.datetime.now(dt.timezone.utc)
    checked = problems = 0
    for i in items:
        if i["status"] not in ("scheduled", "posted") or not i.get("request_id") or not i.get("scheduled_for"):
            continue
        if i.get("checked") in ("ok", "partial"):
            continue                                   # settled on an earlier check
        late = (now - dt.datetime.fromisoformat(i["scheduled_for"])).total_seconds() / 60
        if late < GRACE_MINUTES:
            continue
        try:
            res = api.get_status(i["request_id"])
        except Exception as e:                         # a hiccup on their side is not a failed post
            print(f"{i['id']}: could not ask Upload-Post ({str(e)[:120]})")
            continue
        state, rows = outcome(res, late)
        checked += 1
        if state == "wait":
            continue
        new = i.get("checked") != state                # say it once, not at every check
        i["checked"], i["results"] = state, rows
        for plat, r in rows.items():
            if r["url"]:
                i.setdefault("urls", {})[plat] = r["url"]
        when = dt.datetime.fromisoformat(i["scheduled_for"]).strftime("%a %d %b %H:%M")
        if state == "ok":
            i["status"] = "posted"
            print(f"{i['id']}  {when}  live on {', '.join(sorted(rows))}")
            continue
        bad = {p: r["error"] or "no reason given" for p, r in rows.items() if not r["ok"]}
        if state == "partial":
            i["status"] = "posted"
            print(f"{i['id']}  {when}  PROBLEM: failed on {', '.join(sorted(bad))}; live on the rest")
        elif state == "failed":
            i["status"], i["error"] = "failed", "; ".join(f"{p}: {e}" for p, e in bad.items())[:300]
            print(f"{i['id']}  {when}  PROBLEM: did not go out on any platform")
        else:
            print(f"{i['id']}  {when}  PROBLEM: still not published {late:.0f} minutes after its slot")
        for plat, err in bad.items():
            print(f"        {plat}: {err}")
        problems += 1 if new else 0
    save(items)
    print(f"checked {checked} posts: {problems} with new problems")
    return problems


def stats(cfg: dict, items: list[dict]) -> None:
    api = client()
    raw_dir = ROOT / "data" / "stats_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    for i in items:
        if i["status"] != "posted" or not i["request_id"]:
            continue                                   # `check` decides what counts as posted
        try:
            res = api.get_post_analytics(i["request_id"])
        except Exception as e:
            print(f"{i['id']}: {str(e)[:120]}")
            continue
        (raw_dir / f"{i['id']}.json").write_text(json.dumps(res, indent=1, default=str))
        views = {}
        for platform, row in _rows(res).items():
            m = row.get("post_metrics") if isinstance(row.get("post_metrics"), dict) else row
            for k in VIEW_KEYS:
                if isinstance(m.get(k), (int, float)):
                    views[platform] = int(m[k])
                    break
            if row.get("post_url"):
                i.setdefault("urls", {})[platform] = row["post_url"]
        if views:
            i["views"] = views
        # Keep the fuller numbers too (engaged views and view percentage on Shorts,
        # reach, shares and watch time on Instagram): they say more than raw views.
        for platform, row in _rows(res).items():
            m = row.get("post_metrics") if isinstance(row.get("post_metrics"), dict) else {}
            keep = {k: v for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            if keep:
                i.setdefault("metrics", {})[platform] = keep
        print(f"{i['id']}  {sum(views.values()):>8,} views  {views}  {Path(i['video']).name[:50]}")
    save(items)


def growth(cfg: dict) -> None:
    """Append today's follower counts to data/growth.csv and show the change."""
    api = client()
    path = ROOT / "data" / "growth.csv"
    rows = []
    if path.exists():
        rows = [line.split(",") for line in path.read_text().splitlines()[1:] if line]
    today = dt.date.today().isoformat()
    rows = [r for r in rows if r[0] != today]            # one row per platform per day
    res = api.get_analytics(cfg["profile"], platforms=list(cfg["platforms"]),
                            page_id=cfg.get("facebook_page_id") or None)
    data = res.get("analytics", res)
    print(f"{'platform':<11} {'followers':>9} {'since yesterday':>15} {'since start':>11} {'views/reach':>11}")
    for plat in cfg["platforms"]:
        d = data.get(plat) or {}
        if not isinstance(d, dict) or d.get("success") is False:
            print(f"{plat:<11} {'no data':>9}   {str(d.get('message', ''))[:50] if isinstance(d, dict) else ''}")
            continue
        followers = int(d.get("followers") or 0)
        reach = int(float(d.get("views") or d.get("reach") or d.get("impressions") or 0))
        before = [r for r in rows if r[1] == plat]
        day = followers - int(before[-1][2]) if before else 0
        total = followers - int(before[0][2]) if before else 0
        rows.append([today, plat, str(followers), str(reach)])
        print(f"{plat:<11} {followers:>9,} {day:>+15,} {total:>+11,} {reach:>11,}")
    path.write_text("date,platform,followers,reach\n" + "\n".join(",".join(r) for r in rows) + "\n")


def report(items: list[dict]) -> None:
    """Average views per clip, split every way that can be changed."""
    posted = [i for i in items if i.get("views")]
    if not posted:
        print("no view data yet: post some clips, wait a day, then run `post.py stats` and this again")
        return
    def table(title, key):
        groups: dict[str, list[int]] = {}
        for i in posted:
            groups.setdefault(str(key(i)), []).append(sum(i["views"].values()))
        print(f"\n{title:<22} {'clips':>5} {'avg views':>10} {'best':>8}")
        for name, v in sorted(groups.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
            print(f"{name[:22]:<22} {len(v):>5} {sum(v) / len(v):>10,.0f} {max(v):>8,}")
    table("moment type", lambda i: i.get("kind") or "not judged")
    table("judge rating", lambda i: (f"{i['judge']['overall']}/10" if i.get("judge") else "not judged"))
    table("format", lambda i: i.get("format") or "classic")
    table("caption style", lambda i: i.get("style", "long"))
    table("Instagram hashtags", lambda i: "none" if i.get("ig_tags") is False else "with tags")
    table("game", lambda i: i.get("game") or "?")
    table("streamer", lambda i: i.get("streamer") or "?")
    table("time slot", lambda i: dt.datetime.fromisoformat(i["scheduled_for"]).strftime("%H:%M"))
    table("clip length", lambda i: "under 20 s" if i.get("seconds", 0) < 20 else "20 s and over")
    plats: dict[str, list[int]] = {}
    for i in posted:
        for p, v in i["views"].items():
            plats.setdefault(p, []).append(v)
    print(f"\n{'platform':<22} {'clips':>5} {'avg views':>10} {'best':>8}")
    for p, v in sorted(plats.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"{p:<22} {len(v):>5} {sum(v) / len(v):>10,.0f} {max(v):>8,}")
    if len(posted) < 20:
        print(f"\nonly {len(posted)} clips have data; differences this small are mostly luck until about 20")


def show(items: list[dict]) -> None:
    if not items:
        print("the queue is empty")
        return
    print(f"{'id':<6} {'status':<10} {'slot':<17} {'streamer':<14} {'views':>8}  clip")
    for i in items:
        slot = dt.datetime.fromisoformat(i["scheduled_for"]).strftime("%a %d %b %H:%M") if i["scheduled_for"] else "-"
        print(f"{i['id']:<6} {i['status']:<10} {slot:<17} {i.get('streamer', '')[:14]:<14} "
              f"{sum(i['views'].values()) if i['views'] else 0:>8,}  {Path(i['video']).name[:60]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("plan")
    s = sub.add_parser("send")
    s.add_argument("--yes", action="store_true", help="really post, for this run, even if auto_post is false")
    s.add_argument("--only", help="send just this clip id")
    s.add_argument("-v", "--verbose", action="store_true", help="show the full text for every platform")
    k = sub.add_parser("now", help="publish clips immediately instead of at their slot")
    k.add_argument("ids", nargs="+")
    k = sub.add_parser("resend", help="reschedule waiting clips with today's platform list")
    k.add_argument("ids", nargs="+")
    k = sub.add_parser("skip")
    k.add_argument("id")
    sub.add_parser("check", help="confirm that every post whose slot has passed went out")
    sub.add_parser("stats")
    sub.add_parser("report")
    sub.add_parser("growth")
    a = ap.parse_args()

    cfg, items = config(), load()
    if a.cmd == "list":
        show(items)
    elif a.cmd == "plan":
        placed = plan(cfg, items)
        save(items)
        print(f"placed {len(placed)} clips")
        show(items)
    elif a.cmd == "send":
        if a.only:
            if not any(i["id"] == a.only for i in items):
                sys.exit(f"error: no clip with id {a.only}")
            for i in items:          # hold the others back for this run only
                i["_held"] = i["id"] != a.only
        if send(cfg, items, really=bool(cfg.get("auto_post")) or a.yes, verbose=a.verbose):
            sys.exit(1)          # at least one clip could not be handed to Upload-Post
    elif a.cmd == "now":
        post_now(cfg, items, a.ids)
    elif a.cmd == "resend":
        resend(cfg, items, a.ids)
    elif a.cmd == "skip":
        hit = [i for i in items if i["id"] == a.id]
        if not hit:
            sys.exit(f"error: no clip with id {a.id}")
        job = (hit[0].get("response") or {}).get("job_id")
        if hit[0]["status"] == "posted":
            sys.exit(f"error: {a.id} is already posted; delete it on the platforms themselves")
        if hit[0]["status"] == "scheduled" and job:
            # Already handed to Upload-Post: withdraw it there too, or it still goes out.
            res = client().cancel_scheduled(job)
            print(f"withdrawn from Upload-Post: {res.get('message') or res}")
        hit[0]["status"], hit[0]["scheduled_for"] = "skipped", None
        save(items)
        print(f"skipped {a.id}; run `post.py plan` to fill its slot")
    elif a.cmd == "check":
        if check(cfg, items):
            sys.exit(1)
    elif a.cmd == "stats":
        stats(cfg, items)
    elif a.cmd == "report":
        report(items)
    elif a.cmd == "growth":
        growth(cfg)


if __name__ == "__main__":
    main()
