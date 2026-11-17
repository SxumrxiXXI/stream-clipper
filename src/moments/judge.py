#!/usr/bin/env python3
"""Watch one candidate moment and decide whether it is a clip, and where it starts and ends.

    .venv/bin/python src/moments/judge.py <video file> [--game "..."] [--streamer "..."]

The scanner only knows that chat reacted. This step sends the stretch of video
around the reaction to Gemini together with what chat said and when, and asks
three things a clip page editor would ask:

  1. Is there one clear moment here that a stranger would understand?
  2. Exactly where should the clip start and end to hold that moment?
  3. What is a true, specific hook for it?

A candidate that is only chat talking, a giveaway, a poll, or something that
needs earlier context is rejected, and no clip is made from it.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent.parent
MODEL = "gemini-3.1-flash-lite"     # the model OpenShorts uses; override with JUDGE_MODEL in .env
MIN_SECONDS, MAX_SECONDS = 10, 30
# A clip is made only if the judge finds a moment and rates it at least this.
KEEP_OVERALL, KEEP_STANDALONE = 6, 5


DANGLING = {"a", "an", "the", "of", "on", "in", "to", "with", "and", "or", "for", "at", "by", "from",
            "against", "his", "her", "their", "is", "as", "but", "that", "this"}


class Verdict(BaseModel):
    what_happens: str
    moment_type: str            # funny | clutch | fail | rage | shock | wholesome | hot_take | story | none
    # Closed questions first: they are answered far more reliably than scores.
    event_happened: bool        # a discrete thing occurs (a play, a fail, a joke landing, an outburst)
    chat_was_prompted: bool     # chat is typing because it was asked to (vote, answer, mini-game, giveaway)
    needs_earlier_context: bool
    watching_a_video: bool      # the stream is showing someone else's video or clip, and that is the moment
    reaction_in_clip: bool      # the streamer, casters or crowd audibly or visibly react
    has_clear_moment: bool
    start: str                  # "MM:SS" in the video; the model is steadier with clock times
    end: str
    payoff_at: str
    standalone: int             # 0-10: would a stranger get it with no context?
    opening: int                # 0-10: do the first two seconds make you stay?
    payoff: int                 # 0-10: is there a clear peak or punchline?
    overall: int                # 0-10
    problems: list[str]
    hook: str
    caption: str
    youtube_title: str
    names_in_text: list[str]
    question: str               # one real question about the moment, for platforms built on replies
    game_label_ok: bool         # does the game named above match what is on screen?


PROMPT = """You are the editor of a short-form clips page for live-stream moments. You are given a video
{duration} long (MM:SS) from a stream, and what the live chat wrote during it. Chat reacted
strongly at about {burst} in this video. All times below and in your answer are MM:SS from the
start of THIS video. Chat reacts a few seconds AFTER something happens, so the
moment itself is usually 3 to 12 seconds before chat erupts.

Context you can rely on (do not contradict it):
- Streamer / channel: {streamer}
- Game or category: {game}
- Stream title: {title}
- Chat's reaction looks like: {kind}
{viewer_clips}
Live chat during this stretch, as "MM:SS message":
{chat}

Watch and listen to the whole video, then decide.

0. Four yes/no facts. Answer these before anything else, literally:
   - event_happened: does one discrete event occur in the video: a play, a kill, a fail, a joke
     that lands, an accident, an outburst, a surprise? A person simply talking, explaining,
     reading chat or asking a question is NOT an event.
   - chat_was_prompted: is chat typing because someone ASKED it to: a vote, a poll, a
     prediction, "type 1", a question to chat ("what should I...?"), a chat-controlled game,
     a giveaway, an emote the stream told them to spam? If chat is answering or playing
     along rather than reacting to something that just happened, answer true.
   - needs_earlier_context: would a stranger need to have seen earlier parts of the stream?
   - watching_a_video: is the thing being reacted to a video, clip or post by someone else
     that the streamer is playing in a browser or player (a reaction to other people's
     content)? Gameplay, the streamer's own camera and a tournament broadcast are not this.
   - reaction_in_clip: can you hear or see the streamer, casters or a crowd react?

