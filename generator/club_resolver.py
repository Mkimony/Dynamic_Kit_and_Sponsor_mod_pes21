#!/usr/bin/env python3
r"""club_resolver.py - work out a club row (id, name, colours, tier) for ANY team.

The kit generator used to know only the handful of clubs listed in clubs.csv.
Now that the Lua mod auto-detects whichever club you manage, the watcher has to
build a kit for a team id it may never have seen.  This module turns such an id
into the same kind of row clubs.csv holds:

    {"id": 205, "name": "Middlesbrough",
     "primary": "#E21C25", "secondary": "#FFFFFF", "accent": "#111111",
     "tier": "mid"}

Where the colours come from, best first
---------------------------------------
   1. clubs.csv (MEASURED)            the club's colours read straight off its
                                      own kit texture by measure_clubs.py.  This
                                      is the club's real, current palette.
   2. CURATED                         a hand-checked real-life palette for the
                                      prominent clubs whose kits live inside a
                                      CPK (so they cannot be measured).
   3. content/KitGenerator/club.txt   written by KitGenerator.lua from the LIVE
                                      game (ctx.kits.get) -- the club's real kit
                                      colours, exactly as the game holds them.
   4. content/kits/map.txt + the kit   the club's kit folder ("England\
      folder's p1/config.txt           Middlesbrough"); its config.txt carries
                                      ShirtColor1 / ShortsColor / ... keys.
   5. a deterministic palette         derived from the team id with the same
                                      djb2 roll the Lua mod uses, so the colours
                                      are stable across sessions.

The row also carries a `structure` (plain / vertical / horizontal / halves /
check) which the renderer uses to pick a matching modern pattern family.

Nothing here is random at run time: the same team id always resolves to the same
colours, whether or not the game is running.
"""
from pathlib import Path

from kg_core import h32, color_dist

DEFAULT_SIDER = Path(r"C:\Program Files (x86)\SP Football Life 2027\SiderAddons")

# Deterministic fallback kits, used only when neither club.txt nor a kit
# config.txt has anything for the club.  Each triple is (home, pattern, accent).
FALLBACK_PALETTES = [
    ("#034694", "#FFFFFF", "#C8102E"),   # royal blue
    ("#B3121F", "#FFFFFF", "#111111"),   # red
    ("#0A7EA4", "#F5F5F5", "#FFB020"),   # sky / teal
    ("#2E8B2E", "#FFFFFF", "#111111"),   # green
    ("#111111", "#F2F2F2", "#D61F69"),   # black / pink
    ("#6A1FB8", "#F5F5F5", "#F2C200"),   # purple / gold
    ("#F2C200", "#111111", "#FFFFFF"),   # yellow / black
    ("#7A1F1F", "#F5F5F5", "#E8B23A"),   # maroon / gold
    ("#1B3A8C", "#FFFFFF", "#F2C200"),   # navy / gold
    ("#0E7C5A", "#FFFFFF", "#F2C200"),   # emerald / gold
    ("#C8102E", "#FFFFFF", "#0E1B3A"),   # red / navy
    ("#3A3A3A", "#FFFFFF", "#E8B23A"),   # graphite / gold
    ("#E86A17", "#FFFFFF", "#111111"),   # orange / black
    ("#F5F5F5", "#034694", "#C8102E"),   # white / blue
    ("#1A1A2E", "#F5F5F5", "#E94560"),   # midnight / red
    ("#00857C", "#F5F5F5", "#FF6B35"),   # teal / orange
]

_MAP_CACHE = {}          # str(sider) -> {team_id: folder}
_MEASURED = None         # {team_id: row} from clubs.csv (loaded once)
_HERE = Path(__file__).resolve().parent


