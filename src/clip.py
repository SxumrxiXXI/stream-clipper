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
    cmd = ["yt-dlp", "--no-warnings", "-q", "--download-sections", f"*{int(start)}-{int(end)}",
           "--force-keyframes-at-cuts", "-f", "best[height<=1080]/best", "--merge-output-format", "mp4",
           "-o", str(out), url]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out.exists():
        die(f"could not download that stretch of the stream: {res.stderr.strip()[-300:]}")
    return out


def judge_moment(window: Path, moment: dict, args) -> dict | None:
    """Have the judge watch the window. None means it could not run (then the
    clip is made the old way, with OpenShorts choosing the cut)."""
    import judge
    ws = args.start
    dur = args.end - args.start
    try:
        v = judge.judge(
            window, dur, burst=float(moment["burst_start"]) - ws,
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
    events = [f"Dialogue: 0,{ass_time(0)},{ass_time(payoff)},Before,,0,0,0,,{{\\fad(120,120)}}{esc(before)}"]
    if after:
        events.append(f"Dialogue: 0,{ass_time(payoff)},{ass_time(total + 5)},After,,0,0,0,,{{\\fad(150,0)}}{esc(after)}")
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
        if lines is not None and not any(a <= (s + e) / 2 <= b for s, e, _ in lines for a, b in parts):
            # The judge heard a reaction that the transcript does not contain. A
            # cut in which nobody says a word, with nothing said right after it
            # either, has been a dull clip every time, so it is turned down here
            # and remembered, and scan.py moves on to the next candidate.
            log("nobody speaks in this cut or right after it: turned down")
            with open(ROOT / "data" / "judged.jsonl", "a") as f:
                f.write(json.dumps({
                    "at": dt.datetime.now().isoformat(timespec="seconds"), "source": args.source,
                    "streamer": args.streamer, "start": args.start, "end": args.end, "keep": False,
                    "rejected_because": ["no speech in or right after the cut"],
                    "what_happens": verdict.get("what_happens", ""), "hook": verdict.get("hook", ""),
                }, ensure_ascii=False) + "\n")
            if window:
                window.unlink(missing_ok=True)
            sys.exit(3)
        if abs(float(c0.get("start", 0)) - verdict["start"]) > 1 or abs(float(c0.get("end", 0)) - verdict["end"]) > 1:
            log(f"re-cutting to the judged moment: {verdict['start']:.1f}-{verdict['end']:.1f} s of the window")
            if len(parts) > 1:
                log(f"the reaction comes after a silence: keeping {parts[0][0]:.1f}-{parts[0][1]:.1f} s "
                    f"and {parts[1][0]:.1f}-{parts[1][1]:.1f} s")
            elif parts[0][1] > tail + 0.2:
                log(f"letting the sentence finish: the cut now ends at {parts[0][1]:.1f} s")
            api_json("POST", "/api/clip/rerender", {
                "job_id": job_id, "clip_index": 0, "snap_to_words": True, "reapply_captions": False,
                "segments": [{"start": a, "end": b} for a, b in parts]}, key)
            verdict["end"] = verdict["start"] + sum(b - a for a, b in parts)   # so the length is right
        c0.update({"start": verdict["start"], "end": verdict["end"], "viral_hook_text": verdict["hook"],
                   "video_description_for_tiktok": verdict["caption"],
                   "video_description_for_instagram": verdict["caption"],
                   "video_title_for_youtube_short": verdict["youtube_title"],
                   "predicted_score": verdict["overall"] * 10})

    # Which format this clip gets. The context format needs a stakes line from the
    # judge; one clip in three stays classic so the two can be compared by views.
    fmt = "classic"
    if verdict and verdict.get("context_before") and args.format != "classic":
        fmt = "context" if args.format == "context" or len(queue.load()) % 3 else "classic"
    if fmt == "context":
        # Nothing is printed on a clip unless a second look at the footage, with the
        # exact transcript, backs it. A line that fails is dropped; if the opening
        # line fails, the clip is made the classic way.
        import judge
        try:
            lines_said = [(float(x["start"]), (x.get("text") or "").strip()) for x in transcript_of(job_id)]
            checks = judge.verify(window, [verdict["context_before"], verdict.get("context_after", "")],
                                  start=parts[0][0], end=parts[-1][1], transcript=lines_said,
                                  streamer=args.streamer, game=args.game)
        except Exception as e:
            log(f"the context lines could not be checked ({str(e)[:120]}): making this clip the classic way")
            checks, fmt = [], "classic"
        ok = {c["statement"]: c["status"] == "supported" for c in checks}
        for c in checks:
            log(f"       fact check: {c['status']:<13} \"{c['statement']}\"  ({c['evidence'][:110]})")
        if fmt == "context" and not ok.get(verdict["context_before"]):
            fmt = "classic"
        elif fmt == "context" and verdict.get("context_after") and not ok.get(verdict["context_after"]):
            verdict["context_after"] = ""
        verdict["fact_checks"] = checks
    game = args.game if not verdict or verdict.get("game_label_ok", True) else ""
    if game != args.game:
        log(f"the judge says this is not {args.game}: leaving the game out of the tags and title")

    for i, c in enumerate(clips):
        hook = (c.get("viral_hook_text") or "").strip()
        log(f"clip {i + 1}/{len(clips)}: burning {'captions and context' if fmt == 'context' else 'hook and captions'}")
        if solo:
            progress.report("Making clips", i + 1, len(clips) + 1, f"captions on clip {i + 1} of {len(clips)}",
                            started_at=started)
        if fmt == "context":
            video_url = burn(job_id, i, "", camp, key)
            total = sum(b - a for a, b in parts)
            video_url = add_context(job_id, video_url, verdict["context_before"], verdict.get("context_after", ""),
                                    clip_time(verdict["payoff_at"], parts), total)
            log(f"       before: {verdict['context_before']}  |  after: {verdict.get('context_after') or '-'}")
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
            "hook": hook, "score": args.score or float(c.get("predicted_score") or 0),
            "seconds": round(float(c.get("end", 0)) - float(c.get("start", 0)), 1),
            "captions": {"tiktok": tt, "instagram": ig,
                         "youtube_title": c.get("video_title_for_youtube_short", "") or hook},
            "hashtags": tt_tags, "source": args.source, "permission": args.permission,
            "campaign": args.campaign,
            **({"kind": verdict["moment_type"], "what": verdict["what_happens"],
                "judge": {k: verdict[k] for k in ("overall", "standalone", "opening", "payoff")},
                "names_in_text": verdict["names_in_text"], "question": verdict.get("question", ""),
                "context": [verdict.get("context_before", ""), verdict.get("context_after", "")]
                           if fmt == "context" else []}
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
