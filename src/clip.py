#!/usr/bin/env python3
"""Turn one source (VOD/clip link or local file) into ready-to-post clips.

    .venv/bin/python src/clip.py <link-or-file> --permission "<where clipping is allowed>"

It drives the local OpenShorts backend (vendor/openshorts, http://localhost:8001):
OpenShorts picks the moments and reframes them; this script then burns the hook
and captions the way the campaign template says, downloads the finished clips to
clips/<date>_<name>/ and writes posts.md with a caption and hashtags per clip.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / "publish"))
import post as queue  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent / "moments"))

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8001"
OPENSHORTS_DIR = ROOT / "vendor" / "openshorts"
DOCKER_BIN = "/Applications/Docker.app/Contents/Resources/bin"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def die(msg: str) -> None:
    sys.exit(f"error: {msg}")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_env() -> dict:
    path = ROOT / ".env"
    if not path.exists():
        die(".env not found. Copy .env.example to .env and add GEMINI_API_KEY.")
    env = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if not env.get("GEMINI_API_KEY"):
        die("GEMINI_API_KEY is empty in .env")
    return env


def load_campaign(slug: str) -> dict:
    path = ROOT / "config" / "campaigns" / f"{slug}.yaml"
    if not path.exists():
        die(f"no campaign file at {path}")
    return yaml.safe_load(path.read_text())


# --------------------------------------------------------------------------- #
# OpenShorts
# --------------------------------------------------------------------------- #
def backend_up() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health/ready", timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ensure_backend() -> None:
    """Start OpenShorts if it is not answering. Works on the Mac (Docker Desktop)
    and on a Linux server (Docker runs as a system service there)."""
    if backend_up():
        return
    log("OpenShorts is not running; starting it")
    env = docker_env()
    if subprocess.run(["docker", "info"], env=env, capture_output=True).returncode != 0:
        if sys.platform != "darwin":
            die("Docker is not running (try: sudo systemctl start docker)")
        # After a restart Docker Desktop itself may not be open yet.
        log("opening Docker Desktop")
        subprocess.run(["open", "-ga", "Docker"])
        for _ in range(60):
            time.sleep(3)
            if subprocess.run(["docker", "info"], env=env, capture_output=True).returncode == 0:
                break
        else:
            die("Docker Desktop did not start within 3 minutes")
    # OPENSHORTS_SERVICES=backend on a server: the dashboard is not needed there
    # and must not be reachable from the internet.
    services = os.environ.get("OPENSHORTS_SERVICES", "").split()
    try:
        subprocess.run(["docker", "compose", "up", "-d", *services], cwd=OPENSHORTS_DIR,
                       env=env, check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        die(f"could not start OpenShorts ({e}). Is Docker running?")
    for _ in range(60):
        if backend_up():
            return
        time.sleep(3)
    die("OpenShorts did not become ready within 3 minutes")


def api_json(method: str, path: str, body: dict | None = None, key: str | None = None,
             allow: str | None = None) -> dict | None:
    """Call OpenShorts. An error stops the run, except one whose text contains
    `allow`, which returns None so the caller can carry on without that step."""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-Gemini-Key"] = key
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read()[:500].decode("utf-8", "replace")
        if allow and allow in text:
            return None
        die(f"{method} {path} -> HTTP {e.code}: {text}")


def submit(source: str, is_file: bool, camp: dict, n_clips: int, key: str,
           start: float | None = None, end: float | None = None) -> str:
    clip = camp["clip"]
    fields = {
        "acknowledged": "true",
        "output_format": "vertical",
        "layouts": "auto",          # OpenShorts decides: facecam split, screen, or plain
        "target_clips": str(n_clips),
        "clip_min_seconds": str(clip["min_seconds"]),
        "clip_max_seconds": str(clip["max_seconds"]),
        "auto_hook": "false",       # hook and captions are burned below, to the template
        "captions": "false",
    }
    if is_file and start is not None and end is not None:
        fields["source_start"] = str(start)
        fields["source_end"] = str(end)
    # curl streams large uploads; the key travels in a config read from stdin so
    # it never shows up in the process list.
    cfg = [f'header = "X-Gemini-Key: {key}"']
    args = ["curl", "-sS", "-K", "-", "-X", "POST", f"{API}/api/process"]
    for k, v in fields.items():
        args += ["--form-string", f"{k}={v}"]
    if is_file:
        args += ["-F", f"file=@{source}"]
    else:
        args += ["--form-string", f"url={source}"]
    out = subprocess.run(args, input="\n".join(cfg), capture_output=True, text=True)
    if out.returncode != 0:
        die(f"upload failed: {out.stderr.strip()[:300]}")
    try:
        resp = json.loads(out.stdout)
    except json.JSONDecodeError:
        die(f"unexpected reply from OpenShorts: {out.stdout[:300]}")
    if "job_id" not in resp:
        die(f"OpenShorts refused the job: {out.stdout[:400]}")
    return resp["job_id"]


def fetch_window(url: str, start: float, end: float) -> Path:
    """Download only [start, end] of a VOD (OpenShorts would fetch all of it)."""
    WINDOWS = ROOT / "data" / "windows"
    WINDOWS.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"\W+", "_", urllib.parse.urlparse(url).path).strip("_")[-40:]
    out = WINDOWS / f"{stem}_{int(start)}-{int(end)}.mp4"
    if out.exists() and out.stat().st_size > 100_000:
        return out
    cmd = ["yt-dlp", "--no-warnings", "-q", "--socket-timeout", "30", "--retries", "3",
           "--download-sections", f"*{int(start)}-{int(end)}",
           "--force-keyframes-at-cuts", "-f", "best[height<=1080]/best", "--merge-output-format", "mp4",
           "-o", str(out), url]
    try:
        # A stalled download once held a run up for two hours. Ninety seconds of
        # video should take two or three minutes; give it eight, then give up.
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=480)
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        die("could not download that stretch of the stream: it stalled for 8 minutes")
    if res.returncode != 0 or not out.exists():
        die(f"could not download that stretch of the stream: {res.stderr.strip()[-300:]}")
    return out


def data_line(moment: dict, seed: int = 0) -> str:
    """One sentence about how the moment was received, from the scanner's own
    numbers: true by construction, and something the footage does not show.
    The strongest fact available is used, in one of a few wordings so that
    clips do not all wear the same sentence. "" when nothing is worth saying."""
    people, ratio = int(moment.get("chatters") or 0), round(float(moment.get("chat_ratio") or 0))
    views = int(moment.get("viewer_clip_views") or 0)
    hours = int(float(moment.get("stream_seconds") or 0) // 3600)
    if moment.get("stream_peak") and hours >= 2:
        say = [f"Chat's biggest reaction in {hours} hours of stream",
               f"No moment in {hours} hours of stream got chat going more",
               f"{hours} hours of stream, and chat reacted most to this"]
    elif views >= 500:
        say = [f"Viewers' own Twitch clips of this: {views:,} views",
               f"Twitch viewers clipped this themselves: {views:,} views so far",
               f"{views:,} views on viewers' own Twitch clips of this"]
    elif ratio >= 5 and people >= 40:
        say = [f"Chat was {ratio} times busier than usual here",
               f"Chat ran at {ratio}x its usual pace for this",
               f"{ratio}x the usual chat: {people} people reacted"]
    elif people >= 60:
        say = [f"{people} people in chat reacted to this",
               f"{people} different chatters reacted to this one",
               f"Chat's reaction: {people} people typing"]
    else:
        return ""
    return say[seed % len(say)]


def word_times(job_id: str) -> list[tuple[float, str]]:
    """(time, word) for every word OpenShorts transcribed, in window seconds."""
    out = []
    for seg in transcript_of(job_id):
        words = seg.get("words") or []
        if words:
            out += [(float(w["start"]), str(w.get("word") or "")) for w in words]
        else:                                   # no word timings: spread the line evenly
            said = (seg.get("text") or "").split()
            a, b = float(seg["start"]), float(seg["end"])
            out += [(a + (b - a) * n / max(1, len(said)), w) for n, w in enumerate(said)]
    return sorted(out)


def labelled_transcript(job_id: str, parts: list[list[float]]) -> list[tuple[str, float, str]]:
    """The window's speech as (BEFORE | IN CLIP | AFTER, seconds, text), one
    entry per spoken line. A line the clip's edge falls inside counts as IN
    CLIP whole: half a sentence is not something to quote. Lines cut out
    between two pieces of the clip are left out: neither heard nor 'after'."""
    out = []
    for seg in transcript_of(job_id):
        text = " ".join((seg.get("text") or "").split())
        times = [float(w["start"]) for w in seg.get("words") or []] or [float(seg["start"])]
        if not text:
            continue
        if any(a - 0.2 <= t <= b + 0.2 for t in times for a, b in parts):
            label = "IN CLIP"
        elif times[-1] < parts[0][0]:
            label = "BEFORE"
        elif times[0] > parts[-1][1]:
            label = "AFTER"
        else:
            continue
        out.append((label, times[0], text))
    return out


FRAMES = {"before": ["Moments before:", "Seconds earlier:", "Just before this:", "Right before:"],
          "after": ["Right after:", "Seconds later:", "Afterwards:", "And then:"]}
QUOTE_REACH = 30.0      # seconds from the clip within which words still belong to the moment
# Words that end a clip's chances: said inside the cut, they are in the sound and
# in the burned captions of everything the page posts.
SLURS = re.compile(r"\b(retard\w*|fag(got)?s?|nigg\w*|trann(y|ies)|kikes?|chinks?|spics?|rape[ds]?|raping|rapists?|"
                   r"kill (yo)?urself|kys)\b", re.I)
# Words that are fine in a streamer's mouth but not in large type under the
# page's name: platforms hold swearing and sensitive subjects in on-screen text
# against a post.
QUOTE_BLOCK = re.compile(
    r"\b(\w*fuck\w*|\w*shit\w*|bitch\w*|cunt\w*|dick\w*|cock\w*|puss\w*|ass(hole)?s?|bastard\w*|whore\w*|slut\w*|"
    r"retard\w*|fag\w*|nigg\w*|rape\w*|rapist\w*|suicid\w*|kill (yo)?urself|kys|nazi\w*|hitler|autis\w*|"
    r"cancer|porn\w*|sex\w*|naked|nude\w*|drug\w*|cocaine|heroin|terroris\w*|shoot up|bomb\w*)\b", re.I)


def context_lines(verdict: dict, parts: list[list[float]], job_id: str, moment: dict | None,
                  args) -> tuple[str, str]:
    """-> (line shown until the payoff, line shown after it); either may be "".

    The lines are the part of the post that is ours: something true the viewer
    cannot get from the clip. A line is words spoken on the stream just outside
    the clip, printed exactly, in quotation marks, behind a few words of ours
    ('Moments before: "..."'). Nothing is reworded, so nothing can be misstated.
    An excerpt is printed only if:
    1. it is in the transcript word for word, outside the clip, on the right
       side of it and within half a minute of it (a plain search);
    2. a second, separate model call finds it plainly about the clip's event,
       understandable alone and, for the opening line, not a spoiler, and picks
       it as the one worth printing. It is free to pick none.
    If no words from after the clip qualify, the line after the payoff is one of
    the scanner's own facts about how the moment was received.
    """
    import judge
    script = labelled_transcript(job_id, parts)
    picks, cost = judge.pick_quotes(what=verdict.get("what_happens", ""), streamer=args.streamer,
                                    game=args.game, transcript=script)
    words = word_times(job_id)
    rows = []
    for role in ("before", "after"):
        for q in picks[role]:
            at = judge.find_quote(q, words)
            n = len(q.split())
            if at is None:
                problem = "not in the transcript word for word"
            elif not 3 <= n <= 10:
                problem = "too short or too long"
            elif QUOTE_BLOCK.search(q):
                problem = "not words to print in large type"
            elif any(a - 0.5 <= (at[0] + at[1]) / 2 <= b + 0.5 for a, b in parts):
                problem = "repeats the clip itself"
            elif role == "before" and not parts[0][0] - QUOTE_REACH <= at[1] < parts[0][0]:
                problem = "not said just before the clip"
            elif role == "after" and not parts[-1][1] < at[0] <= parts[-1][1] + QUOTE_REACH:
                problem = "not said just after the clip"
            else:
                problem = ""
            rows.append({"role": role, "quote": q, "problem": problem})
    alive = [r for r in rows if not r["problem"]]
    chosen = {"before": None, "after": None}
    if alive:
        answers, best = judge.screen_quotes(verdict.get("what_happens", ""), [(r["role"], r["quote"]) for r in alive])
        for r, a in zip(alive, answers):
            if not a["about_the_event"]:
                r["problem"] = "about something else"
            elif not a["clear_alone"]:
                r["problem"] = "unclear without the stream"
            elif r["role"] == "before" and a["spoils"]:
                r["problem"] = "gives the ending away"
        for role in ("before", "after"):
            n = best[role]
            if n is not None and not alive[n]["problem"]:
                chosen[role] = alive[n]
            for r in alive:
                if r["role"] == role and not r["problem"] and r is not chosen[role]:
                    r["problem"] = "passed, not the pick"
    for r in rows:
        log(f"       {r['role']:<6} {r['problem'] or 'PRINT':<36} \"{r['quote']}\"")
    verdict["context_candidates"] = rows
    seed = int(float((moment or {}).get("burst_start") or verdict.get("payoff_at") or 0)) // 10

    def line(role: str) -> str:
        if not chosen[role]:
            return ""
        said = chosen[role]["quote"].strip(" ,;:-").split()
        while len(said) > 3 and said[-1].lower().strip(".,!?") in judge.DANGLING:
            said.pop()                      # "Dumb and Dumber a": the transcript line broke off there
        q = " ".join(said)
        return f'{FRAMES[role][seed % len(FRAMES[role])]} "{q[:1].upper() + q[1:]}"'

    before, after = line("before"), line("after")
    if not after:
        after = data_line(moment or {}, seed)
        if after:
            log(f"       after  from our own numbers                 \"{after}\"")
    return before, after


def judge_moment(window: Path, moment: dict, args) -> dict | None:
    """Have the judge watch the window. None means it could not run (then the
    clip is made the old way, with OpenShorts choosing the cut)."""
    import judge
    ws = args.start
    dur = args.end - args.start
    try:
        v = judge.judge(
            window, dur, burst=float(moment["burst_start"]) - ws,
            peak=float(moment.get("peak") or moment["burst_start"]) - ws,
            chat=[(t - ws, text) for t, text in moment.get("chat") or []],
            streamer=args.streamer, game=args.game, title=moment.get("vod_title", ""),
            kind=moment.get("kind", ""),
            viewer_clips=[(o - ws, title, views) for o, title, views in moment.get("clips") or []])
    except Exception as e:
        log(f"judge unavailable ({str(e)[:160]}); letting OpenShorts choose the cut")
        return None
    log(f"judge: {'KEEP' if v['keep'] else 'REJECT'}  overall {v['overall']}/10, standalone {v['standalone']}/10, "
        f"opening {v['opening']}/10, payoff {v['payoff']}/10  [{v['moment_type']}]  ${v['cost_usd']}")
    log(f"       {v['what_happens'][:200]}")
    if v["rejected_because"]:
        log(f"       rejected: {'; '.join(v['rejected_because'])}")
    elif v["problems"]:
        log(f"       weak points: {'; '.join(v['problems'])[:200]}")
    if v["keep"]:
        log(f"       cut {v['start']:.1f}-{v['end']:.1f} s (from {v['cut_from']}), hook: {v['hook']}")
    judged = ROOT / "data" / "judged.jsonl"
    judged.parent.mkdir(exist_ok=True)
    with open(judged, "a") as f:
        f.write(json.dumps({
            "at": dt.datetime.now().isoformat(timespec="seconds"), "source": args.source,
            "streamer": args.streamer, "start": args.start, "end": args.end, "keep": v["keep"],
            **({"live": True} if os.environ.get("CLIP_LIVE") else {}),      # asked by the live watcher
            # Rejections are remembered so the same moment is not judged again tomorrow.
            **{k: v[k] for k in ("overall", "standalone", "opening", "payoff", "rank", "moment_type",
                                 "what_happens", "rejected_because", "problems", "hook", "cost_usd")},
        }, ensure_ascii=False) + "\n")
    return v


def wait(job_id: str) -> dict:
    seen = 0
    while True:
        st = api_json("GET", f"/api/status/{job_id}")
        logs = st.get("logs") or []
        for line in logs[seen:]:
            line = str(line).strip()
            # Progress bars repaint on one line; keep the milestones only.
            if line and "it/s]" not in line and len(line) < 220:
                print("    " + line, flush=True)
        seen = len(logs)
        if st.get("status") == "completed":
            return st.get("result") or {}
        if st.get("status") in ("failed", "error", "cancelled"):
            die(f"OpenShorts job {st.get('status')}. Last log lines are above.")
        time.sleep(8)


def preview(video: Path, name: str) -> None:
    """Save a strip of frames (one every 3 s) to data/previews/, small enough to keep.

    The clip file itself may not outlive the run (a hosted runner is wiped), and
    this is what lets someone check afterwards how it was framed and captioned.
    """
    out = ROOT / "data" / "previews" / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', name)}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "fps=1/3,scale=270:-1,tile=10x1",
                    "-frames:v", "1", "-q:v", "5", str(out)], capture_output=True)


def docker_env() -> dict:
    return {**os.environ, "PATH": f"{DOCKER_BIN}:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}


def clip_time(t: float, parts: list[list[float]]) -> float:
    """A time in the downloaded stretch -> seconds into the finished clip (which may
    be two pieces with a gap cut out)."""
    acc = 0.0
    for a, b in parts:
        if t <= b:
            return acc + max(0.0, t - a)
        acc += b - a
    return acc


def ass_time(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def landscape(camp: dict) -> bool:
    """The layout the owner asked for on 2026-11-19: the whole landscape picture
    across the middle of the 9:16 frame, over a blurred, zoomed copy of itself,
    with everything we print in the blurred bands above and below it."""
    return str((camp.get("render") or {}).get("fill", "")).lower() == "landscape_blur"


def caption_chunks(job_id: str, parts: list[list[float]], max_chars: int,
                   max_seconds: float = 1.4) -> list[tuple[float, float, str]]:
    """The words spoken inside the clip as (start, end, text) in clip seconds,
    two or three words at a time."""
    words = []
    for seg in transcript_of(job_id):
        for w in seg.get("words") or []:
            try:
                t0, t1, text = float(w["start"]), float(w["end"]), str(w.get("word") or "").strip()
            except (KeyError, TypeError, ValueError):
                continue
            if text and any(a - 0.05 <= (t0 + t1) / 2 <= b + 0.05 for a, b in parts):
                words.append((clip_time(t0, parts), clip_time(t1, parts), text))
    chunks, cur = [], []
    for w in words:
        if cur and (len(" ".join(x[2] for x in cur + [w])) > max_chars
                    or w[1] - cur[0][0] > max_seconds or w[0] - cur[-1][1] > 0.7):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    out = []
    for n, c in enumerate(chunks):
        start, end = c[0][0], c[-1][1] + 0.3
        if n + 1 < len(chunks):
            end = min(end, chunks[n + 1][0][0])
        out.append((start, max(end, start + 0.25), " ".join(x[2] for x in c)))
    return out


def draw_text(job_id: str, video_url: str, *, parts: list[list[float]], aspect: float, camp: dict,
              hook: str = "", before: str = "", after: str = "", payoff: float = 0.0) -> str:
    """Burn everything we print onto a clip in the landscape layout, in one pass,
    and return the new video URL.

    The picture is a band across the middle. Captions sit directly under it and
    grow downwards; the hook and the context lines sit directly above it and
    grow upwards. Nothing covers the gameplay, and nothing is low enough to end
    up under the platforms' own buttons and caption text (roughly the bottom
    quarter of the screen).
    """
    name = urllib.parse.unquote(video_url.rsplit("/", 1)[-1])
    src = OPENSHORTS_DIR / "output" / job_id / name
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError:
        w, h = 1080, 1920
    r = camp["render"]
    picture = min(h, int(round(w / max(aspect, 0.1))))
    top = (h - picture) // 2
    bottom = top + picture
    gap, side = round(h * 0.016), round(w * 0.06)
    cap_size, line_size = round(h * 0.068), round(h * 0.051)
    outline = max(5, round(h * 0.0045))
    total = sum(b - a for a, b in parts)

    def esc(text: str) -> str:
        return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()

    def style(label: str, size: int, colour: str, align: int, margin_v: int) -> str:
        return (f"Style: {label},Anton,{size},{colour},{colour},&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,"
                f"{outline},0,{align},{side},{side},{margin_v},1")

    events = []
    caps = r["captions"]
    for a, b, text in caption_chunks(job_id, parts, int(caps.get("max_chars", 16))):
        text = text.upper() if caps.get("uppercase", True) else text
        events.append(f"Dialogue: 1,{ass_time(a)},{ass_time(min(b, total))},Cap,,0,0,0,,{esc(text)}")
    if not events:
        log("no speech in this cut: keeping the clip without captions")
    payoff = min(max(payoff + 0.3, 2.5), max(2.5, total - 2.5))
    if hook and (r.get("hook") or {}).get("enabled", True):
        seconds = float((r.get("hook") or {}).get("duration_seconds", 2))
        events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(seconds)},Line,,0,0,0,,{{\\fad(0,150)}}{esc(hook)}")
    if before:
        events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(payoff)},Line,,0,0,0,,{{\\fad(120,120)}}{esc(before)}")
    if after:
        late = max(payoff, total - 7.0)        # it is about what came after: the clip's last seconds
        events.append(f"Dialogue: 0,{ass_time(late)},{ass_time(total + 5)},After,,0,0,0,,{{\\fad(150,0)}}{esc(after)}")
    if not events:
        return video_url
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        style("Cap", cap_size, "&H00FFFFFF", 8, bottom + gap // 2),         # top edge just under the picture
        style("Line", line_size, "&H00FFFFFF", 2, h - top + gap),           # bottom edge just above it
        style("After", line_size, "&H0000E6FF", 2, h - top + gap),          # yellow (BGR)
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, ""])
    sub = OPENSHORTS_DIR / "uploads" / f"txt_{job_id}.ass"
    sub.parent.mkdir(exist_ok=True)
    sub.write_text(ass)
    out_name = "text_" + name
    cmd = ["docker", "compose", "exec", "-T", "backend", "ffmpeg", "-v", "error", "-y",
           "-i", f"/app/output/{job_id}/{name}",
           "-vf", f"ass=/app/uploads/{sub.name}:fontsdir=/app/fonts",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", f"/app/output/{job_id}/{out_name}"]
    res = subprocess.run(cmd, cwd=OPENSHORTS_DIR, env=docker_env(), capture_output=True, text=True)
    sub.unlink(missing_ok=True)
    if res.returncode != 0:
        die(f"could not draw the text on the clip: {res.stderr.strip()[-300:]}")
    return f"/videos/{job_id}/{urllib.parse.quote(out_name)}"


def reframe(job_id: str, index: int, parts: list[list[float]], key: str, wide: bool) -> tuple[str, list[list[float]]]:
    """Cut clip `index` to `parts` (window seconds) and, for the landscape layout,
    have OpenShorts show the whole frame over a blurred copy of itself. Returns
    the new video URL and the pieces as OpenShorts snapped them to word edges."""
    body = {"job_id": job_id, "clip_index": index, "snap_to_words": True, "reapply_captions": False,
            "segments": [{"start": a, "end": b} for a, b in parts]}
    if wide:
        body["framing"] = "full"
    res = api_json("POST", "/api/clip/rerender", body, key) or {}
    snapped = ((res.get("recipe") or {}).get("segments")) or res.get("segments") or []
    try:
        snapped = [[float(x["start"]), float(x["end"])] for x in snapped]
    except (KeyError, TypeError, ValueError):
        snapped = []
    return res.get("new_video_url") or res.get("video_url") or "", snapped or parts


def add_context(job_id: str, video_url: str, before: str, after: str, payoff: float, total: float) -> str:
    """Burn the page's own context onto a finished clip and return the new video URL.

    `before` (the stakes) is on screen from the start until the payoff, `after`
    (what it led to) from the payoff to the end, in a second colour. This is the
    part of the post that is ours and not the streamer's: the platforms no longer
    count captions or a reframe as making a clip original, but they do count
    on-video text that tells the viewer something the footage does not.

    The text is drawn by ffmpeg inside the OpenShorts container, which has the
    Anton font and libass on every machine the pipeline runs on.
    """
    name = urllib.parse.unquote(video_url.rsplit("/", 1)[-1])
    src = OPENSHORTS_DIR / "output" / job_id / name
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError:
        w, h = 1080, 1920
    size, margin_v, margin_h, outline = round(h * 0.066), round(h * 0.11), round(w * 0.07), max(4, round(h * 0.004))

    def esc(text: str) -> str:
        return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()

    def style(name: str, colour: str) -> str:
        return (f"Style: {name},Anton,{size},{colour},{colour},&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,"
                f"{outline},2,8,{margin_h},{margin_h},{margin_v},1")

    payoff = min(max(payoff + 0.3, 2.5), max(2.5, total - 2.5)) if after else total
    # The second line belongs to the end of the clip: it is about what came after,
    # and the judge's payoff time can be early. Seven seconds is enough to read it.
    late = max(payoff, total - 7.0)
    events = []
    if before:
        events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(payoff)},Before,,0,0,0,,{{\\fad(120,120)}}{esc(before)}")
    if after:
        events.append(f"Dialogue: 0,{ass_time(late)},{ass_time(total + 5)},After,,0,0,0,,{{\\fad(150,0)}}{esc(after)}")
    ass = "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        style("Before", "&H00FFFFFF"), style("After", "&H0000E6FF"),      # white, then yellow (BGR)
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events, ""])
    sub = OPENSHORTS_DIR / "uploads" / f"ctx_{job_id}.ass"
    sub.parent.mkdir(exist_ok=True)
    sub.write_text(ass)
    out_name = "context_" + name
    cmd = ["docker", "compose", "exec", "-T", "backend", "ffmpeg", "-v", "error", "-y",
           "-i", f"/app/output/{job_id}/{name}",
           "-vf", f"ass=/app/uploads/{sub.name}:fontsdir=/app/fonts",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", f"/app/output/{job_id}/{out_name}"]
    res = subprocess.run(cmd, cwd=OPENSHORTS_DIR, env=docker_env(), capture_output=True, text=True)
    sub.unlink(missing_ok=True)
    if res.returncode != 0:
        die(f"could not draw the context lines: {res.stderr.strip()[-300:]}")
    return f"/videos/{job_id}/{urllib.parse.quote(out_name)}"


def transcript_of(job_id: str) -> list[dict]:
    """Every spoken line of the job's transcript, as OpenShorts saved it."""
    for f in (OPENSHORTS_DIR / "output" / job_id).glob("*_metadata.json"):
        try:
            return (json.loads(f.read_text()).get("transcript") or {}).get("segments") or []
        except (OSError, ValueError):
            pass
    return []