# Curated real-life palettes for prominent clubs whose kits live inside a CPK and
# so cannot be measured from a loose texture (see measure_clubs.py).  Keyed by
# the game's team id:
#     name: (primary, secondary, accent, structure)
# These are the clubs' classic identities; the measured table still wins for the
# clubs it covers.
CURATED = {
    100:  ("Manchester United", "#DA291C", "#111111", "#FBE122", "plain"),
    101:  ("Arsenal", "#EF0107", "#FFFFFF", "#063672", "plain"),
    102:  ("Chelsea", "#034694", "#FFFFFF", "#C8102E", "plain"),
    103:  ("Liverpool", "#C8102E", "#F6EB61", "#00B2A9", "plain"),
    107:  ("Aston Villa", "#95BFE5", "#670E36", "#FFFFFF", "plain"),
    108:  ("Barcelona", "#A50044", "#004D98", "#EDBB00", "vertical"),
    109:  ("Real Madrid", "#FEBE10", "#FFFFFF", "#00529F", "plain"),
    110:  ("Valencia", "#F5F5F5", "#EE3524", "#F1BE48", "plain"),
    116:  ("Ajax", "#D2122E", "#FFFFFF", "#000000", "plain"),
    117:  ("Feyenoord", "#D71920", "#FFFFFF", "#000000", "halves"),
    118:  ("PSV", "#ED1C24", "#FFFFFF", "#000000", "plain"),
    119:  ("Internazionale", "#0068A8", "#000000", "#FFFFFF", "vertical"),
    120:  ("Juventus", "#000000", "#FFFFFF", "#C8A84B", "vertical"),
    121:  ("Milan", "#FB090B", "#000000", "#FFFFFF", "vertical"),
    122:  ("Lazio", "#87D8F7", "#FFFFFF", "#0B1B3A", "plain"),
    125:  ("Roma", "#8E1F2F", "#F0BC42", "#FFFFFF", "plain"),
    126:  ("Dortmund", "#FDE100", "#000000", "#FFFFFF", "plain"),
    128:  ("Leverkusen", "#E32221", "#000000", "#FFFFFF", "halves"),
    130:  ("Galatasaray", "#A90432", "#FDB912", "#FFFFFF", "halves"),
    131:  ("Celtic", "#018749", "#FFFFFF", "#000000", "plain"),
    132:  ("Rangers", "#1B458F", "#FFFFFF", "#CC0000", "plain"),
    172:  ("Atletico", "#CB3524", "#FFFFFF", "#1B2B5E", "vertical"),
    173:  ("Manchester City", "#6CABDD", "#1C2C5B", "#FFFFFF", "plain"),
    174:  ("Anderlecht", "#FFFFFF", "#4B2E83", "#000000", "plain"),
    179:  ("Tottenham", "#FFFFFF", "#132257", "#000000", "plain"),
    191:  ("Benfica", "#E00000", "#FFFFFF", "#000000", "plain"),
    197:  ("Fenerbahce", "#FFED00", "#0A1A3C", "#FFFFFF", "vertical"),
    265:  ("Sevilla", "#FFFFFF", "#D8232A", "#000000", "plain"),
    327:  ("Napoli", "#12A0D7", "#FFFFFF", "#000000", "plain"),
    5010: ("Leipzig", "#FFFFFF", "#DD0741", "#001F47", "plain"),
}


def tier_from_id(club_id):
    """The same small/mid/big roll the Lua synth uses: hash('tier:<id>') % 3."""
    return ("small", "mid", "big")[h32("tier:%d" % int(club_id)) % 3]


def _hexc(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in range(0, 6, 2))


def _lum(rgb):
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def _norm_hex(value):
    """Normalise a colour the game/config may write in any of its shapes.

    Accepts '#034694', '034694', '#034694FF' (alpha ignored), '0x034694' or a
    packed integer.  Returns '#RRGGBB' (upper) or None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return "#%06X" % (int(value) & 0xFFFFFF)
    s = str(value).strip().lstrip("#")
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) >= 6:
        s = s[:6]
    if len(s) != 6:
        return None
    try:
        int(s, 16)
    except ValueError:
        return None
    return "#" + s.upper()


def _shift(colour, amount):
    """Lighten (amount>0) or darken (amount<0) a colour."""
    r, g, b = _hexc(colour)
    if amount > 0:
        r, g, b = (int(v + (255 - v) * amount) for v in (r, g, b))
    else:
        r, g, b = (int(v * (1 + amount)) for v in (r, g, b))
    return "#%02X%02X%02X" % (r, g, b)


def default_palette(club_id):
    return FALLBACK_PALETTES[h32("palette:%d" % int(club_id)) % len(FALLBACK_PALETTES)]


def _pick(cands, exclude, min_d=0.08):
    """First candidate far enough from every colour in `exclude` (or None)."""
    for c in cands:
        if all(color_dist(_hexc(c), _hexc(e)) > min_d for e in exclude):
            return c
    return None


def palette_from_colors(club_id, colors):
    """Turn the game's kit colours into (primary, secondary, accent)."""
    primary = colors.get("shirt1") or colors.get("shirt2")
    if not primary:
        return default_palette(club_id)

    cands = [colors.get(k) for k in ("shirt2", "shorts", "socks", "uni2", "uni1")]
    cands = [c for c in cands if c]

    secondary = _pick(cands, [primary])
    if secondary is None:
        secondary = "#FFFFFF" if _lum(_hexc(primary)) < 140 else "#101014"

    accent = _pick(cands, [primary, secondary])
    if accent is None:
        rest = [c for c in cands
                if c != secondary and color_dist(_hexc(c), _hexc(primary)) > 0.08]
        if rest:
            accent = max(rest, key=lambda c: color_dist(_hexc(c), _hexc(primary)))
        else:
            light, dark = _shift(primary, 0.55), _shift(primary, -0.55)
            accent = max((light, dark),
                         key=lambda c: min(color_dist(_hexc(c), _hexc(primary)),
                                           color_dist(_hexc(c), _hexc(secondary))))
    return primary, secondary, accent


def read_club_txt(sider):
    """The club.txt written by the Lua mod, or None."""
    p = Path(sider) / "content" / "KitGenerator" / "club.txt"
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        try:
            cid = int(parts[0])
        except (ValueError, IndexError):
            continue
        rec = {"id": cid, "name": parts[1].strip() if len(parts) > 1 else ""}
        for i, key in enumerate(("shirt1", "shirt2", "shorts", "socks", "uni1", "uni2")):
            rec[key] = _norm_hex(parts[2 + i]) if len(parts) > 2 + i else None
        return rec
    return None


