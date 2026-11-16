"""One small JSON file that says what the pipeline is doing right now.

The progress bar above the chat prompt (the clip-progress mod) reads it every
few seconds. Anything long-running calls report() as it moves along.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "progress.json"


def report(task: str, done: int, total: int, label: str = "", *, started_at: float | None = None,
           finished: bool = False, step_seconds: float | None = None) -> float:
    """Write the current state. Returns started_at so callers can pass it back in.

    step_seconds is a rough guess of how long one step takes, used for the
    time-left estimate until the first step has actually finished.
    """
    started_at = started_at or time.time()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "task": task, "done": done, "total": total, "label": label,
        "started_at": started_at, "updated_at": time.time(),
        "step_seconds": step_seconds, "finished": finished,
    }))
    os.replace(tmp, PATH)      # readers never see a half-written file
    return started_at
