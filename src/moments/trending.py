"""Twitch's official API (Helix): a streamer's recent VODs and the clips viewers
made from them. Viewer clips say where the audience already thought "clip that".

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env (a free app registered at
https://dev.twitch.tv/console/apps). The app token reads public data only.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


class Twitch:
    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise SystemExit("error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are missing from .env")
        self.client_id = client_id
        data = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret,
                                       "grant_type": "client_credentials"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as r:
                self.token = json.loads(r.read())["access_token"]
        except urllib.error.HTTPError as e:
            raise SystemExit(f"error: Twitch rejected the app credentials (HTTP {e.code})")

    def get(self, path: str, **params) -> list[dict]:
        url = f"{HELIX}/{path}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers={"Client-Id": self.client_id,
                                                   "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("data", [])
        except urllib.error.HTTPError as e:
            raise SystemExit(f"error: Twitch API {path} -> HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}")

    def user_id(self, login: str) -> str:
        users = self.get("users", login=login.lower())
        if not users:
            raise SystemExit(f"error: no Twitch channel called {login}")
        return users[0]["id"]

    def logins(self, ids: list[str]) -> dict[str, str]:
        """broadcaster id -> login (the name in their twitch.tv address)."""
        out = {}
        for i in range(0, len(ids), 100):
            for u in self.get("users", id=ids[i:i + 100]):
                out[u["id"]] = u["login"]
        return out

    def top_games(self, n: int) -> list[dict]:
        """Categories with the most viewers right now."""
        return self.get("games/top", first=min(n, 100))

    def game(self, name: str) -> dict | None:
        found = self.get("games", name=name)
        return found[0] if found else None

    def game_clips(self, game_id: str, days: int) -> list[dict]:
        """The most-viewed clips made in a category in the last `days` days."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.get("clips", game_id=game_id, started_at=since, first=100)

    def live_streams(self, game_id: str | None = None, language: str | None = None) -> list[dict]:
        """Live streams, most viewers first; for one category or all of Twitch."""
        params = {"first": 100}
        if game_id:
            params["game_id"] = game_id
        if language:
            params["language"] = language
        return self.get("streams", **params)

    def recent_vods(self, user_id: str, days: int, limit: int = 5) -> list[dict]:
        """Past broadcasts from the last `days` days, newest first."""
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        vods = []
        for v in self.get("videos", user_id=user_id, type="archive", first=20):
            made = dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
            if made >= since:
                vods.append({"id": v["id"], "url": v["url"], "title": v["title"],
                             "created_at": v["created_at"], "seconds": float(_seconds(v["duration"])),
                             "views": v.get("view_count", 0)})
        return vods[:limit]

    def viewer_clips(self, user_id: str, days: int) -> list[dict]:
        """Clips viewers cut in the last `days` days, most viewed first (Helix order)."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clips = []
        for c in self.get("clips", broadcaster_id=user_id, started_at=since, first=100):
            if c.get("video_id") and c.get("vod_offset") is not None:
                clips.append({"vod_id": c["video_id"], "offset": float(c["vod_offset"]),
                              "seconds": float(c.get("duration") or 30), "views": c["view_count"],
                              "title": c["title"], "url": c["url"]})
        return clips


def _seconds(text: str) -> int:
    """Helix durations look like 3h21m33s."""
    parts = dict((u, int(n)) for n, u in re.findall(r"(\d+)([hms])", text))
    return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0)
