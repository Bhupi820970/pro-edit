"""Group clips so one video is about one movie, one series, or one star player."""
from __future__ import annotations

import re
from collections import defaultdict

SINGLE_WORK_CATEGORIES = frozenset({"movie", "series", "romance", "gaming", "marvel", "dc", "anime", "celebrity", "comedy"})

# Longer aliases first so "cristiano ronaldo" wins over a bare "ronaldo".
STAR_PLAYERS: list[tuple[str, tuple[str, ...]]] = [
    ("lebron james", ("lebron james", "king james", "lebron")),
    ("lionel messi", ("lionel messi", "leo messi", "messi")),
    ("cristiano ronaldo", ("cristiano ronaldo", "cristiano", "cr7", "ronaldo")),
    ("stephen curry", ("stephen curry", "steph curry", "curry")),
    ("kevin durant", ("kevin durant", "durant")),
    ("kobe bryant", ("kobe bryant", "kobe")),
    ("michael jordan", ("michael jordan", "jordan")),
    ("erling haaland", ("erling haaland", "haaland")),
    ("kylian mbappe", ("kylian mbappe", "mbappe", "mbappé")),
    ("neymar", ("neymar jr", "neymar")),
    ("patrick mahomes", ("patrick mahomes", "mahomes")),
    ("tom brady", ("tom brady", "brady")),
    ("virat kohli", ("virat kohli", "kohli")),
    ("roger federer", ("roger federer", "federer")),
    ("rafael nadal", ("rafael nadal", "nadal")),
    ("novak djokovic", ("novak djokovic", "djokovic")),
]

_ALIAS_RE = [
    (canon, re.compile(r"\b" + r"\b|\b".join(re.escape(a) for a in aliases) + r"\b", re.I))
    for canon, aliases in STAR_PLAYERS
]

GTA_RE = re.compile(r"\b(gta|grand theft auto)\b", re.I)

TRAILER_TITLE_RE = re.compile(
    r"(?i)\b(trailers?|teasers?|promos?|tv\s*spots?)\b"
)


# Gaming: official publisher trailers and gameplay reveals ARE allowed (BiBi, round 5); teasers, TV spots,
# promos and fan "trailer reactions" still are not.
GAME_TRAILER_OK_RE = re.compile(r"(?i)\b(official\s+)?(gameplay\s+)?(trailer|reveal)\b")
GAME_TRAILER_BAD_RE = re.compile(r"(?i)\b(teasers?|promos?|tv\s*spots?|reaction|breakdown|explained|fan[- ]made|concept)\b")


# Uploads with no real scene/story to cut: bloopers, BTS, interviews, watch parties, recaps...
# (sports keeps interviews / press conferences — those are its content)
JUNK_TITLE_RE = re.compile(
    r"(?i)\b(bloopers?|gag\s*reel|outtakes?|behind[\s-]+the[\s-]+scenes?|bts|interviews?|watch\s*party|"
    r"reactions?|recap|explained|diar(?:y|ies)|cast\s+(?:plays|answers|reacts|talks)|featurettes?|"
    r"making\s+of|first\s+look|sneak\s+peek|previews?|table\s+read|red\s+carpet|podcast|q\s*&\s*a)\b"
)


def is_junk_title(title: str, category: str | None = None) -> bool:
    return category != "sports" and bool(JUNK_TITLE_RE.search(title or ""))


def trailer_blocked(title: str, category: str | None = None) -> bool:
    """is_trailer_title, except gaming keeps official trailers / gameplay reveals. Also blocks
    bloopers/BTS/interviews/previews (no story to edit) outside sports."""
    if is_junk_title(title, category):
        return True
    if category == "gaming":
        t = title or ""
        return bool(GAME_TRAILER_BAD_RE.search(t)) or (is_trailer_title(t) and not GAME_TRAILER_OK_RE.search(t))
    return is_trailer_title(title)


def is_trailer_title(title: str) -> bool:
    """True when the upload is a trailer / teaser / promo / TV spot — never use these."""
    return bool(TRAILER_TITLE_RE.search(title or ""))


