#!/usr/bin/env python3
"""Free disk space: delete what the pipeline no longer needs.

    .venv/bin/python src/cleanup.py            # delete
    .venv/bin/python src/cleanup.py --dry-run  # only list what would go

What goes:
1. Clip folders in clips/ once every clip in them is either
   - posted, live on at least one platform (a post link came back) and past
     `keep_posted_hours` (config/posting.yaml, default 6), or
   - skipped.
   A clip that is still waiting for its slot is never touched, even though
   Upload-Post already holds a copy: if a post fails we can still resend it.
   Folders that no queued clip points at (hand-made tests) are left alone.
2. OpenShorts' working files (its copy of each downloaded stretch and its
   intermediate renders) older than a day. This is most of the space.
3. Stream stretches left in data/windows older than a day, and chat logs and
   scan candidates older than a week (a scan never looks further back).

The queue, the run log, verdicts, view counts and the daily logs are kept:
they are tiny and the reports need them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "publish"))
import post  # noqa: E402

CLIPS = ROOT / "clips"
DAY = 86400


def size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def done_with(item: dict, now: dt.datetime, keep_hours: float) -> bool:
    """True when this clip's file will never be needed again."""
    if item["status"] == "skipped":
        return True
    if item["status"] != "posted" or not item.get("urls") or not item.get("scheduled_for"):
        return False
    return now - dt.datetime.fromisoformat(item["scheduled_for"]) >= dt.timedelta(hours=keep_hours)


def old(p: Path, days: float) -> bool:
    return time.time() - p.stat().st_mtime > days * DAY


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config" / "posting.yaml").read_text())
    keep_hours = float(cfg.get("keep_posted_hours", 6))
    items = post.load()
    now = dt.datetime.now().astimezone()
    targets: list[tuple[Path, str]] = []

    # 1. clip folders, judged by every queued clip that lives in them
    by_folder: dict[Path, list[dict]] = {}
    for i in items:
        v = Path(i["video"])
        if CLIPS in v.parents:
            by_folder.setdefault(v.parent, []).append(i)
    for folder, clips in by_folder.items():
        if folder.exists() and folder != CLIPS and all(done_with(c, now, keep_hours) for c in clips):
            targets.append((folder, "posted or skipped: " + ", ".join(c["id"] for c in clips)))

    # 2. OpenShorts' working copies
    vendor = ROOT / "vendor" / "openshorts"
    for sub in ("output", "uploads"):
        d = vendor / sub
        if d.is_dir():
            targets += [(p, "OpenShorts working files, over a day old") for p in d.iterdir()
                        if p.name != "thumbnails" and not p.name.startswith(".") and old(p, 1)]

    # 3. our own leftovers
    for sub, days, why in (("windows", 1, "downloaded stretch, over a day old"),
                           ("chat", 7, "chat log, over a week old"),
                           ("previews", 14, "preview strip, over two weeks old"),
                           ("scan/candidates", 7, "scan candidate, over a week old")):
        d = ROOT / "data" / sub
        if d.is_dir():
            targets += [(p, why) for p in d.iterdir() if not p.name.startswith(".") and old(p, days)]

    freed = 0
    for p, why in targets:
        n = size(p)
        freed += n
        print(f"{'would delete' if a.dry_run else 'deleted'}  {n / 1e6:7.1f} MB  {p.relative_to(ROOT)}   ({why})")
        if not a.dry_run:
            try:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            except PermissionError:
                # On Linux the OpenShorts container writes its files as root;
                # it removes them itself after 24 hours.
                freed -= n
                print(f"   (left: owned by the OpenShorts container, which clears it itself)")
    waiting = sum(1 for cl in by_folder.values() for c in cl if c["status"] in ("ready", "scheduled"))
    print(f"{'would free' if a.dry_run else 'freed'} {freed / 1e6:.0f} MB; "
          f"{waiting} clips kept because they have not been posted yet")
    if not a.dry_run and freed:
        gone = {str(p) for p, _ in targets}
        for i in items:
            if str(Path(i["video"]).parent) in gone:
                i["file_deleted"] = now.isoformat(timespec="seconds")
        post.save(items)


if __name__ == "__main__":
    main()
