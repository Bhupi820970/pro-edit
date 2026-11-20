"""20 sleek, compact beside-speaker caption patterns (P01–P20).

Captions stay COMPACT (max ~12–15% frame width), never fat/blocky, never covering faces,
never bottom-edge subtitles. Always float BESIDE the speaker (upper-left, mid-right,
stepped diagonal, split-side) with soft outer glow + clean drop shadow.

Refined cinema fonts (Cinzel, Cormorant Garamond, Montserrat Light/Medium,
Playfair Display Italic, Roboto Condensed) with multi-color keyword highlights.
Tracks used pattern IDs in history.json so no style repeats for at least 3 days (~21 videos).
"""
from __future__ import annotations

import json
from pathlib import Path

# Refined multi-color palettes (highlight + base)
CHAMPAGNE_GOLD = (212, 175, 55)
IVORY = (255, 245, 230)
ICY_CYAN = (170, 230, 255)
SOFT_ROSE = (255, 182, 193)
SILVER = (192, 192, 192)
LAVENDER = (230, 230, 250)
PEARL = (240, 234, 214)
AMBER = (255, 191, 0)
CREAM = (255, 253, 208)
MINT = (189, 252, 201)
CRIMSON = (220, 20, 60)
SUNSET_PEACH = (255, 204, 153)
WHITE = (255, 255, 255)

_P = [
    # id, name, y-rhythm, gap, nudge, words, (key, key_long, filler), palette, no_face_y, font_hint
    ("P01", "Champagne beside-mid", [0.0], 0.85, 0.0, 3, (.072, .062, .048), [CHAMPAGNE_GOLD, IVORY, WHITE], .28, "Cinzel"),
    ("P02", "Icy upper-left", [-0.55], 0.9, 0.0, 3, (.070, .060, .046), [ICY_CYAN, WHITE, SILVER], .22, "Montserrat Light"),
    ("P03", "Soft rose mid-right", [0.0], 0.88, 0.02, 3, (.068, .058, .044), [SOFT_ROSE, SILVER, WHITE], .30, "Cormorant Garamond"),
    ("P04", "Lavender stepped diagonal", [-0.5, 0.0, 0.5], 0.82, 0.0, 3, (.070, .060, .046), [LAVENDER, PEARL, WHITE], .26, "Playfair Display Italic"),
    ("P05", "Amber cream upper", [-0.45], 0.92, 0.0, 2, (.074, .064, .048), [AMBER, CREAM, IVORY], .20, "Cinzel"),
    ("P06", "Mint white close", [0.0], 0.75, 0.03, 3, (.066, .056, .042), [MINT, WHITE, SILVER], .32, "Montserrat Medium"),
    ("P07", "Crimson glow far", [-0.15], 1.05, 0.0, 3, (.070, .060, .046), [CRIMSON, WHITE, IVORY], .28, "Cormorant Garamond"),
    ("P08", "Sunset peach punch", [0.0], 0.88, 0.0, 2, (.076, .066, .048), [SUNSET_PEACH, IVORY, WHITE], .30, "Playfair Display Italic"),
    ("P09", "Pearl split-side", [-0.35, 0.35], 0.85, 0.0, 3, (.068, .058, .044), [PEARL, LAVENDER, WHITE], .26, "Cinzel"),
    ("P10", "Gold ivory compact", [0.0], 0.8, 0.0, 3, (.064, .054, .040), [CHAMPAGNE_GOLD, IVORY, CREAM], .34, "Montserrat Light"),
    ("P11", "Cyan silver step", [0.4, 0.0, -0.4], 0.9, 0.0, 3, (.070, .060, .046), [ICY_CYAN, SILVER, WHITE], .28, "Cormorant Garamond"),
    ("P12", "Rose gold upper-right", [-0.5], 0.95, 0.0, 2, (.072, .062, .048), [SOFT_ROSE, CHAMPAGNE_GOLD, WHITE], .22, "Playfair Display Italic"),
    ("P13", "Lavender wave", [-0.4, 0.0, 0.4, 0.0], 0.85, 0.0, 3, (.068, .058, .044), [LAVENDER, PEARL, SILVER], .30, "Cinzel"),
    ("P14", "Amber mint mix", [0.0], 0.82, 0.02, 3, (.066, .056, .042), [AMBER, MINT, IVORY], .32, "Montserrat Medium"),
    ("P15", "Crimson cream lower", [0.45], 1.0, 0.0, 2, (.070, .060, .046), [CRIMSON, CREAM, WHITE], .40, "Cormorant Garamond"),
    ("P16", "Peach silver diagonal", [-0.3, 0.2], 0.88, 0.0, 3, (.068, .058, .044), [SUNSET_PEACH, SILVER, IVORY], .26, "Playfair Display Italic"),
    ("P17", "Ivory cyan compact", [0.0], 0.78, 0.0, 3, (.064, .054, .040), [IVORY, ICY_CYAN, WHITE], .34, "Cinzel"),
    ("P18", "Gold rose upper", [-0.48], 0.9, 0.0, 2, (.072, .062, .048), [CHAMPAGNE_GOLD, SOFT_ROSE, PEARL], .22, "Montserrat Light"),
    ("P19", "Mint lavender step-down", [-0.55, 0.0, 0.55], 0.85, 0.0, 3, (.070, .060, .046), [MINT, LAVENDER, WHITE], .28, "Cormorant Garamond"),
    ("P20", "Silver pearl far", [-0.1], 1.1, 0.0, 3, (.068, .058, .044), [SILVER, PEARL, IVORY], .30, "Playfair Display Italic"),
]

PATTERNS = [{"id": i, "name": n, "y": y, "gap": g, "nudge": nu, "words": w,
             "key": k[0], "key_long": k[1], "filler": k[2], "keys": pal,
             "no_face_y": nf, "font_hint": fh, "classic": i == "P01"}
            for i, n, y, g, nu, w, k, pal, nf, fh in _P]

ROTATION = [p["id"] for p in PATTERNS]
BY_ID = {p["id"]: p for p in PATTERNS}
BY_ID["P00"] = BY_ID["original_gta"] = BY_ID["P01"]

HISTORY_PATH = Path(__file__).parent / "history.json"


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except Exception:
            pass
    return {"used_patterns": [], "uploaded": []}


def save_history(h: dict) -> None:
    HISTORY_PATH.write_text(json.dumps(h, indent=2))


def pick_pattern(avoid_days: int = 3) -> dict:
    """Pick a pattern not used in the last ~21 videos (3 days at 7/day)."""
    h = load_history()
    used = set(h.get("used_patterns", [])[-21:])
    candidates = [p for p in PATTERNS if p["id"] not in used]
    if not candidates:
        candidates = PATTERNS
    chosen = candidates[0]  # sequential for reproducibility; randomize if needed
    h.setdefault("used_patterns", []).append(chosen["id"])
    save_history(h)
    return chosen


def get(pid: str | None) -> dict:
    return BY_ID.get(pid or "P01", PATTERNS[0])
