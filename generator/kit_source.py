#!/usr/bin/env python3
r"""kit_source.py - real-club artwork for the generated kits.

Two things the renderer needs that are NOT generated from the club's colours:

* the **club badge** - in PES 2021 the crest is baked into the kit texture, so a
  generated kit used to have no badge at all.  Football Life ships the crests
  separately, one PNG per team id, in

      <sider>\content\anthem-jacket\cache\emblem\<team_id>.png

  which is exactly what we composite onto the chest.
* the **kit supplier** mark - baked into the original kit texture at the
  KIT_SUPPLIER anchor.  There is no loose asset for it, so we lift it out of the
  club's own loose kit texture when one exists (most clubs only have a CPK kit,
  in which case the supplier mark is simply skipped).

Nothing here is generated or invented: every pixel comes from the game's files.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

import ftex_io
import kit_regions

DEFAULT_SIDER = Path(r"C:\Program Files (x86)\SP Football Life 2027\SiderAddons")

# caches keyed by (str(sider), team_id)
_EMBLEM_CACHE: dict[tuple[str, int], Image.Image | None] = {}
_ATLAS_CACHE: dict[tuple[str, int], np.ndarray | None] = {}
_MAP_CACHE: dict[str, dict[int, str]] = {}


# --------------------------------------------------------------------------- #
#  club badge (emblem cache)
# --------------------------------------------------------------------------- #
def emblem_path(sider, team_id) -> Path:
    return Path(sider) / "content" / "anthem-jacket" / "cache" / "emblem" / f"{int(team_id)}.png"


def load_emblem(sider, team_id) -> Image.Image | None:
    """The club's crest as an RGBA image trimmed to its visible pixels, or None."""
    key = (str(sider), int(team_id))
    if key in _EMBLEM_CACHE:
        return _EMBLEM_CACHE[key]
    img = None
    p = emblem_path(sider, team_id)
    if p.is_file():
        try:
            raw = Image.open(p).convert("RGBA")
            img = _trim_alpha(raw)
        except Exception:                                 # noqa: BLE001
            img = None
    _EMBLEM_CACHE[key] = img
    return img


def _trim_alpha(img: Image.Image, threshold: int = 8) -> Image.Image:
    """Crop away fully-transparent margins so the crest fills the badge box."""
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] != 4:
        return img
    ys, xs = np.where(a[:, :, 3] > threshold)
    if len(xs) == 0:
        return img
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    if box == (0, 0, img.width, img.height):
        return img
    return img.crop(box)


# --------------------------------------------------------------------------- #
#  the club's own loose kit texture (for the supplier mark)
# --------------------------------------------------------------------------- #
def read_map(sider) -> dict[int, str]:
    key = str(Path(sider))
    if key in _MAP_CACHE:
        return _MAP_CACHE[key]
    import re
    out: dict[int, str] = {}
    p = Path(sider) / "content" / "kits" / "map.txt"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0]
            if "," not in line:
                continue
            tid, rest = line.split(",", 1)
            tid = tid.strip()
            m = re.search(r'"([^"]+)"', rest)
            folder = m.group(1) if m else rest.strip().split(",")[0].strip()
            if tid.isdigit() and folder:
                out.setdefault(int(tid), folder)
    _MAP_CACHE[key] = out
    return out


def source_atlas(sider, team_id, slot: int = 1) -> np.ndarray | None:
    """Decode the club's own loose kit texture to an (H, W, 3) array, or None.

    Only clubs whose kit lives as a loose ``.ftex`` under content/kits can be
    read; everyone else (kits inside a CPK) returns None and the caller falls
    back gracefully.
    """
    key = (str(sider), int(team_id))
    if key in _ATLAS_CACHE:
        return _ATLAS_CACHE[key]
    atlas = None
    folder = read_map(sider).get(int(team_id))
    if folder:
        d = Path(sider) / "content" / "kits" / folder / f"p{slot}"
        cand = None
        if d.is_dir():
            exact = d / f"u{int(team_id):04d}p{slot}.ftex"
            cand = exact if exact.is_file() else next(iter(sorted(d.glob("u*p*.ftex"))), None)
        if cand:
            try:
                dds = ftex_io.ftex_to_dds(Path(cand).read_bytes())
                atlas = np.asarray(Image.open(io.BytesIO(dds)).convert("RGB"))
            except Exception:                             # noqa: BLE001
                atlas = None
    _ATLAS_CACHE[key] = atlas
    return atlas


def extract_mark(atlas: np.ndarray, anchor, margin: float = 1.35,
                 bg_tol: float = 14.0, soft: float = 30.0) -> Image.Image | None:
    """Lift a printed mark (supplier logo) out of a kit atlas at `anchor`.

    `anchor` is a kit_regions anchor ``(cx, cy, area)``.  The mark's background is
    the modal (shirt) colour in that box; pixels close to it become transparent,
    everything else is kept.  Returns a trimmed RGBA image or None.
    """
    if atlas is None:
        return None
    cx, cy, area = anchor
    side = int(round((float(area) ** 0.5) * margin))
    h, w = atlas.shape[:2]
    x0 = max(0, cx - side // 2)
    y0 = max(0, cy - side // 2)
    crop = atlas[y0:y0 + side, x0:x0 + side]
    if crop.size == 0:
        return None
    q = (crop // 16).astype(np.int32)
    keys = q[:, :, 0] * 65536 + q[:, :, 1] * 256 + q[:, :, 2]
    vals, counts = np.unique(keys, return_counts=True)
    k = int(vals[int(np.argmax(counts))])
    bg = np.array([(k >> 16) & 255, (k >> 8) & 255, k & 255], dtype=np.float32)
    d = np.sqrt(((crop.astype(np.float32) - bg) ** 2).sum(axis=2))
    alpha = np.clip((d - bg_tol) / soft, 0.0, 1.0) * 255.0
    if float(alpha.max()) < 40:                            # nothing stood out
        return None
    rgba = np.dstack([crop, alpha.astype(np.uint8)])
    return _trim_alpha(Image.fromarray(rgba, "RGBA"), threshold=40)


def supplier_mark(sider, team_id, slot: int = 1) -> Image.Image | None:
    return extract_mark(source_atlas(sider, team_id, slot), kit_regions.KIT_SUPPLIER)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="inspect a club's sourced artwork")
    ap.add_argument("club_id", type=int)
    ap.add_argument("--sider", default=str(DEFAULT_SIDER))
    args = ap.parse_args()

    sider = Path(args.sider)
    em = load_emblem(sider, args.club_id)
    at = source_atlas(sider, args.club_id)
    sup = supplier_mark(sider, args.club_id)
    print("emblem  :", em.size if em else None, emblem_path(sider, args.club_id).is_file())
    print("atlas   :", at.shape if at is not None else None)
    print("supplier:", sup.size if sup else None)
