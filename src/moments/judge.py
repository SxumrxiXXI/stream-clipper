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
import json
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
    # What the page adds to the footage (see "6." in the prompt). Empty when
    # nothing true can be said beyond what the picture already shows.
    context_before: str
    context_after: str
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

6. Context the page adds. This is what makes the post more than a copy of the footage, so
   it must tell the viewer something they could NOT get from simply watching the clip.
   Never describe what is visibly happening ("he shoots three players"): that adds nothing.
   Good context is a fact from the scoreboard, the casters, the streamer's own words, the
   stream title or the earlier part of this video: the score or the stakes, what the player
   had left, what was on the line, what a rule, item or ability means, who the person is,
   or what the result changed.
   - context_before: at most 9 words. The situation or stakes going INTO the moment, shown
     on screen from the start of your cut until payoff_at. It replaces the hook, so it must
     also make the viewer want the outcome and must not give the outcome away.
     Examples of the kind of line (never reuse their wording): "Down 0-2, one life left,
     no healing", "His first ranked game after a two-year break".
   - context_after: at most 9 words. What the moment led to or why it mattered, shown from
     payoff_at to the end: the new score, what it won or cost, a record, the consequence.
     Examples of the kind (never reuse their wording): "That kill forced overtime",
     "It cost him the whole run: 3 hours gone".
   - Every word must be true and checkable in this video or the context given above. No
     invented numbers, scores, names or records. If you cannot state such a fact, return
     an empty string for that line; an empty line is far better than a guess.
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
          streamer: str = "", game: str = "", title: str = "", kind: str = "",
          viewer_clips: list[tuple[float, str, int]] | None = None) -> dict:
    """-> the verdict as a dict, plus `keep`, `cost_usd` and `model`.

    `chat` and `viewer_clips` use seconds relative to the start of `video`.
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
        resp = client.models.generate_content(
            model=model, contents=[up, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=Verdict, temperature=0.2))
        v = json.loads(resp.text)
    finally:
        try:
            client.files.delete(name=up.name)
        except Exception:
            pass

    # The model's timestamps are trusted only when they hang together: inside
    # the video, payoff inside the cut, and the cut on the stretch chat reacted
    # to. Otherwise the cut is anchored on the chat reaction instead.
    try:
        start, end, payoff = to_seconds(v["start"]), to_seconds(v["end"]), to_seconds(v["payoff_at"])
        sane = (0 <= start < end <= duration + 1 and start - 1 <= payoff <= end + 1
                and start <= burst + 12 and end >= burst - 25)
    except (ValueError, TypeError):
        start = end = payoff = 0.0
        sane = False
    v["cut_from"] = "judge"
    if not sane:
        # Chat erupts a few seconds after the moment: keep the lead-up and the reaction.
        start, end, payoff = burst - 18, burst + 6, burst - 5
        v["cut_from"] = "chat reaction (the judge's times did not add up)"
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
    for k, limit in (("context_before", 11), ("context_after", 11)):
        w = str(v.get(k) or "").strip().strip('"').split()
        v[k] = " ".join(w) if len(w) <= limit else ""      # an over-long line is dropped, not chopped
    v["question"] = str(v.get("question") or "").strip()[:110]
    v["game_label_ok"] = bool(v.get("game_label_ok", True))
    # The yes/no facts gate the clip; the scores only rank what passes.
    reasons = []
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


class Check(BaseModel):
    statement: str
    status: str                 # supported | contradicted | unverifiable
    evidence: str               # what was seen or heard, with its MM:SS
    only_describes: bool        # it just narrates the action the viewer is watching


class Checks(BaseModel):
    checks: list[Check]


CHECK_PROMPT = """You are a fact-checker for a clips page. An editor wants to print the statements
below ON SCREEN over the part of this video from {start} to {end} (MM:SS). A wrong statement
would be published under the page's name, so be strict.

What you may rely on: the video itself (scoreboard, kill feed, name tags, on-screen text),
what people in it say, and this transcript of the speech, "MM:SS text":
{transcript}

Known for certain: channel {streamer}; game or category {game}.

For each statement decide:
- supported: you can point to the exact evidence (what is shown or said, and when). Names of
  people and teams, scores and numbers must match the evidence exactly, and the statement
  must be about the player, team or person the clip actually shows.
- contradicted: the video or transcript shows otherwise.
- unverifiable: it may be true, but you cannot point to evidence for every part of it.
Guessing "supported" is the one mistake that matters. When in doubt, answer unverifiable.

Also answer only_describes for each statement: true if it merely narrates the action the
viewer watches happen anyway ("he tries to catch a fish", "he shoots three players"); false
if it adds something a casual viewer would not take from watching: the score or stakes,
what was on the line, background on the person, what a rule or item means, or what the
moment led to.

Statements:
{statements}
"""


def verify(video: Path, statements: list[str], *, start: float, end: float,
           transcript: list[tuple[float, str]], streamer: str = "", game: str = "") -> list[dict]:
    """A second, narrower look: are these on-screen statements backed by the video?

    Returns one {statement, status, evidence} per statement, in order. Raises on
    API errors; the caller then prints nothing it could not check.
    """
    statements = [x for x in statements if x]
    if not statements:
        return []
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
        prompt = CHECK_PROMPT.format(
            start=mmss(start), end=mmss(end), streamer=streamer or "unknown", game=game or "unknown",
            transcript="\n".join(f"  {mmss(t)} {text}" for t, text in transcript) or "  (no speech)",
            statements="\n".join(f"{n}. {x}" for n, x in enumerate(statements, 1)))
        resp = client.models.generate_content(
            model=model, contents=[up, prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Checks,
                                               temperature=0))
        got = json.loads(resp.text).get("checks") or []
    finally:
        try:
            client.files.delete(name=up.name)
        except Exception:
            pass
    out = []
    for n, x in enumerate(statements):
        c = got[n] if n < len(got) else {}
        status = str(c.get("status", "")).lower().strip()
        out.append({"statement": x, "status": status if status in ("supported", "contradicted") else "unverifiable",
                    "evidence": str(c.get("evidence", ""))[:200], "only_describes": bool(c.get("only_describes"))})
    return out


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