1. has_clear_moment: true only if ONE specific thing happens that chat is reacting to and that
   a stranger who has never seen this streamer would understand and enjoy from the clip alone.
   Answer false when chat is reacting to a poll, prediction, giveaway, subscription, raid,
   donation message, ad break or technical problem; when it is ordinary conversation with no
   peak; when nothing notable happens on screen or in the audio; or when it only makes sense
   with knowledge of what happened earlier. Be strict. Most stretches are not clips.

2. start and end, as MM:SS in THIS video, for the best single clip between {min_s} and {max_s}
   seconds long. Aim for 11 to 20 seconds when the moment fits in that; go up to {max_s}
   only when the setup or the reaction truly needs it. The clip must contain the thing chat reacted to, which happened shortly
   before {burst}:
   - Start where the setup begins, at the start of a sentence or action, never mid-word.
     Do not start earlier than needed: every second before the moment must earn its place.
   - End 1 to 3 seconds after the payoff, keeping the streamer's immediate reaction, and
     before the energy drops. Never cut off a sentence or the reaction. If the streamer or
     casters say something about what just happened, the clip must include those words.
   - payoff_at is the MM:SS where the peak or punchline lands. It must lie between start and end.

3. Scores from 0 to 10: standalone (understandable with no context), opening (do the first two
   seconds of YOUR chosen clip make someone stop scrolling), payoff (how strong the peak is),
   overall (would you post this).

4. Text for the post. Everything must be true to what is in the clip.
   - hook: at most 9 words, burned on screen for the first 2 seconds. Give the concrete
     setup and make the viewer want the outcome, without stating the outcome. Use only facts
     visible or audible in the clip; no invented numbers. It is shown over the FIRST two
     seconds of your cut, so it must be true of exactly what is on screen at your start
     time, not of something earlier in this video. It must be a complete phrase.
     Good: "1 HP, no ammo, three enemies left", "Asked why he quit the team. His answer:",
     "Last lap, two tenths behind, one corner left", "Watch what the caster does at zero".
     These show the style only. Never reuse their wording.
     Bad, never write these: "You won't believe this", "Insane moment", "Can he do the
     impossible?", "The most ___ ever?", a flat description with no tension such as
     "Streamer talks to his teammate", or anything that reveals the ending.
   - caption: at most 100 characters, casual, no hashtags, no emojis.
   - youtube_title: at most 70 characters, names the game.
   - Name a person only if their name is clearly visible on screen or clearly spoken; otherwise
     say "he", "she", "they" or the streamer's name given above. List every name you used in
     names_in_text.

5. what_happens: one or two plain sentences describing only what is seen and heard.
   problems: anything that weakens the clip (empty list if none).
   moment_type: one of funny, clutch, fail, rage, shock, wholesome, hot_take, story, none.

6. Two more things:
   - question: one genuine question about this moment that viewers would disagree on, at
     most 90 characters ("Right call to push with 12 seconds left?"). Not "thoughts?",
     not "who did it better?", nothing that just asks for likes, comments or follows.
     Empty string if there is no real question.
   - game_label_ok: true if the game or category named above is what is actually on screen;
     false if a different game or activity is being shown.