def spoken_lines(job_id: str) -> list[tuple[float, float, int]] | None:
    """(start, end, words) of every spoken line OpenShorts transcribed, in window
    seconds. None when the transcript could not be read at all."""
    out, found = [], False
    for f in (ROOT / "vendor" / "openshorts" / "output" / job_id).glob("*_metadata.json"):
        try:
            segs = (json.loads(f.read_text()).get("transcript") or {}).get("segments") or []
        except (OSError, ValueError):
            continue
        found = True
        for s in segs:
            n = len((s.get("text") or "").split())
            if n:
                out.append((float(s["start"]), float(s["end"]), n))
    return sorted(out) if found else None


def fit_to_speech(start: float, end: float, lines: list[tuple[float, float, int]], limit: float,
                  max_len: float = 30.0) -> list[list[float]]:
    """Adjust the judged cut to the real word timings so the reaction is not left behind.

    The judge reads times off the video and is a few seconds loose. Two repairs, both
    using the transcript's exact timings:
    1. a line being spoken at the cut's end, or starting within 1.5 s of it, is allowed
       to finish (up to 6 s more);
    2. if the cut is almost wordless and the streamer speaks within 15 s after it, the
       silence is cut out and those lines are appended as a second piece.
    Returns the pieces to keep, as [start, end] in window seconds.
    """
    # Measured from the judged end only: one line may finish, never a chain of them,
    # or commentators who never pause would drag every clip out to the maximum.
    finish = [b for a, b, _ in lines if a <= end + 1.5 and end < b <= end + 6 and b + 0.4 - start <= max_len]
    end = min(max(finish) + 0.4 if finish else end, limit)
    parts = [[start, end]]
    words_in = sum(n for a, b, n in lines if start <= (a + b) / 2 <= end)
    later = [(a, b) for a, b, _ in lines if end + 1.5 < a <= end + 15]
    room = max_len - (end - start)
    if words_in < 5 and later and room >= 3:
        a, b = later[0]
        for a2, b2 in later[1:]:
            if a2 - b <= 1.5:              # he keeps talking: take the next line too
                b = b2
        a = max(end, a - 0.3)
        parts.append([a, min(b + 0.5, a + room, limit)])
    return parts