# Fan / official "full scene" uploads. Broad on purpose: Clip Kings / Clip Empire titles
# often use "(Full Scene)" or a trailing "Scene", not an official-channel name.
FULL_SCENE_TITLE_RE = re.compile(
    r"(?i)\b("
    r"full\s+scenes?|movie\s+scenes?|tv\s+scenes?|series\s+scenes?|"
    r"official\s+clips?|official\s+scenes?|movie\s+clips?|"
    r"full\s+episodes?|scene\s+clips?|press\s+conferences?|"
    r"full\s+highlights?|official\s+gameplay|scenes?"
    r")\b"
)


def is_full_scene_upload(title: str) -> bool:
    """True for a full-scene / official-clip upload that is not a trailer."""
    t = title or ""
    return bool(FULL_SCENE_TITLE_RE.search(t)) and not is_trailer_title(t)


# Trailer / marketing junk stripped from a source title to get the work name.
_TRAILER_RE = re.compile(
    r"(?ix)"
    r"\b(official|final|new|exclusive|ultimate|red\s*band|4k|hdr|uhd|english)\b|"
    r"\b(teaser|trailer|clip|featurette|excerpt|official\s+scene|gameplay\s+trailer)\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)\s+anniversary\b|"
    r"\bnow\s+streaming\b|\bin\s+theaters\b|\bcoming\s+soon\b|"
    r"\ba\s+[^.|]+?\s+film\b|"
    r"\bwith\s+[A-Z][A-Za-z.]+(?:\s+(?:&|and)\s+[A-Z][A-Za-z.]+)*\b|"
    r"\bstarring\b.*$|"
    r"\(\s*\d{4}\s*\)|"
    r"\brestoration\b|\bvolume\s+\d+\b|\bvol\.?\s*\d+\b|"
    r"\bseason\s+\d+\b|\bs\d{1,2}\b|\bep(?:isode)?\s*\d+\b"
)

_CHANNEL_BITS = re.compile(
    r"(?ix)^(netflix|prime\s*video(?:\s+au(?:\s*&\s*nz)?)?|hbo\s*max|hbo|max|"
    r"mubi|lionsgate(?:\s+movies)?|warner\s*bros\.?|sony\s*pictures(?:\s+entertainment)?|"
    r"universal\s*pictures|paramount(?:\s*\+)?|disney(?:\s*\+|\s+plus)?|apple\s*tv\+?|"
    r"hulu|bbc|fx|amc|showtime|peacock|rockstar\s*games.*|playstation|xbox|"
    r"movieclips(?:\s+.*)?|clipzone.*|super\s+scenes|dc|marvel(?:\s+entertainment)?|"
    r"rotten\s+tomatoes.*|fandango.*|film\s*clip|"
    r"4k|hdr|uhd)$"
)