"""


def mmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def to_seconds(text) -> float:
    """"MM:SS", "M:SS.s", "H:MM:SS" or a bare number of seconds -> seconds."""
    parts = [float(x) for x in str(text).strip().split(":")]
    return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))


def api_key() -> tuple[str, str]:
    key, model = "", MODEL
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("GEMINI_API_KEY="):
            key = line.split("=", 1)[1].strip()
        if line.startswith("JUDGE_MODEL=") and line.split("=", 1)[1].strip():
            model = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("error: GEMINI_API_KEY is empty in .env")
    return key, model


def judge(video: Path, duration: float, *, burst: float, chat: list[tuple[float, str]],
          peak: float | None = None,
          streamer: str = "", game: str = "", title: str = "", kind: str = "",
          viewer_clips: list[tuple[float, str, int]] | None = None) -> dict:
    """-> the verdict as a dict, plus `keep`, `cost_usd` and `model`.

    `chat` and `viewer_clips` use seconds relative to the start of `video`.
    `burst` is where chat's reaction began and `peak` where it was strongest;
    a long reaction can peak half a minute after it began.
    Raises on API errors; the caller decides what to do without a verdict.
    """
    key, model = api_key()
    client = genai.Client(api_key=key)
    with open(video, "rb") as fh:
        up = client.files.upload(file=fh, config={"mime_type": "video/mp4", "display_name": video.name})
    try:
        deadline = time.time() + 180
        while True:
            state = str(getattr(getattr(client.files.get(name=up.name), "state", ""), "name", "")).upper()
            if state == "ACTIVE":
                break
            if state == "FAILED" or time.time() > deadline:
                raise RuntimeError(f"Gemini could not process the video ({state or 'timeout'})")
            time.sleep(2)

        clips_text = ""
        if viewer_clips:
            lines = "\n".join(f'  - starting {mmss(max(0, t))}, titled "{name}" ({views:,} views)'
                              for t, name, views in viewer_clips)
            clips_text = ("- Viewers of the stream made their own clips of this stretch. Their titles are a strong "
                          f"hint of what the moment is:\n{lines}\n")
        chat_text = "\n".join(f"  {mmss(t)} {m}" for t, m in chat if 0 <= t <= duration) or "  (no chat available)"
        prompt = PROMPT.format(duration=mmss(duration), burst=mmss(burst), streamer=streamer or "unknown",
                               game=game or "unknown", title=title or "unknown", kind=kind or "unknown",
                               viewer_clips=clips_text, chat=chat_text, min_s=MIN_SECONDS, max_s=MAX_SECONDS)
        for attempt in range(2):            # the service drops the odd call
            try:
                resp = client.models.generate_content(
                    model=model, contents=[up, prompt],
                    config=types.GenerateContentConfig(response_mime_type="application/json",
                                                       response_schema=Verdict, temperature=0.2))
                v = json.loads(resp.text)
                break
            except Exception:
                if attempt:
                    raise
                time.sleep(8)
    finally:
        try:
            client.files.delete(name=up.name)
        except Exception:
            pass

    # The hook, the caption and the title describe the moment the judge saw, so
    # the clip has to be cut where the judge put it. Its times are used whenever
    # they hang together (inside the video, long enough, payoff inside the cut),
    # also when they sit away from where chat's reaction began: a long reaction
    # peaks later, and on 17 Nov a clip cut at the start of chat's reaction
    # ended before the moment its own hook promised. Times that do not hang
    # together mean the moment cannot be placed, and no clip is made from a guess.
    try:
        start, end, payoff = to_seconds(v["start"]), to_seconds(v["end"]), to_seconds(v["payoff_at"])
        placed = 0 <= start < end <= duration + 1 and end - start >= 5 and start - 1 <= payoff <= end + 1
    except (ValueError, TypeError):
        start = end = payoff = 0.0
        placed = False
    late = max(burst, peak if peak is not None else burst)
    v["cut_from"] = "judge" if start <= late + 12 and end >= burst - 25 else "judge, away from chat's reaction"
    if not placed:
        start, end, payoff = burst - 18, burst + 6, burst - 5      # only so the log shows something
        v["cut_from"] = "nowhere (the judge's times did not add up)"
    start = max(0.0, min(start, duration - MIN_SECONDS))
    end = max(start + MIN_SECONDS, min(end, duration))
    if end - start > MAX_SECONDS:
        # Too long: keep the payoff and its reaction, drop setup from the front.
        payoff = min(max(payoff, start), end)
        end = min(end, payoff + 4)
        start = max(start, end - MAX_SECONDS)
    v["start"], v["end"], v["payoff_at"] = round(start, 2), round(end, 2), round(payoff, 2)
    # A long hook is shortened, but never left hanging on "the" or "with".
    words = str(v["hook"]).split()
    if len(words) > 12:
        words = words[:12]
        while words and words[-1].lower().strip(".,:") in DANGLING:
            words.pop()
    v["hook"] = " ".join(words).strip()
    v["question"] = str(v.get("question") or "").strip()[:110]
    v["game_label_ok"] = bool(v.get("game_label_ok", True))
    # The yes/no facts gate the clip; the scores only rank what passes.
    reasons = []
    if v.get("watching_a_video"):
        # A clip of someone watching a clip is other people's footage twice over,
        # and mostly shows a browser window.
        reasons.append("the streamer is watching someone else's video")
    if not v["has_clear_moment"] or v["moment_type"] == "none":
        reasons.append("no clear moment")
    if not v["event_happened"]:
        reasons.append("nothing discrete happens (talking only)")
    if v["chat_was_prompted"]:
        reasons.append("chat was asked to type; it is not reacting")
    if v["needs_earlier_context"]:
        reasons.append("needs earlier context")
    if v["overall"] < KEEP_OVERALL or v["standalone"] < KEEP_STANDALONE:
        reasons.append("scored too low")
    if not placed:
        reasons.append("the judge could not say where in the video the moment is")
    v["rejected_because"] = reasons
    v["keep"] = not reasons
    # One number to rank accepted clips by: payoff and opening matter most for retention.
    v["rank"] = round(0.35 * v["payoff"] + 0.3 * v["opening"] + 0.2 * v["standalone"] + 0.15 * v["overall"]
                      + (0.5 if v["reaction_in_clip"] else 0), 2)
    usage = getattr(resp, "usage_metadata", None)
    tokens_in = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    v["tokens"] = {"in": tokens_in, "out": tokens_out}
    # Flash-Lite list price at the time of writing: $0.25 in, $1.50 out per million tokens.
    v["cost_usd"] = round(tokens_in * 0.25e-6 + tokens_out * 1.5e-6, 5)
    v["model"] = model
    return v


class Quotes(BaseModel):
    before: list[str]
    after: list[str]


PICK_PROMPT = """You help the editor of a clips page. A short clip was cut from a live stream. Its viewers
see and hear ONLY the clip. The page prints one line at the top of the clip that tells them
something the clip does not: words that were said on the stream just BEFORE the clip
(printed as: Moments before: "...") or just AFTER it (printed as: Right after: "...").