def burn(job_id: str, index: int, hook_text: str, camp: dict, key: str) -> str:
    """Burn the hook, then the captions, per the template. Returns the video URL path."""
    r = camp["render"]
    hook, caps = r["hook"], r["captions"]
    filename = None
    if hook.get("enabled") and hook_text:
        res = api_json("POST", "/api/hook", {
            "job_id": job_id, "clip_index": index, "text": hook_text,
            "position": hook.get("position", "top"), "size": "L",
            "duration_seconds": float(hook["duration_seconds"]),
            "style": hook.get("style", "outline"), "font": hook.get("font", "anton"),
        }, key)
        filename = res["new_video_url"].rsplit("/", 1)[-1]
    body = {
        "job_id": job_id, "clip_index": index,
        # OpenShorts places captions at top / middle / bottom only. "middle" is
        # the nearest it has to the seam line when there is no facecam.
        "position": caps.get("openshorts_position", "middle"),
        "style": "karaoke", "effect": "none",
        "font_name": "Anton", "font_size": int(caps.get("font_size", 44)),
        "font_color": caps["color"], "highlight_color": caps["color"],
        "border_color": caps["outline_color"], "border_width": int(caps["outline_px"]),
        "uppercase": bool(caps.get("uppercase", True)),
        "max_chars": int(caps.get("max_chars", 16)), "max_duration": 1.4,
    }
    if filename:
        body["input_filename"] = urllib.parse.unquote(filename)
    res = api_json("POST", "/api/subtitle", body, key, allow="No words found")
    if res is None:
        # Nobody speaks in this cut (pure gameplay with crowd or game sound).
        # The clip is still good: keep the hook and go without captions.
        log("no speech in this cut: keeping the clip without captions")
        if not filename:
            die("this cut has no speech and no hook, so there is nothing to burn onto it")
        return f"/videos/{job_id}/{filename}"
    return res["new_video_url"]


