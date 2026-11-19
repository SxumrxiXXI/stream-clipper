#!/usr/bin/env python3
"""Merge two versions of the posting queue, clip by clip.

The daily run, the post check and the live watcher each keep their own copy of
data/queue.json while they work and save it when they finish. Saved as plain
text, two of them changing the file at once would collide. This is a git merge
driver (see deploy/save_state.sh) that merges the list by clip id instead:

    merge_queue.py <base> <ours> <theirs>      # result is written over <ours>

For a clip both sides changed, each field is taken from the side that changed
it. Where both changed the same field, the side whose clip is further along
wins (posted > skipped > failed > scheduled > ready), and "theirs" on a tie.
Exit 0 always means a clean merge; anything unreadable exits 1 so git reports
a conflict instead of guessing.
"""
import json
import sys

RANK = {"posted": 5, "skipped": 4, "failed": 3, "scheduled": 2, "ready": 1}


def merge(base: list[dict], ours: list[dict], theirs: list[dict]) -> list[dict]:
    b = {i["id"]: i for i in base}
    t = {i["id"]: i for i in theirs}
    out, used = [], set()
    for o in ours:
        cid = o["id"]
        used.add(cid)
        if cid not in t:
            out.append(o)
            continue
        th, ba = t[cid], b.get(cid)
        if ba is None and o.get("video") != th.get("video"):
            # Two different clips were given the same number. Keep ours here;
            # theirs is added below under the next free number.
            out.append(o)
            used.discard(cid)
            t[cid] = {**th, "_renumber": True}
            continue
        if o == th or th == ba:
            out.append(o)
        elif o == ba:
            out.append(th)
        else:
            first = th if RANK.get(th.get("status"), 0) >= RANK.get(o.get("status"), 0) else o
            both = {}
            for k in list(o) + [k for k in th if k not in o]:
                ov, tv, bv = o.get(k), th.get(k), (ba or {}).get(k)
                both[k] = ov if ov == tv or tv == bv else tv if ov == bv else first.get(k)
            out.append({k: v for k, v in both.items() if k in first or v is not None})
    taken = {i["id"] for i in out}
    for th in theirs:
        item = t[th["id"]]
        if th["id"] in used:
            continue
        if item.pop("_renumber", False) or th["id"] in taken:
            prefix = str(th["id"])[:1]
            n = max((int(x[1:]) for x in taken if x[:1] == prefix and x[1:].isdigit()), default=0) + 1
            item = {**item, "id": f"{prefix}{n:04d}"}
        taken.add(item["id"])
        out.append(item)
    return sorted(out, key=lambda i: str(i.get("created", "")))      # stable: equal times keep their order


def main() -> None:
    try:
        base, ours, theirs = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:4])
        merged = merge(base, ours, theirs)
    except Exception as e:
        print(f"merge_queue: {e}", file=sys.stderr)
        sys.exit(1)
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
