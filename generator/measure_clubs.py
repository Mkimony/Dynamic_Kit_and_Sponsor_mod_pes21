#!/usr/bin/env python3
r"""measure_clubs.py - rebuild generator/clubs.csv by MEASURING the real kits.

The old clubs.csv was hand-typed and wrong: made-up team ids, 78 clubs sharing
one red, 258 clubs with no real accent, some colours reversed.  This tool
replaces all of that with data measured from the game's own kit textures.

Where everything comes from
---------------------------
* id -> name      : content/anthem-jacket/tim/map.txt   (the game's own table)
* id -> kit folder: content/kits/map.txt                (the ACTIVE override)
* colours         : the club's real home kit texture, sampled through the UV
                    masks in kit_regions.py (shirt body, sleeves, shorts, socks)
* structure       : detected from the same shirt pixels (stripes / hoops / plain)

Output columns
--------------
    team_id,name,primary,secondary,accent,tier,src,structure

`src` is "measured" for a row that came from a real texture, or "fallback" for
one that had to borrow a palette.  `structure` is one of
plain / vertical / horizontal / halves / check.

Usage
-----
    python measure_clubs.py                       # write generator/clubs.csv
    python measure_clubs.py --ids 102,205,1203    # just these, print a table
    python measure_clubs.py --sider "C:\...\SiderAddons"
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ftex_io          # noqa: E402
import kit_regions      # noqa: E402
from club_resolver import tier_from_id   # noqa: E402

DEFAULT_SIDER = Path(r"C:\Program Files (x86)\SP Football Life 2027\SiderAddons")


# --------------------------------------------------------------------------- #
#  parsing the game's own tables
# --------------------------------------------------------------------------- #
def load_kv_map(path: Path) -> dict[int, str]:
    """team_id, "folder"  ->  {id: folder}; first non-comment entry wins."""
    out: dict[int, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0]
        if "," not in line:
            continue
        tid, rest = line.split(",", 1)
        tid = tid.strip()
        m = re.search(r'"([^"]+)"', rest)
        folder = m.group(1) if m else rest.strip().split(",")[0].strip()
        if tid.isdigit() and folder:
            out.setdefault(int(tid), folder)
    return out


def club_name(folder: str) -> str:
    return folder.replace("\\", "/").rstrip("/").split("/")[-1].strip()


# --------------------------------------------------------------------------- #
#  texture -> pixels
# --------------------------------------------------------------------------- #
def ftex_to_rgb(path: Path) -> np.ndarray | None:
    """Decode a .ftex to an (H, W, 3) uint8 array (mip 0), or None."""
    try:
        data = Path(path).read_bytes()
        dds = ftex_io.ftex_to_dds(data)
        img = Image.open(io.BytesIO(dds)).convert("RGB")
        return np.asarray(img)
    except Exception:                                     # noqa: BLE001
        return None


def find_home_ftex(kits_root: Path, folder: str, team_id: int) -> Path | None:
    d = kits_root / folder / "p1"
    if not d.is_dir():
        return None
    exact = d / f"u{team_id:04d}p1.ftex"
    if exact.is_file():
        return exact
    cands = sorted(d.glob("u*p1.ftex"))
    return cands[0] if cands else None


# --------------------------------------------------------------------------- #
#  colour clustering (quantised histogram + greedy separation)
# --------------------------------------------------------------------------- #
def _quant_keys(pixels: np.ndarray, q: int) -> np.ndarray:
    qp = (pixels.astype(np.int32) // q) * q + q // 2
    return qp[:, 0] * 65536 + qp[:, 1] * 256 + qp[:, 2]


def dominant_colours(pixels: np.ndarray, q: int = 16, limit: int = 64):
    """[(count, (r,g,b)), ...] most common coarse-quantised colours."""
    if pixels.size == 0:
        return []
    keys = _quant_keys(pixels, q)
    uniq, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)
    out = []
    for i in order[:4000]:
        k = int(uniq[i])
        out.append((int(counts[i]), ((k >> 16) & 255, (k >> 8) & 255, k & 255)))
        if len(out) >= limit:
            break
    return out


def cluster_colours(pixels: np.ndarray, q: int = 16, merge_d: float = 0.10):
    """[(count, (r,g,b)), ...] merged dominant colours, most common first.

    Near-duplicate buckets (a body colour and its own shadow/highlight) collapse
    into one cluster, so a plain shirt yields ONE cluster and a striped shirt
    yields one cluster per stripe colour.
    """
    buckets = dominant_colours(pixels, q=q)
    total = int(pixels.shape[0]) if pixels.ndim == 2 else 0
    clusters: list[list] = []          # [count, sum_r, sum_g, sum_b]
    for count, col in buckets:
        for cl in clusters:
            mean = (cl[1] / cl[0], cl[2] / cl[0], cl[3] / cl[0])
            if _dist(col, mean) < merge_d:
                cl[0] += count
                cl[1] += col[0] * count
                cl[2] += col[1] * count
                cl[3] += col[2] * count
                break
        else:
            clusters.append([count, col[0] * count, col[1] * count, col[2] * count])
    clusters.sort(key=lambda c: -c[0])
    out = []
    for c in clusters:
        out.append((c[0], (round(c[1] / c[0]), round(c[2] / c[0]), round(c[3] / c[0]))))
    return out, total


def _lab(rgb):
    r, g, b = (v / 255.0 for v in rgb)
    r = r / 12.92 if r <= 0.04045 else ((r + 0.055) / 1.055) ** 2.4
    g = g / 12.92 if g <= 0.04045 else ((g + 0.055) / 1.055) ** 2.4
    b = b / 12.92 if b <= 0.04045 else ((b + 0.055) / 1.055) ** 2.4
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b))) ** 0.5


def _chroma(rgb):
    _l, a, b = _lab(rgb)
    return (a * a + b * b) ** 0.5


def pick_distinct(cands, exclude, total, min_d=0.10, min_frac=0.02, min_chroma=0.0):
    """First candidate far enough from every excluded colour (or None).

    ``total`` is the pixel count of the WHOLE region the candidates came from,
    not the sum of the candidates, so ``min_frac`` is a true coverage fraction
    (this is what stops a 0.7% speck from becoming a club's secondary colour).
    ``min_chroma`` rejects near-neutral colours (anti-aliasing greys), which is
    how an accent stays an actual club colour instead of a white/black blend.
    """
    if total <= 0:
        total = sum(c for c, _ in cands) or 1
    for count, col in cands:
        if count / total < min_frac:
            continue
        if min_chroma and _chroma(col) < min_chroma:
            continue
        if all(_dist(col, e) > min_d for e in exclude):
            return col
    return None


# --------------------------------------------------------------------------- #
#  structure detection
# --------------------------------------------------------------------------- #
def detect_structure(rgb: np.ndarray, shirt_mask: np.ndarray) -> str:
    ys, xs = np.where(shirt_mask)
    if len(xs) < 500:
        return "plain"
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    sub = rgb[y0:y1, x0:x1].astype(np.float32)
    sub_mask = shirt_mask[y0:y1, x0:x1]
    lum = 0.2126 * sub[:, :, 0] + 0.7152 * sub[:, :, 1] + 0.0722 * sub[:, :, 2]
    lum = np.where(sub_mask, lum, np.nan)
    with np.errstate(invalid="ignore"):
        col_mean = np.nanmean(lum, axis=0)
        row_mean = np.nanmean(lum, axis=1)
    col_mean = col_mean[~np.isnan(col_mean)]
    row_mean = row_mean[~np.isnan(row_mean)]
    if col_mean.size < 4 or row_mean.size < 4:
        return "plain"
    vcol, vrow = float(np.var(col_mean)), float(np.var(row_mean))

    # halves: is one half markedly lighter than the other?
    half = sub.shape[1] // 2
    lm = np.where(sub_mask[:, :half], lum[:, :half], np.nan)
    rm = np.where(sub_mask[:, half:], lum[:, half:], np.nan)
    with np.errstate(invalid="ignore"):
        lmean, rmean = np.nanmean(lm), np.nanmean(rm)
    if not (np.isnan(lmean) or np.isnan(rmean)) and abs(lmean - rmean) > 26:
        return "halves"

    # counts of crossings tell stripes from a single panel
    def crossings(sig):
        s = sig - sig.mean()
        sign = np.sign(s)
        sign[sign == 0] = 1
        return int(np.sum(np.abs(np.diff(sign)) > 0))

    # Absolute variance floors: a plain shirt still has a little shading, so a
    # mere 1.4x ratio is not enough - the signal must actually swing (lum^2).
    cc, rc = crossings(col_mean), crossings(row_mean)
    if cc >= 4 and vcol > 300 and vcol > 1.4 * max(vrow, 1.0):
        return "vertical"
    if rc >= 3 and vrow > 300 and vrow > 1.4 * max(vcol, 1.0):
        return "horizontal"
    if cc >= 5 and rc >= 4 and vcol > 300 and vrow > 300:
        return "check"
    return "plain"


# --------------------------------------------------------------------------- #
#  one club
# --------------------------------------------------------------------------- #
def measure_club(team_id: int, folder: str, masks, kits_root: Path):
    """Return a row dict, or None when the texture cannot be read."""
    ftex = find_home_ftex(kits_root, folder, team_id)
    if not ftex:
        return None
    rgb = ftex_to_rgb(ftex)
    if rgb is None:
        return None
    h, w = rgb.shape[:2]

    def sample(name):
        m = masks.get(name)
        if m is None or m.shape != (h, w):
            return np.empty((0, 3), dtype=np.uint8)
        ys, xs = np.where(m)
        return rgb[ys, xs]

    shirt = np.concatenate([sample("body_front"), sample("body_back")]) \
        if sample("body_front").size else sample("body_front")
    sleeves = sample("sleeve")
    shorts = sample("shorts")
    socks = sample("socks")

    shirt_c, shirt_total = cluster_colours(shirt)
    other_px = np.concatenate([sleeves, shorts, socks]) \
        if (sleeves.size or shorts.size or socks.size) else sleeves
    other, other_total = cluster_colours(other_px)

    if not shirt_c:
        return None
    primary = shirt_c[0][1]
    primary_neutral = _chroma(primary) < 0.03
    # When the body is itself neutral (white/black), the "second colour" must be
    # a real colour, not the grey that anti-aliasing leaves between white and
    # black.  When the body is colourful, a neutral second colour is normal
    # (blue + white, red + black, ...).
    sec_chroma = 0.03 if primary_neutral else 0.0
    secondary = pick_distinct(shirt_c[1:], [primary], shirt_total, min_d=0.12,
                              min_chroma=sec_chroma)
    if secondary is None:
        secondary = pick_distinct(other, [primary], other_total, min_d=0.12,
                                  min_chroma=sec_chroma)
    if secondary is None:
        # a clean black or white, whichever contrasts with the body
        secondary = (16, 16, 18) if _lab(primary)[0] > 0.6 else (245, 245, 245)
    accent = pick_distinct(other, [primary, secondary], other_total, min_d=0.12,
                           min_chroma=0.035)
    if accent is None:
        accent = pick_distinct(shirt_c[1:], [primary, secondary], shirt_total,
                               min_d=0.12, min_chroma=0.035)
    if accent is None or _dist(accent, primary) < 0.10 or _dist(accent, secondary) < 0.10:
        # No real third colour (or it collided with one of the other two): fall
        # back to a neutral that contrasts with BOTH the body and the second
        # colour, so an accent trim never vanishes into the shirt.
        lo = min(_lab(primary)[0], _lab(secondary)[0])
        hi = max(_lab(primary)[0], _lab(secondary)[0])
        if lo > 0.55:
            accent = (18, 18, 20)             # both light -> dark accent
        elif hi < 0.45:
            accent = (242, 242, 244)          # both dark -> light accent
        else:
            accent = (112, 114, 120)          # one light, one dark -> mid grey

    structure = detect_structure(rgb, masks["body_front"]) \
        if masks.get("body_front") is not None else "plain"

    return {"team_id": team_id,
            "name": club_name(folder),
            "primary": _hex(primary),
            "secondary": _hex(secondary),
            "accent": _hex(accent),
            "tier": tier_from_id(team_id),
            "src": "measured",
            "structure": structure}


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(int(max(0, min(255, v))) for v in rgb)


def _shade(rgb, f):
    return tuple(max(0, min(255, int(round(v * f)))) for v in rgb)


# --------------------------------------------------------------------------- #
#  driver
# --------------------------------------------------------------------------- #
def build_rows(sider: Path, only=None, verbose=True):
    kits_root = sider / "content" / "kits"
    kitmap = load_kv_map(kits_root / "map.txt")
    anthem = load_kv_map(sider / "content" / "anthem-jacket" / "tim" / "map.txt")
    masks = kit_regions.build_masks()

    ids = sorted(kitmap) if not only else [i for i in sorted(kitmap) if i in only]
    rows, skipped = [], []
    for n, tid in enumerate(ids, 1):
        folder = kitmap[tid]
        if not (kits_root / folder).is_dir():
            skipped.append((tid, folder, "folder missing"))
            continue
        try:
            row = measure_club(tid, folder, masks, kits_root)
        except Exception as exc:                          # noqa: BLE001
            skipped.append((tid, folder, "%s: %s" % (type(exc).__name__, exc)))
            continue
        if row is None:
            skipped.append((tid, folder, "no readable p1 texture"))
            continue
        rows.append(row)
        if verbose and n % 25 == 0:
            print("  measured %d/%d ..." % (n, len(ids)), flush=True)
    # Prefer the game's own club name (anthem table) over the folder leaf; both
    # are league\club paths, so keep only the club segment.
    for r in rows:
        nm = anthem.get(r["team_id"])
        if nm:
            r["name"] = club_name(nm)
    return rows, skipped


def write_csv(rows, path: Path):
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["team_id", "name", "primary", "secondary",
                                          "accent", "tier", "src", "structure"])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sider", default=str(DEFAULT_SIDER))
    ap.add_argument("--out", default=str(HERE / "clubs.csv"))
    ap.add_argument("--ids", default=None, help="comma-separated team ids (debug)")
    args = ap.parse_args(argv)

    sider = Path(args.sider)
    only = None
    if args.ids:
        only = {int(x) for x in args.ids.replace(" ", "").split(",") if x}

    rows, skipped = build_rows(sider, only=only)
    if only:
        for r in rows:
            print("%(team_id)6s  %(primary)s %(secondary)s %(accent)s  "
                  "%(structure)-10s %(name)s" % r)
        for s in skipped:
            print("  SKIP", s)
        return 0

    write_csv(rows, Path(args.out))
    print("wrote %s  (%d clubs, %d skipped)" % (args.out, len(rows), len(skipped)))
    if skipped:
        print("skipped:")
        for tid, folder, why in skipped[:40]:
            print("  %6d  %-50s %s" % (tid, folder, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