The clip is about this: {what}
Channel: {streamer}. Game or category: {game}.

Transcript of the stretch of stream around the clip, as "MM:SS text". [IN CLIP] lines are
heard by the viewer. [BEFORE] and [AFTER] lines are not in the clip.

{transcript}

Pick the words worth printing:
- before: up to 3 excerpts from [BEFORE] lines that set this event up: what the streamer
  had just claimed, planned, predicted, been warned about or been trying to do. Printed
  while the viewer waits for the outcome, so it must not give the outcome away.
- after: up to 3 excerpts from [AFTER] lines about what just happened: the excuse, the
  verdict, the regret, what it cost.
Rules for every excerpt:
- Copy it exactly from ONE [BEFORE] or [AFTER] line: 3 to 9 words in a row, a complete
  thought that a stranger understands with no other knowledge. Never from an [IN CLIP] line.
- It must be about the event in the clip. Most things said near a moment are about
  something else (another topic, a tip, an aside to chat): leave those out, however odd or
  funny they are.
- No filler: greetings, thanks, "oh my god", "let's go", half sentences, garbled words.
Best first. Empty lists are the right answer when nothing said outside the clip is about
the event; that is common.
"""


def pick_quotes(*, what: str, streamer: str, game: str,
                transcript: list[tuple[str, float, str]]) -> tuple[dict, float]:
    """Words said just outside a finished cut that are worth printing on it.

    `transcript` is [(label, seconds, text)] with label BEFORE, IN CLIP or
    AFTER relative to the clip. Text only. Returns ({"before": [excerpt],
    "after": [excerpt]}, cost in dollars); clip.py still checks every excerpt
    against the transcript and has screen_quotes() look at it again.
    """
    if not any(label in ("BEFORE", "AFTER") for label, _, _ in transcript):
        return {"before": [], "after": []}, 0.0
    key, model = api_key()
    client = genai.Client(api_key=key)
    prompt = PICK_PROMPT.format(
        what=what, streamer=streamer or "unknown", game=game or "unknown",
        transcript="\n".join(f"  [{label}] {mmss(t)} {text}" for label, t, text in transcript))
    resp = client.models.generate_content(
        model=model, contents=[prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Quotes,
                                           temperature=0.3))
    got = json.loads(resp.text)
    out = {}
    for role in ("before", "after"):
        seen = []
        for q in got.get(role) or []:
            q = " ".join(str(q or "").strip().strip('"“”').split())
            if q and q not in seen:
                seen.append(q)
        out[role] = seen[:3]
    usage = getattr(resp, "usage_metadata", None)
    return out, ((getattr(usage, "prompt_token_count", 0) or 0) * 0.25e-6
                 + (getattr(usage, "candidates_token_count", 0) or 0) * 1.5e-6)


class Screened(BaseModel):
    number: int
    about_the_event: bool
    clear_alone: bool
    spoils: bool


class Screen(BaseModel):
    lines: list[Screened]
    best_before: int            # number of the line to print before the payoff; 0 = none
    best_after: int


SCREEN_PROMPT = """You are the editor of a clips page. A short clip from a live stream is about this event:
{what}

