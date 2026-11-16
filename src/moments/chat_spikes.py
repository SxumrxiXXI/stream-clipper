#!/usr/bin/env python3
"""Find the moments in a Twitch VOD or YouTube stream where chat blew up.

    .venv/bin/python src/moments/chat_spikes.py https://www.twitch.tv/videos/<id>

Chat replay is fetched with TwitchDownloaderCLI (cached in data/chat/). Messages are counted
in 10-second buckets and each bucket is compared with the stream's own rolling
baseline, so a busy chat and a quiet chat are judged on the same scale. A burst
is a run of buckets well above that baseline.

Chat reacts after the moment, so each burst becomes a clip window that starts
LEAD seconds before the burst begins and ends TAIL seconds after it peaks.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CHAT_DIR = ROOT / "data" / "chat"

BUCKET = 10            # seconds per bucket
BASELINE_SPAN = 300    # compare each bucket with the +/- 5 minutes around it
MIN_Z = 3.0            # how far above baseline counts as a burst
MIN_CHATTERS = 5       # ignore "bursts" made by a handful of people
LEAD = 30              # clip starts this long before the burst begins
TAIL = 20              # clip ends this long after the burst peaks
MERGE_GAP = 20         # bursts closer than this are one moment

EDGE_START = 240       # ignore the first minutes: greetings, "we're live" spam
EDGE_END = 120         # ...and the goodbye / raid at the end

# What chat is doing during a burst tells us what kind of moment it was.
FAMILIES = {
    "laugh": re.compile(r"\b(lul\w*|lol+|lmf?ao+|kekw*|omegalul|icant|haha+\w*|pepelaugh|xd+|dead)\b|💀|😂|🤣", re.I),
    "hype": re.compile(r"\b(pog\w*|w+|lets? ?go+|o{3,}h?|holy|insane|clutch|gg+|ez+|goat|sheesh|clip( it| that)?|clipped|"
                       r"cracked|banger|cinema)\b|🔥", re.I),
    "shock": re.compile(r"\b(wtf+|what+|omg+|no way|aintnoway|bruh+|monkas|huh+|yo+|nahh+|ayo+)\b|\?{2,}|^\?$|D:", re.I),
    "fail": re.compile(r"\b(f|rip|no{3,}|sadge|l+|oof|unlucky|throw\w*|choke\w*|washed)\b", re.I),
}
HYPE = re.compile("|".join(f.pattern for f in FAMILIES.values()), re.I)

# Automated accounts and commands are not reactions.
BOTS = {"nightbot", "streamelements", "streamlabs", "fossabot", "moobot", "wizebot", "soundalerts",
        "sery_bot", "pokemoncommunitygame", "botrixoficial", "kofistreambot", "tangiabot", "blerp"}


@dataclass
class Spike:
    start: float          # where the clip window starts in the VOD (seconds)
    end: float            # where it ends
    burst_start: float    # first bucket above baseline
    peak: float           # busiest bucket
    messages: int         # messages in the burst
    chatters: int         # distinct people in the burst
    ratio: float          # peak rate / baseline rate
    z: float              # peak height in robust standard deviations
    hype: float           # share of burst messages that are reactions
    kind: str             # laugh / hype / shock / fail, or "talk" when chat is just discussing
    moment: float         # best guess of when the thing happened (chat lags a few seconds)
    vote: bool            # looks like chat answering a poll or prediction, not reacting
    score: float
    sample: list[str]     # a few messages from the peak
    chat: list[tuple[float, str]] | None = None   # (seconds into the stream, message) around the burst


def source_of(url: str) -> tuple[str, str]:
    """-> (platform, id). Twitch VODs and YouTube videos/streams are supported."""
    url = url.strip()
    m = re.search(r"twitch\.tv/(?:[^/]+/)?(?:videos?|v)/(\d+)", url) or re.fullmatch(r"(\d+)", url)
    if m:
        return "twitch", m.group(1)
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/)|youtu\.be/)([\w-]{11})", url)
    if m:
        return "youtube", m.group(1)
    if "kick.com" in url:
        sys.exit("error: Kick has no chat-replay download yet. Export the chat by hand from "
                 "streamscharts.com/tools/chat-logs-downloader, or scan the stream without chat.")
    sys.exit(f"error: not a Twitch VOD or YouTube link: {url}")


def vod_id(url: str) -> str:
    return source_of(url)[1]


def downloader() -> str:
    """Path to TwitchDownloaderCLI: tools/ in this project first, then PATH."""
    local = ROOT / "tools" / "TwitchDownloaderCLI"
    if local.exists():
        return str(local)
    found = shutil.which("TwitchDownloaderCLI")
    if found:
        return found
    sys.exit("error: TwitchDownloaderCLI not found. Put the build for this machine from "
             "https://github.com/lay295/TwitchDownloader/releases in tools/TwitchDownloaderCLI")


def fetch_chat(url: str) -> Path:
    """Download the chat replay once; later calls reuse the file on disk."""
    platform, vid = source_of(url)
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    if platform == "twitch":
        path = CHAT_DIR / f"{vid}.json"
        if path.exists() and path.stat().st_size > 2:
            return path
        tmp = path.with_suffix(".part.json")
        cmd = [downloader(), "chatdownload", "--id", vid, "-o", str(tmp), "--collision", "Overwrite"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            sys.exit(f"error: chat download for Twitch VOD {vid} took over 15 minutes; skipped")
        if out.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            sys.exit(f"error: no chat replay for Twitch VOD {vid}: {(out.stderr or out.stdout).strip()[-300:]}")
        tmp.rename(path)
        return path
    # YouTube: yt-dlp writes the live chat replay as one JSON object per line.
    path = CHAT_DIR / f"{vid}.live_chat.json"
    if path.exists() and path.stat().st_size > 2:
        return path
    # Only finished streams: on a stream that is still live yt-dlp would record chat forever.
    cmd = ["yt-dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-warnings",
           "--match-filter", "live_status=was_live",
           "-o", str(CHAT_DIR / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        sys.exit(f"error: chat download for YouTube video {vid} took over 30 minutes; stopped")
    if not path.exists():
        sys.exit(f"error: no chat replay for YouTube video {vid} (still live, not a past live stream, or replay is off)")
    return path


def _youtube_messages(path: Path) -> list[tuple[float, str, str]]:
    msgs = []
    for line in path.read_text().splitlines():
        try:
            replay = json.loads(line)["replayChatItemAction"]
        except (json.JSONDecodeError, KeyError):
            continue
        t = float(replay.get("videoOffsetTimeMsec", 0)) / 1000
        for act in replay.get("actions", []):
            item = (act.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r or t < 0:
                continue
            who = r.get("authorExternalChannelId") or (r.get("authorName") or {}).get("simpleText") or "?"
            runs = (r.get("message") or {}).get("runs") or []
            text = "".join(x.get("text") or ":" + ((x.get("emoji") or {}).get("shortcuts") or ["emoji"])[0].strip(":") + ":"
                           for x in runs)
            msgs.append((t, who, text))
    msgs.sort()
    return msgs


def load_messages(path: Path) -> list[tuple[float, str, str]]:
    """-> [(offset_seconds, chatter, text)], sorted by time."""
    if path.name.endswith(".live_chat.json"):
        return _youtube_messages(path)
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):                 # TwitchDownloaderCLI: {"comments": [...]}
        raw = raw.get("comments") or []
    msgs = []
    for c in raw:
        node = c.get("node", c)
        t = node.get("contentOffsetSeconds", node.get("content_offset_seconds"))
        if t is None:
            continue
        who = (node.get("commenter") or {})
        who = who.get("name") or who.get("login") or who.get("display_name") or who.get("_id") or "?"
        frags = (node.get("message") or {}).get("fragments") or []
        text = "".join(f.get("text", "") for f in frags) or (node.get("message") or {}).get("body", "")
        msgs.append((float(t), str(who), text))
    msgs.sort()
    return msgs


def detect(msgs: list[tuple[float, str, str]], *, min_z: float = MIN_Z, lead: int = LEAD,
           tail: int = TAIL, top: int | None = None) -> list[Spike]:
    if not msgs:
        return []
    n = int(msgs[-1][0] // BUCKET) + 1
    counts = [0] * n
    people: list[set] = [set() for _ in range(n)]
    texts: list[list[str]] = [[] for _ in range(n)]
    for t, who, text in msgs:
        if who.lower() in BOTS or text.startswith("!"):
            continue
        b = int(t // BUCKET)
        counts[b] += 1
        people[b].add(who)
        texts[b].append(text)
    # One person spamming is not a moment: judge buckets by distinct chatters.
    signal = [len(p) for p in people]

    half = BASELINE_SPAN // BUCKET
    z = [0.0] * n
    base = [0.0] * n
    for i in range(n):
        window = signal[max(0, i - half): i + half + 1]
        med = statistics.median(window)
        mad = statistics.median(abs(x - med) for x in window)
        spread = max(1.4826 * mad, math.sqrt(med + 1), 1.0)   # floor: Poisson noise
        base[i] = med
        z[i] = (signal[i] - med) / spread

    # Runs of buckets above the threshold, merged when close together.
    runs: list[list[int]] = []
    for i in range(n):
        if i * BUCKET < EDGE_START or i * BUCKET > msgs[-1][0] - EDGE_END:
            continue
        if z[i] >= min_z and signal[i] >= MIN_CHATTERS:
            if runs and (i - runs[-1][1]) * BUCKET <= MERGE_GAP + BUCKET:
                runs[-1][1] = i
            else:
                runs.append([i, i])

    spikes = []
    for a, b in runs:
        peak = max(range(a, b + 1), key=lambda i: signal[i])
        burst_msgs = [m for i in range(a, b + 1) for m in texts[i]]
        chatters = len(set().union(*people[a: b + 1]))
        hype = sum(1 for m in burst_msgs if HYPE.search(m)) / max(1, len(burst_msgs))
        ratio = signal[peak] / max(base[peak], 1.0)
        # Chat voting ("1", "2", "left", "E8") floods chat without anything
        # happening on screen: mostly tiny or identical answers, few reactions.
        plain = [m.strip().lower() for m in burst_msgs if not HYPE.search(m)]
        tiny = sum(1 for m in plain if len(m) <= 2) / max(1, len(burst_msgs))
        top_share = (max(plain.count(x) for x in set(plain)) / len(burst_msgs)) if plain else 0.0
        vote = hype < 0.25 and (tiny >= 0.4 or top_share >= 0.4)
        shares = {k: sum(1 for m in burst_msgs if rx.search(m)) / max(1, len(burst_msgs))
                  for k, rx in FAMILIES.items()}
        kind = max(shares, key=shares.get) if max(shares.values()) >= 0.15 else "talk"
        # Size of the reaction matters as well as its height above baseline. A
        # burst with no reactions in it is chat discussing something, which is
        # rarely a moment that works as a clip.
        score = (z[peak] * math.log2(1 + chatters) * (1 + hype)
                 * (0.25 if vote else 1.0) * (0.5 if kind == "talk" else 1.0))
        spikes.append(Spike(
            start=max(0.0, a * BUCKET - lead), end=peak * BUCKET + BUCKET + tail,
            burst_start=a * BUCKET, peak=peak * BUCKET, messages=len(burst_msgs),
            chatters=chatters, ratio=round(ratio, 1), z=round(z[peak], 1),
            hype=round(hype, 2), kind=kind, moment=max(0.0, a * BUCKET - 6),
            vote=vote, score=round(score, 1),
            chat=[(round(t, 1), text[:90]) for t, who, text in msgs
                  if a * BUCKET - 20 <= t <= peak * BUCKET + BUCKET + 10
                  and who.lower() not in BOTS and not text.startswith("!")][:60],
            sample=[m[:80] for m in texts[peak][:5]],
        ))
    spikes.sort(key=lambda s: s.score, reverse=True)
    return spikes[:top] if top else spikes


def hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vod", help="Twitch VOD link or id, or a YouTube stream link")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-z", type=float, default=MIN_Z)
    ap.add_argument("--lead", type=int, default=LEAD, help=f"seconds before the burst (default {LEAD})")
    ap.add_argument("--tail", type=int, default=TAIL, help=f"seconds after the peak (default {TAIL})")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    msgs = load_messages(fetch_chat(a.vod))
    spikes = detect(msgs, min_z=a.min_z, lead=a.lead, tail=a.tail, top=a.top)
    if a.json:
        print(json.dumps([{k: v for k, v in asdict(s).items() if k != "chat"} for s in spikes], indent=1))
        return
    dur = msgs[-1][0] if msgs else 0
    print(f"{len(msgs)} messages over {hms(dur)}; {len(spikes)} moments\n")
    print(f"{'#':>2}  {'clip window':<19} {'peak':>8} {'chatters':>8} {'x base':>7} {'score':>6}  sample")
    for i, s in enumerate(spikes, 1):
        print(f"{i:>2}  {hms(s.start)}-{hms(s.end)}  {hms(s.peak):>8} {s.chatters:>8} {s.ratio:>7} {s.score:>6}  "
              f"[{s.kind}] {'[vote?] ' if s.vote else ''}{' | '.join(s.sample[:3])[:60]}")


if __name__ == "__main__":
    main()