def work_title(raw: str, category: str | None = None) -> str:
    """Normalize a YouTube title to a movie / series / game work name."""
    if not raw:
        return ""
    s = raw.replace("\u2013", "-").replace("\u2014", "-").replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    parts = [p.strip(" -–—|") for p in re.split(r"\s*\|\s*", s) if p.strip(" -–—|")]
    kept = []
    for p in parts:
        if _CHANNEL_BITS.match(p.strip()):
            continue
        if re.fullmatch(r"(?i)official\s+(trailer|teaser|clip|scene|gameplay.*)", p.strip()):
            continue
        if re.match(r"(?i)^official\b", p.strip()):
            continue
        if re.fullmatch(r"(?i)now\s+streaming|in\s+theaters.*|coming\s+soon", p.strip()):
            continue
        if re.match(r"(?i)^with\s+[A-Z]", p.strip()):
            continue
        kept.append(p)
    core = kept[0] if kept else (parts[0] if parts else s)
    # "GOAT (2026) 4K - Diss Track" / "GOAT - Film Clip" / "The Batman - Ending Monologue":
    # the work name is the short part before the first " - " when the rest is scene text.
    dash = [d.strip() for d in re.split(r"\s+-\s+", core) if d.strip()]
    dash_hit = len(dash) > 1 and len(dash[0].split()) <= 5 and (
        re.search(r"(?i)\b(scene|clip|track|rap|trailer|teaser|monologue|ending|opening|fight|chase|in\s+theaters|\d+/\d+)",
                  " ".join(dash[1:]))
        or re.search(r"(?i)\(\s*\d{4}\s*\)|\b4k\b", dash[0]))
    if dash_hit:
        kept = [dash[0]]
        core = dash[0]
    # Fan channels often use "scene description | Movie Name". Prefer the short title part.
    def _clean_part(p: str) -> str:
        x = re.sub(r"\s*\([^)]*\)", " ", p)
        x = re.sub(r"(?i)\b(full\s+)?(scene|clip)s?\b", " ", x)
        return re.sub(r"\s+", " ", x).strip(" -–—|")
    shorts = []
    for p in kept:
        cp = _clean_part(p)
        if cp and 1 <= len(cp.split()) <= 6:
            shorts.append(cp)
    first = kept[0] if kept else ""
    if shorts and (
        re.search(r'(?i)(full\s+scene|\bscene\b|\bclip\b)|^["“]', first)
        or len(first) > 48
        or (shorts[-1] != _clean_part(first) and len(shorts[-1].split()) <= 4 and len(first.split()) >= 4)
    ):
        core = shorts[-1]
    core = re.sub(r"(?i)\b4k\b", " ", core)
    # "The Love Hypothesis - Official Trailer" / '"Wuthering Heights"'
    core = core.strip().strip('"').strip("'").strip()
    core = re.sub(r"\s*[-:]\s*(official|final|new|exclusive).*$", "", core, flags=re.I)
    core = _TRAILER_RE.sub(" ", core)
    core = re.sub(r"\s*[-:,/]+\s*$", "", core)
    core = re.sub(r"\s+", " ", core).strip(" -–—|")
    cat = (category or "").lower()
    if cat in {"series", "romance"} or cat == "series":
        core = re.sub(r"(?i)\s+(season|series)\s+\d+\s*$", "", core).strip()
        # "Stranger Things 5" -> "Stranger Things" (season number, not a 3-digit show name)
        m = re.search(r"^(.*[A-Za-z].*)\s+(\d{1,2})$", core)
        if m and m.group(1).strip().lower() not in {"the", "a", "an"}:
            core = m.group(1).strip()
    return re.sub(r"\s+", " ", core).strip()


def player_name(title: str, channel: str = "") -> str | None:
    """Return a canonical star-player name if the title/channel names one, else None."""
    blob = f"{title} {channel}"
    for canon, rx in _ALIAS_RE:
        if rx.search(blob):
            return canon
    return None


def is_gta_title(title: str) -> bool:
    return bool(GTA_RE.search(title or ""))


def _clip_title(c: dict) -> str:
    return c.get("title") or ""


def _clip_channel(c: dict) -> str:
    return c.get("channel") or c.get("credit") or ""


def restrict_to_single_work(clips: list[dict], category: str, seed: int = 0) -> list[dict]:
    """Keep only clips of one movie/series. No-op for other categories."""
    if category not in SINGLE_WORK_CATEGORIES or not clips:
        return list(clips)
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in clips:
        key = c.get("work") or work_title(_clip_title(c), category) or c.get("id") or c.get("src_id") or "unknown"
        groups[key.lower()].append(c)
    return _pick_largest_group(groups, seed)


def _clip_duration(c: dict) -> float:
    try:
        return float(c.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def _pick_largest_group(groups: dict[str, list[dict]], seed: int) -> list[dict]:
    def weight(cs: list[dict]) -> tuple:
        return (len(cs), sum(_clip_duration(c) for c in cs))
    best_w = max(weight(cs) for cs in groups.values())
    tied = sorted(k for k, cs in groups.items() if weight(cs) == best_w)
    key = tied[seed % len(tied)]
    return list(groups[key])


def restrict_to_single_player(clips: list[dict], seed: int = 0) -> list[dict]:
    """Keep only clips that name the same star player. Drops clips with no player."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in clips:
        p = player_name(_clip_title(c), _clip_channel(c))
        if p:
            groups[p].append(c)
    if not groups:
        return []
    # Prefer a cluster that includes at least one likely-spoken source if tagged.
    def pref(item: tuple[str, list[dict]]) -> tuple:
        k, cs = item
        spoken = sum(1 for c in cs if c.get("has_dialogue"))
        return (-spoken, -len(cs), k)
    ordered = sorted(groups.items(), key=pref)
    # If seed is used, still keep spoken-first; only rotate among equally preferred.
    best = pref(ordered[0])
    tied = [k for k, cs in ordered if pref((k, cs))[:2] == best[:2]]
    key = tied[seed % len(tied)]
    return list(groups[key])

