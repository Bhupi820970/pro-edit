"""Reject source shots with burned-in credits/title cards or a persistent corner logo/bug."""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

HEAD_TAIL_S = 8.0

# Studio / credit / league-bug wording that must never survive into an edit.
CREDIT_RE = re.compile(
    r"(?is)\b("
    r"universal(\s+pictures)?|paramount|lionsgate|netflix|pixar|columbia|tristar|"
    r"warner\s*bros\.?|sony\s*pictures|20th\s*century|metro[- ]goldwyn|\bmgm\b|"
    r"new\s*line|dreamworks|lucasfilm|marvel\s*studios|\ba24\b|syncopy|"
    r"\bhbo\b|prime\s*video|apple\s*tv\+?|\bhulu\b|\bmubi\b|\bmax\b|peacock|"
    r"presents|presented\s+by|a\s+film\s+by|directed\s+by|produced\s+by|"
    r"written\s+(and\s+directed\s+)?by|executive\s+producer|in\s+association|"
    r"coming\s+soon|only\s+in\s+theaters|in\s+theaters|now\s+streaming|"
    r"official\s+trailer|teaser\s+trailer|all\s+rights\s+reserved|"
    r"copyright|premieres?|coming\s+to|"
    r"premier\s*league|\bespn\b|sky\s*sports|tnt\s*sports"
    r")\b"
)


def head_tail_margin(clip_len: float) -> float:
    """~8s on typical trailers; shrink on very short clips so a usable body remains."""
    if clip_len <= 0:
        return 0.0
    return float(min(HEAD_TAIL_S, max(2.5, clip_len * 0.12)))


def head_tail_bad(s: float, e: float, clip_len: float) -> str:
    m = head_tail_margin(clip_len)
    if s < m or e > clip_len - m:
        return "clip head/tail (ratings card, logos, end slate)"
    return ""


def ocr_available() -> bool:
    return bool(shutil.which("tesseract"))