def download(url_path: str, dest: Path) -> None:
    url = API + urllib.parse.quote(urllib.parse.unquote(url_path))
    with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


# --------------------------------------------------------------------------- #
# Post text
# --------------------------------------------------------------------------- #
TAG = re.compile(r"#\w+")


def tag(text: str) -> str:
    return "#" + re.sub(r"\W+", "", text).lower()


def split_caption(text: str, camp: dict, credit: str, first: list[str] | None = None) -> tuple[str, list[str]]:
    """-> (caption without hashtags, hashtags). `first` are tags we know are
    right (the real game, the streamer); they lead, the model's fill the rest."""
    post = camp["post"]
    tags = list(dict.fromkeys((first or []) + post["hashtags"].get("always", []) + TAG.findall(text or "")))
    tags = tags[: post["hashtags"]["count_max"]]
    body = TAG.sub("", text or "").strip()
    body = re.sub(r"\s{2,}", " ", body)
    # Shorten the text, never the credit: the streamer's tag must survive.
    suffix = f" (🎥 {credit})" if credit and post["caption"].get("credit_streamer") else ""
    limit = post["caption"]["max_chars"] - len(suffix)
    if len(body) > limit:
        body = body[: limit - 1].rstrip() + "…"
    body += suffix
    return body, tags


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^\w\s\-']", "", text).strip()
    return text[:limit].rstrip() or "clip"


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", help="VOD or clip link, or a path to a local video file")
    ap.add_argument("--permission", required=True,
                    help="where clipping this source is allowed: the campaign brief URL, "
                         "or the streamer's clipping policy URL")
    ap.add_argument("--campaign", default="general", help="file name in config/campaigns (default: general)")
    ap.add_argument("--credit", default="", help="streamer name or @handle to credit in captions")
    ap.add_argument("--clips", type=int, help="how many clips to ask for (default: the template's maximum)")
    ap.add_argument("--game", default="", help="the game or category, used as the first hashtag")
    ap.add_argument("--streamer", default="", help="streamer name, for the queue and a hashtag")
    ap.add_argument("--score", type=float, default=0, help="moment score from the scan, for queue order")
    ap.add_argument("--moment", help="a candidate from scan.py (JSON file): the stretch around it is "
                                     "watched by the judge, which accepts or rejects it and sets the cut")
    ap.add_argument("--judge-only", action="store_true",
                    help="with --moment: only watch and judge it; the verdict is saved into the moment file")
    ap.add_argument("--format", choices=["auto", "classic", "context"], default="auto",
                    help="classic: a 2 s hook title. context: the judge's stakes line during the build-up "
                         "and its result line after the payoff. auto: rotate, one classic in three")
    ap.add_argument("--start", type=float, help="only use the source from this second")
    ap.add_argument("--end", type=float, help="...to this second (needs --start)")
    args = ap.parse_args()

    env = load_env()
    key = env["GEMINI_API_KEY"]
    camp = load_campaign(args.campaign)
    n = args.clips or camp["clip"]["count_max"]

    is_file = not re.match(r"^https?://", args.source)
    source = args.source
    if is_file:
        p = Path(source).expanduser()
        if not p.is_file():
            die(f"no such file: {p}")
        source = str(p)
        name = p.stem
    else:
        name = (args.credit.lstrip("@") + " " if args.credit else "") + \
            urllib.parse.urlparse(source).netloc.replace("www.", "")

    moment = json.loads(Path(args.moment).read_text()) if args.moment else None
    if moment:
        # Wide enough to hold the setup before chat reacted and the reaction after.
        burst = float(moment["burst_start"])
        ws = max(0.0, burst - 50)
        we = max(float(moment["peak"]) + 25, burst + 30)
        for offset, _title, _views in moment.get("clips") or []:
            ws, we = min(ws, max(0.0, offset - 3)), max(we, offset + 35)
        ws = max(ws, burst - 65)
        we = min(we, ws + 100)
        args.start, args.end = float(int(ws)), float(int(we))

    if not args.judge_only:
        ensure_backend()
        log(f"submitting to OpenShorts ({camp['name']}; asking for {n} clips of "
            f"{camp['clip']['min_seconds']}-{camp['clip']['max_seconds']} s)")
    started = time.time()
    if (args.start is None) != (args.end is None):
        die("--start and --end go together")
    # When scan.py runs this for each moment, it owns the progress bar.
    solo = not os.environ.get("CLIP_PROGRESS_PARENT")
    steps = n + 1          # picking and reframing, then one burn per clip
    if solo and not args.judge_only:
        progress.report("Making clips", 0, steps, "OpenShorts is picking and reframing", started_at=started,
                        step_seconds=150)
    window, verdict = None, None
    if not is_file and args.start is not None:
        log(f"downloading {int(args.end - args.start)} s of the stream (from {int(args.start)} s)")
        window = fetch_window(source, args.start, args.end)
        if moment:
            # A verdict saved by an earlier --judge-only pass is reused as is.
            verdict = moment.get("verdict") or judge_moment(window, moment, args)
            if verdict and not verdict["keep"]:
                window.unlink(missing_ok=True)
                sys.exit(3)                 # tells scan.py the judge said no
            if args.judge_only:
                if not verdict:
                    sys.exit(4)             # the judge could not run
                moment["verdict"] = verdict
                moment["window"] = str(window)      # scan.py compares the kept candidates' footage
                Path(args.moment).write_text(json.dumps(moment, ensure_ascii=False))
                sys.exit(0)                 # the window stays on disk for the clip itself
        job_id = submit(str(window), True, camp, n, key)
    else:
        job_id = submit(source, is_file, camp, n, key, args.start, args.end)
    log(f"job {job_id}")
    result = wait(job_id)
    clips = result.get("clips") or []
    if not clips:
        die("OpenShorts returned no clips for this source")

    stamp = f"_{int(args.start)}s" if args.start is not None else ""
    out_dir = ROOT / "clips" / f"{dt.date.today():%Y-%m-%d}_{safe_name(name, 40)}{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    posts = [f"# {name}\n", f"Source: {args.source}  ", f"Permission: {args.permission}  ",
             f"Template: {camp['name']}\n"]
    handle = camp["post"]["handle"]

    wide = landscape(camp)
    framed: dict[int, str] = {}        # clip index -> the re-rendered video, before any text
    aspect = 16 / 9
    if wide:
        shape = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height", "-of", "csv=p=0", str(window or source)],
                               capture_output=True, text=True).stdout.strip().split(",")
        try:
            aspect = int(shape[0]) / int(shape[1])
        except (ValueError, IndexError, ZeroDivisionError):
            pass
    if verdict:
        # One clip, cut exactly where the judge put the moment, with the judge's text.
        clips = clips[:1]
        c0 = clips[0]
        # A little air after the judged end; both ends are later snapped to word
        # boundaries so the clip never stops in the middle of a word.
        tail = min(args.end - args.start, verdict["end"] + 1.5)
        lines = spoken_lines(job_id)
        parts = fit_to_speech(verdict["start"], tail, lines or [], args.end - args.start,
                              float(camp["clip"]["max_seconds"]))
        def turn_down(said: str, reason: str) -> None:
            """Refuse this cut, remember why, and let scan.py move on to the next candidate."""
            log(f"{said}: turned down")
            with open(ROOT / "data" / "judged.jsonl", "a") as f:
                f.write(json.dumps({
                    "at": dt.datetime.now().isoformat(timespec="seconds"), "source": args.source,
                    "streamer": args.streamer, "start": args.start, "end": args.end, "keep": False,
                    "rejected_because": [reason],
                    "what_happens": verdict.get("what_happens", ""), "hook": verdict.get("hook", ""),
                }, ensure_ascii=False) + "\n")
            if window:
                window.unlink(missing_ok=True)
            sys.exit(3)

        if lines is not None and not any(a <= (s + e) / 2 <= b for s, e, _ in lines for a, b in parts):
            # The judge heard a reaction that the transcript does not contain. A
            # cut in which nobody says a word, with nothing said right after it
            # either, has been a dull clip every time.
            turn_down("nobody speaks in this cut or right after it", "no speech in or right after the cut")
        heard = " ".join(w for t, w in word_times(job_id) if any(a - 0.3 <= t <= b + 0.3 for a, b in parts))
        if SLURS.search(heard):
            # The captions print every spoken word in large type, and the sound is
            # posted as it is. A clip made on 17 Nov had "retarded" burned into it
            # twice and was withdrawn hours before it went out.
            turn_down("a slur is spoken in this cut", "a slur is spoken in the cut")
        if wide or abs(float(c0.get("start", 0)) - verdict["start"]) > 1 or abs(float(c0.get("end", 0)) - verdict["end"]) > 1:
            log(f"re-cutting to the judged moment: {verdict['start']:.1f}-{verdict['end']:.1f} s of the window"
                + (", whole picture over a blurred copy" if wide else ""))
            if len(parts) > 1:
                log(f"the reaction comes after a silence: keeping {parts[0][0]:.1f}-{parts[0][1]:.1f} s "
                    f"and {parts[1][0]:.1f}-{parts[1][1]:.1f} s")
            elif parts[0][1] > tail + 0.2:
                log(f"letting the sentence finish: the cut now ends at {parts[0][1]:.1f} s")
            framed[0], snapped = reframe(job_id, 0, parts, key, wide)
            if wide:
                parts = snapped                # captions are timed against the cut as it really is
            verdict["end"] = verdict["start"] + sum(b - a for a, b in parts)   # so the length is right
        c0.update({"start": verdict["start"], "end": verdict["end"], "viral_hook_text": verdict["hook"],
                   "video_description_for_tiktok": verdict["caption"],
                   "video_description_for_instagram": verdict["caption"],
                   "video_title_for_youtube_short": verdict["youtube_title"],
                   "predicted_score": verdict["overall"] * 10})

    # Which format this clip gets. "context": a line of ours at the top until the
    # payoff, and usually a second one after it. "hook+after": no opening line
    # passed the checks, so the hook opens the clip and our line follows the
    # payoff. "classic": hook only; also one clip in three on purpose, so the
    # formats can be compared by views.
    fmt, ctx_before, ctx_after = "classic", "", ""
    if verdict and args.format != "classic" and (args.format == "context" or len(queue.load()) % 3):
        try:
            ctx_before, ctx_after = context_lines(verdict, parts, job_id, moment, args)
        except Exception as e:
            log(f"the context lines could not be written or checked ({str(e)[:120]}): making this clip the classic way")
        fmt = "context" if ctx_before else "hook+after" if ctx_after else "classic"
    game = args.game if not verdict or verdict.get("game_label_ok", True) else ""
    if game != args.game:
        log(f"the judge says this is not {args.game}: leaving the game out of the tags and title")

    for i, c in enumerate(clips):
        hook = (c.get("viral_hook_text") or "").strip()
        log(f"clip {i + 1}/{len(clips)}: burning " + {"context": "captions and context", "hook+after":
            "hook, captions and a context line"}.get(fmt, "hook and captions"))
        if solo:
            progress.report("Making clips", i + 1, len(clips) + 1, f"captions on clip {i + 1} of {len(clips)}",
                            started_at=started)
        if wide:
            # Everything is drawn by us, in one pass, around the picture.
            mine = parts if verdict else [[float(c.get("start", 0)), float(c.get("end", 0))]]
            if i not in framed:
                framed[i], mine = reframe(job_id, i, mine, key, True)
            video_url = draw_text(
                job_id, framed[i] or c.get("video_url", ""), parts=mine, aspect=aspect, camp=camp,
                hook="" if fmt == "context" else hook, before=ctx_before, after=ctx_after,
                payoff=clip_time(verdict["payoff_at"], mine) if verdict else 0.0)
            if fmt != "classic":
                log(f"       before: {ctx_before or '(the hook)'}  |  after: {ctx_after or '-'}")
        elif fmt != "classic":
            video_url = burn(job_id, i, "" if fmt == "context" else hook, camp, key)
            total = sum(b - a for a, b in parts)
            video_url = add_context(job_id, video_url, ctx_before, ctx_after,
                                    clip_time(verdict["payoff_at"], parts), total)
            log(f"       before: {ctx_before or '(the hook)'}  |  after: {ctx_after or '-'}")
        else:
            video_url = burn(job_id, i, hook, camp, key)
        dest = out_dir / f"{i + 1} - {safe_name(hook or 'clip')}.mp4"
        download(video_url, dest)
        preview(dest, f"{out_dir.name}_{i + 1}")

        known = [tag(x) for x in (game, args.streamer) if x]
        tt, tt_tags = split_caption(c.get("video_description_for_tiktok", ""), camp, args.credit, known)
        ig, ig_tags = split_caption(c.get("video_description_for_instagram", ""), camp, args.credit, known)
        queue.add({
            "video": str(dest), "streamer": args.streamer or args.credit.lstrip("@"), "game": game,
            "format": fmt,
            **({"found_by": ("viewer clip" if not moment.get("chatters") else
                             "chat + viewer clip" if moment.get("viewer_clips") else "chat"),
                "pick": moment.get("pick"), "compare": moment.get("compare"),
                "happened": moment.get("happened")} if moment else {}),
            "hook": hook, "score": args.score or float(c.get("predicted_score") or 0),
            "seconds": round(float(c.get("end", 0)) - float(c.get("start", 0)), 1),
            "captions": {"tiktok": tt, "instagram": ig,
                         "youtube_title": c.get("video_title_for_youtube_short", "") or hook},
            "hashtags": tt_tags, "source": args.source, "permission": args.permission,
            "campaign": args.campaign,
            **({"kind": verdict["moment_type"], "what": verdict["what_happens"],
                "judge": {k: verdict[k] for k in ("overall", "standalone", "opening", "payoff")},
                "names_in_text": verdict["names_in_text"], "question": verdict.get("question", ""),
                "context": [ctx_before, ctx_after] if fmt != "classic" else []}
               if verdict else {}),
        })
        posts += [
            f"## {i + 1}. {hook or 'Clip'}",
            f"File: `{dest.name}`  ",
            f"Source time: {c.get('start', 0):.0f}s to {c.get('end', 0):.0f}s "
            f"({c.get('end', 0) - c.get('start', 0):.0f} s), score {c.get('predicted_score', '?')}\n",
            f"**TikTok**  \n{tt}  \n{' '.join(tt_tags)}\n",
            f"**Instagram**  \n{ig}  \n{' '.join(ig_tags)}\n",
            f"**YouTube Shorts title**  \n{c.get('video_title_for_youtube_short', '')}  \n{' '.join(tt_tags)}\n",
        ]

    if window:
        window.unlink(missing_ok=True)     # the source stretch is not kept once clips exist

    (out_dir / "posts.md").write_text("\n".join(posts) + f"\nPosting as {handle}.\n")

    runs = ROOT / "data" / "runs.jsonl"
    runs.parent.mkdir(exist_ok=True)
    with open(runs, "a") as f:
        f.write(json.dumps({
            "at": dt.datetime.now().isoformat(timespec="seconds"), "source": args.source,
            "permission": args.permission, "campaign": args.campaign, "job_id": job_id,
            "start": args.start, "end": args.end,
            "judge": ({k: verdict[k] for k in ("overall", "standalone", "opening", "payoff", "moment_type",
                                               "what_happens", "start", "end", "cost_usd")} if verdict else None),
            "clips": len(clips), "seconds": round(time.time() - started), "out": str(out_dir),
        }) + "\n")

    if solo:
        progress.report("Making clips", len(clips) + 1, len(clips) + 1, "all done", started_at=started, finished=True)
    log(f"done: {len(clips)} clips in {out_dir} ({(time.time() - started) / 60:.1f} min)")
    log("check every caption and title before posting; the model can get names wrong")


if __name__ == "__main__":
    main()
