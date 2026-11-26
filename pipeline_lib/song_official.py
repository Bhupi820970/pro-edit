"""Only the ORIGINAL artist's / label's audio. Rejects covers, tributes, fan re-uploads and rips
(e.g. SoundCloud "Danilo Lima 64", "... Tribute", "... Experience", lofi / nightcore / remix accounts).

YouTube: accepted from "<Artist> - Topic", <artist>VEVO, the artist's own channel (verified when YouTube
says so), or a known label channel. SoundCloud: the account URL itself must be the artist's or a known
label's (display names are easy to fake: "Imagine Dragons Music" on /smoke-and-mirrors-5). archive.org /
Pixabay copies are never official."""
from __future__ import annotations

import re
import urllib.parse

DENY_RX = re.compile(
    r"(?i)\b(tribute|cover(ed|s)?|karaoke|instrumental|remix(es)?|nightcore|lo-?fi|slowed|reverb|sped\s*up|"
    r"8d|mashup|bootleg|re-?upload|fan\s*(made|edit|page)?|experience|in\s+the\s+style\s+of|made\s+famous|"
    r"originally\s+performed|piano\s+version|acoustic\s+version|y2mate|tiktok|rework|flip|vip\s+mix|"
    r"danilo\s+lima)\b")

LABELS = {
    # majors + big indies / soundtrack labels (normalized: lowercase alnum only)
    "sonymusic", "sonymusicentertainment", "sonymusicindia", "universalmusic", "universalmusicgroup",
    "umg", "warnermusic", "warnerrecords", "warnermusicgroup", "atlanticrecords", "columbiarecords",
    "rcarecords", "interscope", "interscoperecords", "islandrecords", "defjam", "defjamrecordings",
    "capitolrecords", "republicrecords", "epicrecords", "parlophone", "emi", "emimusic", "motown",
    "virginrecords", "polydor", "geffenrecords", "elektrarecords", "fueledbyramen", "roadrunnerrecords",
    "nuclearblast", "recordmakers", "edbangerrecords", "monstercat", "ncs", "nocopyrightsounds",
    "tseries", "zeemusiccompany", "saregama", "saregamamusic", "tipsofficial", "tipsmusic", "yrf",
    "sonymusicsouth", "speedrecords", "desimusicfactory", "erosnow", "erosnowmusic", "adityamusic",
    "leagueoflegends", "riotgames", "rockstargames",
}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _artist_keys(artist: str) -> list[str]:
    a = re.split(r"(?i)\s+(?:ft\.?|feat\.?|featuring|&|and|x|with|,)\s+", artist or "")
    keys = []
    for part in a:
        k = norm(re.sub(r"(?i)^the\s+", "", part))
        if len(k) >= 3:
            keys.append(k)
    full = norm(artist)
    if full and full not in keys:
        keys.append(full)
    return keys


def artist_channel(name: str, artist: str) -> bool:
    n = norm(re.sub(r"(?i)\b(official|music|band|vevo|topic|channel|tv)\b", " ", name or ""))
    if not n:
        return False
    return any(k == n or (len(k) >= 4 and n.startswith(k)) or (len(n) >= 4 and k.startswith(n))
               for k in _artist_keys(artist))


def is_label(name: str) -> bool:
    n = norm(name)
    if any(len(lb) >= 6 and n.startswith(lb) for lb in LABELS):   # "Fueled By Ramen and twenty one pilots"
        return True
    return n in LABELS or norm(re.sub(r"(?i)\b(official|music|records?|channel)\b", " ", name or "")) in LABELS


def soundcloud_account(url: str) -> str:
    try:
        u = urllib.parse.urlparse(url or "")
    except ValueError:
        return ""
    if not (u.hostname or "").endswith("soundcloud.com"):
        return ""
    parts = [p for p in (u.path or "").split("/") if p]
    return parts[0] if parts else ""


def check(rec: dict, artist: str | None = None, title: str | None = None) -> tuple[bool, str]:
    """rec: {source, url, channel, title?, verified?}. Returns (official, reason)."""
    artist = artist or rec.get("artist") or ""
    src = (rec.get("source") or "").lower()
    url = rec.get("url") or ""
    ch = rec.get("channel") or rec.get("uploader") or ""
    blob = " ".join(str(x) for x in (ch, rec.get("track_title") or "", url))
    m = DENY_RX.search(blob.replace("-", " ").replace("_", " "))
    if m and not (title and re.search(re.escape(m.group(0)), title, re.I)):
        return False, f"deny-list: '{m.group(0)}' in uploader/url"
    if src in ("archive", "pixabay") or "archive.org" in url or "pixabay" in url:
        return False, f"{src or 'archive'} copy is not an official upload"
    if "soundcloud.com" in url or src == "soundcloud":
        acct = soundcloud_account(url)
        if not acct or re.fullmatch(r"user-?\d+", acct):
            return False, f"anonymous SoundCloud account '{acct}'"
        if artist_channel(acct.replace("-", " "), artist) or is_label(acct.replace("-", " ")):
            return True, f"official SoundCloud account '{acct}'"
        return False, f"SoundCloud account '{acct}' is not {artist} or their label"
    # YouTube (youtube, yt_mirrors, box find_official)
    if ch.endswith(" - Topic") and artist_channel(ch[:-8], artist):
        return True, "YouTube Topic channel"
    if norm(ch).endswith("vevo") and artist_channel(ch[:-4], artist):
        return True, "VEVO"
    if is_label(ch):
        return True, f"label channel '{ch}'"
    if artist_channel(ch, artist):
        if rec.get("verified") is False:
            return False, f"artist-named channel '{ch}' is not verified"
        return True, f"artist channel '{ch}'"
    if not ch and src in ("", "youtube") and re.fullmatch(r"[\w-]{11}", str(rec.get("id") or "")):
        # legacy box records (find_official picked Topic / VEVO / artist channel but did not store it)
        return True, "legacy official YouTube pick (channel not stored)"
    if not ch and not src and not url:
        return True, "local file with no uploader info"
    return False, f"uploader '{ch}' is not {artist}, their Topic/VEVO or label"