def ocr_text(frame_rgb: np.ndarray) -> str:
    """OCR one RGB frame. Empty string if tesseract is missing or fails."""
    if not ocr_available() or frame_rgb is None or frame_rgb.size == 0:
        return ""
    import cv2
    from PIL import Image
    h, w = frame_rgb.shape[:2]
    img = frame_rgb
    if w < 640:
        scale = 640 / float(w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    try:
        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "f.png"
            Image.fromarray(gray).save(png)
            out = Path(td) / "o"
            subprocess.run(
                ["tesseract", str(png), str(out), "--psm", "6", "-l", "eng"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            tp = out.with_suffix(".txt")
            txt = tp.read_text(errors="ignore") if tp.exists() else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return re.sub(r"\s+", " ", txt).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z0-9'&.-]{1,}", text or "")


def _persistent_text_band(frames: list[np.ndarray], loose: bool) -> bool:
    """Stable horizontal glyph band (title card / lower-third), not a full-frame texture."""
    import cv2
    if len(frames) < 2:
        return False
    masks = []
    for fr in frames:
        g = cv2.cvtColor(fr, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(g, 80, 180)
        ker = cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, fr.shape[1] // 20), 2))
        band = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, ker)
        masks.append((band > 0).astype(np.uint8))
    acc = np.stack(masks, 0).sum(0)
    persist = acc >= min(2, len(frames))
    h, w = persist.shape
    persist[: int(h * 0.06)] = 0
    persist[int(h * 0.94):] = 0
    if float(persist.mean()) > 0.25:
        return False  # texture / lots of edges, not a credit line
    row = persist.mean(axis=1)
    if row.max() < (0.10 if loose else 0.18):
        return False
    peak = int(row.argmax())
    n_hot = int((row > 0.5 * row.max()).sum())
    if n_hot > h * 0.28:
        return False
    sl = persist[max(0, peak - 3): peak + 4].max(axis=0)
    coverage = float(sl.mean())
    need = 0.18 if loose else 0.28
    return coverage >= need and row.max() >= (0.12 if loose else 0.20)


def _title_card_phrases(text: str) -> set[str]:
    """ALL-CAPS 2+ word phrases typical of studio/title cards, not random OCR crumbs."""
    caps = re.findall(r"\b[A-Z]{3,}(?:\s+[A-Z]{2,})+\b", text or "")
    return {re.sub(r"\s+", " ", p).strip() for p in caps if len(p) >= 8}


def has_burned_text(frames: list[np.ndarray]) -> bool:
    """True when frames show credits, studio names, or a persistent title-card overlay."""
    if not frames:
        return False
    if ocr_available():
        texts = [ocr_text(f) for f in frames]
        if any(CREDIT_RE.search(t) for t in texts):
            return True
        # persistent title-card lettering (e.g. "JUMANJI OPEN WORLD"), not in-world signs/OCR junk
        phrase_sets = [_title_card_phrases(t) for t in texts]
        from collections import Counter
        c: Counter[str] = Counter()
        for s in phrase_sets:
            c.update(s)
        if any(n >= 2 for n in c.values()):
            return True
        # OCR ran and found no credits: do not also fire the weaker visual-band heuristic
        return False
    return _persistent_text_band(frames, loose=True)


def _corner_ocr_bug(frames: list[np.ndarray]) -> bool:
    """OCR the four corners; a studio/channel word in 2+ frames is a persistent bug."""
    if not ocr_available() or len(frames) < 2:
        return False
    hits = 0
    for fr in frames:
        h, w = fr.shape[:2]
        cw, ch = max(24, int(w * 0.22)), max(20, int(h * 0.20))
        crops = (fr[:ch, :cw], fr[:ch, w - cw:], fr[h - ch:, :cw], fr[h - ch:, w - cw:])
        if any(CREDIT_RE.search(ocr_text(c)) for c in crops):
            hits += 1
    return hits >= 2


def has_persistent_corner_logo(frames: list[np.ndarray]) -> bool:
    """True when a structured blob sits still in a corner while the picture moves."""
    if len(frames) < 2:
        return False
    if _corner_ocr_bug(frames):
        return True
    import cv2
    h, w = frames[0].shape[:2]
    cw, ch = max(16, int(w * 0.16)), max(14, int(h * 0.16))
    corners = [
        (slice(0, ch), slice(0, cw)),
        (slice(0, ch), slice(w - cw, w)),
        (slice(h - ch, h), slice(0, cw)),
        (slice(h - ch, h), slice(w - cw, w)),
    ]
    mid = (slice(int(h * 0.2), int(h * 0.8)), slice(int(w * 0.2), int(w * 0.8)))
    greys = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY).astype(np.float32) for f in frames]
    center_mot = float(np.mean([
        np.mean(np.abs(greys[i][mid] - greys[i + 1][mid])) for i in range(len(greys) - 1)
    ]))
    for rs, cs in corners:
        patches = [g[rs, cs] for g in greys]
        struct = []
        for p in patches:
            lap = cv2.Laplacian(p, cv2.CV_32F)
            struct.append(float(np.mean(np.abs(lap))))
        if float(np.median(struct)) < 4.0:
            continue
        if float(np.median([float(np.std(p)) for p in patches])) < 8.0:
            continue
        inter = float(np.mean([
            np.mean(np.abs(patches[i] - patches[i + 1])) for i in range(len(patches) - 1)
        ]))
        # only a bug if the picture is moving and that corner is comparatively still.
        # a locked-off shot (static scenery in the corner) must not be flagged.
        if center_mot >= 8.0 and inter < 6.5 and center_mot > inter * 2.0:
            return True
    return False


def shot_overlay_bad(frames: list[np.ndarray]) -> str:
    if has_persistent_corner_logo(frames):
        return "persistent corner logo"
    if has_burned_text(frames):
        return "burned-in text/credits"
    return ""


def sample_indices(start: int, end: int, n: int = 3) -> list[int]:
    """Pick ~n evenly spaced frame indexes in [start, end)."""
    if end - start <= 0:
        return []
    if end - start <= n:
        return list(range(start, end))
    fracs = (0.2, 0.5, 0.8)[:n]
    out, seen = [], set()
    for f in fracs:
        i = start + int((end - start - 1) * f)
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