def read_map(sider):
    """content/kits/map.txt -> {team_id: 'England\\Middlesbrough'}."""
    key = str(Path(sider))
    if key in _MAP_CACHE:
        return _MAP_CACHE[key]
    out = {}
    p = Path(sider) / "content" / "kits" / "map.txt"
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.split("#", 1)[0]
            if "," not in line:
                continue
            tid, rest = line.split(",", 1)
            tid = tid.strip()
            rest = rest.strip()
            if rest.startswith('"'):
                folder = rest[1:].split('"', 1)[0]
            else:
                folder = rest.split()[0] if rest else ""
            if tid.isdigit() and folder:
                out[int(tid)] = folder
    _MAP_CACHE[key] = out
    return out


def read_config_colors(path):
    """Parse the colour keys out of a kit config.txt (p1/p2/p3)."""
    colors = {}
    p = Path(path)
    if not p.is_file():
        return colors
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return colors
    want = {
        "shirt1": "ShirtColor1", "shirt2": "ShirtColor2",
        "shorts": "ShortsColor", "socks": "SocksColor",
        "uni1": "UniColor_Color1", "uni2": "UniColor_Color2",
    }
    for line in text.splitlines():
        line = line.split(";", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        for slot, key in want.items():
            if k == key:
                c = _norm_hex(v)
                if c:
                    colors[slot] = c
    return colors


def _row_from_csv(club_id, r):
    """A club row from a clubs.csv-shaped mapping (adds `structure`)."""
    return {
        "id": int(club_id),
        "name": (r.get("name") or "").strip() or ("Club #%d" % int(club_id)),
        "primary": _norm_hex(r.get("primary")) or "#034694",
        "secondary": _norm_hex(r.get("secondary")) or "#FFFFFF",
        "accent": _norm_hex(r.get("accent")) or "#C8102E",
        "tier": (r.get("tier") or "").strip() or tier_from_id(club_id),
        "structure": (r.get("structure") or "plain").strip() or "plain",
    }


def measured_row(club_id):
    """The MEASURED clubs.csv row for `club_id` (or None). Loaded once."""
    global _MEASURED
    if _MEASURED is None:
        _MEASURED = {}
        p = _HERE / "clubs.csv"
        if p.is_file():
            import csv
            with open(p, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    try:
                        cid = int(r.get("team_id"))
                    except (TypeError, ValueError):
                        continue
                    _MEASURED.setdefault(cid, _row_from_csv(cid, r))
    return _MEASURED.get(int(club_id))


def resolve(club_id, sider=None, csv_row=None):
    """Return a club row for `club_id` (see the module docstring).

    Precedence: measured clubs.csv -> curated -> live club.txt -> kit
    config.txt -> deterministic fallback.  `csv_row` short-circuits the search.
    """
    club_id = int(club_id)
    sider = Path(sider or DEFAULT_SIDER)

    # 1. measured (the club's own kit texture, via measure_clubs.py)
    if csv_row is not None:
        return _row_from_csv(club_id, csv_row)
    row = measured_row(club_id)
    if row is not None:
        return dict(row)

    # 2. curated real-life palette for prominent CPK-only clubs
    cur = CURATED.get(club_id)
    if cur is not None:
        name, primary, secondary, accent, structure = cur
        return {"id": club_id, "name": name,
                "primary": primary, "secondary": secondary, "accent": accent,
                "tier": tier_from_id(club_id), "structure": structure}

    # 3. live club.txt (written by the Lua mod from the running game)
    name = None
    colors = {}
    live = read_club_txt(sider)
    if live and live["id"] == club_id:
        name = live.get("name") or None
        colors = {k: live.get(k) for k in ("shirt1", "shirt2", "shorts", "socks", "uni1", "uni2")}

    # 4. the club's kit folder / config.txt
    folder = read_map(sider).get(club_id)
    if folder:
        if not name:
            name = folder.replace("\\", "/").rstrip("/").split("/")[-1]
        cfg = read_config_colors(sider / "content" / "kits" / folder / "p1" / "config.txt")
        for k, v in cfg.items():
            if not colors.get(k):
                colors[k] = v                # the live game read wins over config.txt

    primary, secondary, accent = palette_from_colors(club_id, colors)
    return {"id": club_id, "name": name or ("Club #%d" % club_id),
            "primary": primary, "secondary": secondary, "accent": accent,
            "tier": tier_from_id(club_id), "structure": "plain"}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="resolve a team id to a club row")
    ap.add_argument("club_id", type=int)
    ap.add_argument("--sider", default=str(DEFAULT_SIDER))
    args = ap.parse_args()
    row = resolve(args.club_id, args.sider)
    for k in ("id", "name", "primary", "secondary", "accent", "tier"):
        print("%-10s %s" % (k, row[k]))