Below are things that were said on the stream shortly before or after the clip (they are
not in the clip). One of them may be printed on the clip as context, for example:
Moments before: "..."   or   Right after: "..."
Most things said near a moment have nothing to do with it. A line is worth printing only
if a stranger who reads it understands the event better or enjoys it more.

{lines}

For every line answer three questions:
- about_the_event: true only if the words are plainly about this event: the same attempt,
  fight, call, joke or mistake, what led to it or what came of it. Words about anything
  else (another topic, a tip, an aside to chat, a different part of the game) are false. If
  you have to guess how they connect, answer false.
- clear_alone: true if a stranger understands the words with no other knowledge of the
  stream. A fragment, garbled words or an inside reference: false.
- spoils: true if the words tell the viewer how the event ends.
Then choose. best_before: the number of the one line said BEFORE the clip that would most
make a stranger want to see how the event ends, or 0 if none is worth printing. best_after:
the number of the one line said AFTER it that adds most to what the viewer just saw, or 0.
A weak line is worse than no line.
"""


def screen_quotes(what: str, quotes: list[tuple[str, str]]) -> tuple[list[dict], dict]:
    """A second, separate look at the excerpts: [(before | after, words)].

    Returns ([{about_the_event, clear_alone, spoils} per excerpt, in order],
    {"before": index of the editor's pick or None, "after": ...}). Text only.
    """
    if not quotes:
        return [], {"before": None, "after": None}
    key, model = api_key()
    client = genai.Client(api_key=key)
    listing = "\n".join(f'{n}. said {"BEFORE" if role == "before" else "AFTER"} the clip: "{q}"'
                        for n, (role, q) in enumerate(quotes, 1))
    resp = client.models.generate_content(
        model=model, contents=[SCREEN_PROMPT.format(what=what or "a moment from a live stream", lines=listing)],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Screen,
                                           temperature=0))
    got = json.loads(resp.text)
    by_number = {int(x.get("number") or 0): x for x in got.get("lines") or []}
    out = []
    for n in range(1, len(quotes) + 1):
        x = by_number.get(n) or {}
        # An excerpt the model did not answer for counts as failed.
        out.append({"about_the_event": bool(x.get("about_the_event")), "clear_alone": bool(x.get("clear_alone")),
                    "spoils": bool(x.get("spoils", True)) if not x else bool(x.get("spoils"))})
    best = {}
    for role, k in (("before", "best_before"), ("after", "best_after")):
        try:
            n = int(got.get(k) or 0) - 1
        except (TypeError, ValueError):
            n = -1
        best[role] = n if 0 <= n < len(quotes) and quotes[n][0] == role else None
    return out, best


def _norm(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("’", "'")).split()


def quoted(text: str) -> list[str]:
    """The quotations inside a line: 'Before: "no way he wins"' -> ['no way he wins']."""
    return [q.strip() for q in re.findall(r'["“]([^"“”]{2,})["”]', text) if q.strip()]


def find_quote(quote: str, words: list[tuple[float, str]]) -> tuple[float, float] | None:
    """Where in the transcript these words are spoken, as (start, end) seconds, or
    None if they are not there. `words` are (time, word) in order. The match is
    loose enough to survive a misheard word, strict enough that an invented
    sentence does not pass."""
    q = _norm(quote)
    said = [(t, w) for t, raw in words for w in _norm(raw)]
    if not q or not said:
        return None
    need = 1.0 if len(q) <= 2 else 0.85
    best, where = 0.0, None
    for size in {len(q), len(q) - 1, len(q) + 1} - {0}:
        for i in range(0, max(1, len(said) - size + 1)):
            chunk = said[i:i + size]
            ratio = difflib.SequenceMatcher(None, q, [w for _, w in chunk]).ratio()
            if ratio > best:
                best, where = ratio, (chunk[0][0], chunk[-1][0])
    return where if best >= need else None


class Place(BaseModel):
    label: str
    why: str


class Order(BaseModel):
    ranking: list[str]            # every label once, best first
    reasons: list[Place]


COMPARE_PROMPT = """You choose what a short-form clips page posts today. Below are {n} videos from live streams,
labelled V1 to V{n} in the order they are given. Each is about 90 seconds around one moment; the page
would cut the best 10 to 30 seconds from it.

Compare them with each other and rank all {n} from best to worst for strangers scrolling a feed:
which would make someone stop, understand it with no context, get a clear payoff, and send it to a friend.
You must give a strict order with no ties, naming every label exactly once. Judge what is actually in
the footage and the sound, and keep each video's label straight. Give a one-line reason for every video."""


def compare(entries: list[dict], runs: int = 3) -> tuple[dict, float]:
    """Order candidates by comparing their footage with each other.

    `entries` are {"id", "video": Path, "streamer", "game"}. The model ranks them
    all in one call, `runs` times with the videos in a different order each time
    (one call's order depends on where a video sits in the list), and a
    candidate's place is the average. Returns ({id: {"mean": place, "ranks":
    [place per valid run]}}, cost in dollars). Raises if no run gave a usable
    order; the caller then keeps the order it had.
    """
    import random
    import subprocess

    key, model = api_key()
    client = genai.Client(api_key=key)
    small, up = {}, {}
    try:
        for e in entries:
            # Ranking needs the gist, not the pixels: 480p at 10 fps keeps a whole
            # day's candidates inside one cheap call.
            dst = e["video"].with_name("cmp_" + e["video"].name)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(e["video"]), "-vf", "scale=-2:480,fps=10",
                            "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-c:a", "aac", "-b:a", "48k",
                            "-ac", "1", str(dst)], check=True, capture_output=True)
            small[e["id"]] = dst
            with open(dst, "rb") as fh:
                up[e["id"]] = client.files.upload(file=fh, config={"mime_type": "video/mp4", "display_name": dst.name})
        deadline = time.time() + 300
        for f in up.values():
            while str(getattr(getattr(client.files.get(name=f.name), "state", ""), "name", "")).upper() != "ACTIVE":
                if time.time() > deadline:
                    raise RuntimeError("Gemini did not finish processing the videos")
                time.sleep(2)
        ids = [e["id"] for e in entries]
        info = {e["id"]: e for e in entries}
        places: dict[str, list[int]] = {i: [] for i in ids}
        cost = 0.0
        for k in range(runs):
            order = ids[:]
            random.Random(1000 + k).shuffle(order)
            labels, parts = {}, []
            for n, i in enumerate(order, 1):
                labels[f"V{n}"] = i
                parts += [f"V{n}: channel {info[i].get('streamer') or 'unknown'}, "
                          f"game or category {info[i].get('game') or 'unknown'}", up[i]]
            resp = None
            for attempt in range(3):            # the service drops the odd call
                try:
                    resp = client.models.generate_content(
                        model=model, contents=parts + [COMPARE_PROMPT.format(n=len(order))],
                        config=types.GenerateContentConfig(response_mime_type="application/json",
                                                           response_schema=Order))
                    break
                except Exception:
                    time.sleep(10)
            if resp is None:
                continue
            usage = getattr(resp, "usage_metadata", None)
            cost += ((getattr(usage, "prompt_token_count", 0) or 0) * 0.25e-6
                     + (getattr(usage, "candidates_token_count", 0) or 0) * 1.5e-6)
            try:
                got = [labels.get(str(x).strip().upper()) for x in json.loads(resp.text).get("ranking") or []]
            except ValueError:
                continue
            if sorted(x for x in got if x) != sorted(ids) or len(got) != len(ids):
                continue                         # a label missing or named twice: this run does not count
            for place, i in enumerate(got, 1):
                places[i].append(place)
        if not places[ids[0]]:
            raise RuntimeError("no comparison run gave a complete order")
        return {i: {"mean": round(sum(p) / len(p), 2), "ranks": p} for i, p in places.items()}, cost
    finally:
        for f in up.values():
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass
        for dst in small.values():
            dst.unlink(missing_ok=True)


def main() -> None:
    import subprocess
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--game", default="")
    ap.add_argument("--streamer", default="")
    ap.add_argument("--burst", type=float, help="seconds into the video where chat reacted (default: 2/3 in)")
    a = ap.parse_args()
    path = Path(a.video).expanduser()
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(path)], capture_output=True, text=True).stdout.strip())
    v = judge(path, dur, burst=a.burst if a.burst is not None else dur * 2 / 3, chat=[],
              streamer=a.streamer, game=a.game)
    print(json.dumps(v, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
