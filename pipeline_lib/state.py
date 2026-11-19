"""Persistent pipeline state: pattern / song / dialogue-window history, vet cache, ready queue."""
from __future__ import annotations

import datetime as dt
import random

from .util import REPO, home, load_json, save_json, today, now_local
from .edit_ranges import merge_ranges, source_id, seg_source_end

import caption_patterns as CP


class State:
    def __init__(self):
        self.path = home() / "state.json"
        self.d = load_json(self.path, {})
        for k, v in {"patterns": [], "songs": [], "windows": [], "sources_used": [], "segments_used": [],
                     "vet": {}, "videos": [], "listing_cache": {}, "song_fail": {}}.items():
            self.d.setdefault(k, v)
        self._seed_historical_segments()

    def save(self):
        save_json(self.path, self.d)

    # -------- caption patterns: never reuse one inside the cool-down window (2-3 days)
    def pick_pattern(self, platform: str, cooldown_days: float = 2.5) -> str:
        cutoff = (now_local() - dt.timedelta(days=cooldown_days)).isoformat()
        recent = [p for p in self.d["patterns"] if p["at"] >= cutoff]
        used = {p["id"] for p in recent}
        free = [pid for pid in CP.ROTATION if pid not in used]
        if not free:  # more videos than patterns inside the window: take the least recently used
            last = {}
            for p in self.d["patterns"]:
                last[p["id"]] = p["at"]
            free = sorted(CP.ROTATION, key=lambda pid: last.get(pid, ""))[:3]
        return random.choice(free[:4]) if len(free) > 4 else free[0]

    def use_pattern(self, pid: str, video_id: str, platform: str):
        self.d["patterns"].append({"id": pid, "at": now_local().isoformat(), "video": video_id, "platform": platform})
        self.d["patterns"] = self.d["patterns"][-400:]

    # -------- songs
    def recent_songs(self, days: int = 10) -> list[str]:
        cutoff = (now_local() - dt.timedelta(days=days)).isoformat()
        return [f'{s["title"]} - {s["artist"]}' for s in self.d["songs"] if s["at"] >= cutoff]

    def use_song(self, song: dict, video_id: str):
        self.d["songs"].append({"title": song["title"], "artist": song["artist"], "url": song.get("url"),
                                "at": now_local().isoformat(), "video": video_id})
        self.d["songs"] = self.d["songs"][-500:]

    # -------- dialogue windows (so the same lines don't repeat in back-to-back videos)
    def _seed_historical_segments(self):
        """Load ranges already used in earlier hand/code/sample GTA edits so new videos don't repeat them."""
        seed = load_json(REPO / "config" / "used_segments_seed.json", {}) or {}
        rows = seed.get("segments") or []
        have = {(s.get("video"), round(float(s.get("start") or 0), 2), s.get("id")) for s in self.d["segments_used"]}
        added = 0
        for s in rows:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            key = (s.get("video"), round(float(s.get("start") or 0), 2), s.get("id"))
            if key in have:
                continue
            self.d["segments_used"].append({
                "id": s["id"], "file": s.get("file") or s["id"], "start": float(s["start"]),
                "end": float(s["end"]), "at": s.get("at") or now_local().isoformat(),
                "video": s.get("video") or "seed", "kind": s.get("kind") or "montage",
            })
            have.add(key)
            added += 1
        if added:
            self.d["segments_used"] = self.d["segments_used"][-2500:]

    def avoid_windows(self, days: int = 3) -> list:
        """[(source_id, start, end), ...] used in recent videos, merged into scene-level blocks."""
        cutoff = (now_local() - dt.timedelta(days=days)).isoformat()
        by = {}
        for s in self.d.get("segments_used") or []:
            if s.get("at", "") < cutoff:
                continue
            key = s.get("id") or source_id(s.get("file") or "")
            by.setdefault(key, []).append((s["start"], s["end"]))
        # also honour the older dialogue-only window log
        for w in self.d.get("windows") or []:
            if w.get("at", "") < cutoff:
                continue
            key = source_id(w.get("file") or "")
            st = float(w.get("start") or 0)
            by.setdefault(key, []).append((st, st + 8.0))
        out = []
        for key, rs in by.items():
            for a, b in merge_ranges(rs):
                out.append((key, a, b))
        return out

    def use_windows(self, segs: list, video_id: str, period: float | None = None):
        from pathlib import Path
        at = now_local().isoformat()
        for s in segs:
            src = s.get("src") or ""
            if not src:
                continue
            start = float(s.get("start") if s.get("start") is not None else 0)
            end = seg_source_end(s, period)
            rec = {"id": source_id(src), "file": Path(src).name, "start": start, "end": end,
                   "at": at, "video": video_id, "kind": s.get("kind") or "montage"}
            self.d["segments_used"].append(rec)
            if s.get("kind") == "dialogue":
                self.d["windows"].append({"file": Path(src).name, "start": start,
                                          "at": at, "video": video_id})
        self.d["windows"] = self.d["windows"][-600:]
        self.d["segments_used"] = self.d["segments_used"][-2500:]

    def source_uses(self, sid: str, days: int = 3) -> int:
        cutoff = (now_local() - dt.timedelta(days=days)).isoformat()
        return sum(1 for u in self.d["sources_used"] if u["id"] == sid and u["at"] >= cutoff)

    def use_sources(self, ids: list[str], video_id: str):
        for i in ids:
            self.d["sources_used"].append({"id": i, "at": now_local().isoformat(), "video": video_id})
        self.d["sources_used"] = self.d["sources_used"][-1500:]

    # -------- videos
    def add_video(self, v: dict):
        self.d["videos"].append(v)

    def videos(self, platform=None, state=None):
        return [v for v in self.d["videos"] if (platform is None or v["platform"] == platform)
                and (state is None or v["state"] == state)]

    def produced_today(self, platform: str) -> list:
        return [v for v in self.d["videos"] if v["platform"] == platform and v["created"][:10] == today()]

