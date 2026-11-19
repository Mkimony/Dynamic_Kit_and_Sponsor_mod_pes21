#!/usr/bin/env python3
"""kit_render.py - paint a generated kit design into the PES 2021 UV atlas.

Pipeline
--------
    club + season  ->  design_from_club          (per-club / per-slot / per-season)
                   ->  kit_patterns.draw          (modern 205-code taxonomy)
                   ->  shirt-local tile  ->  front + back panels   (UV atlas)
                   ->  club badge + supplier + sponsor logos
                   ->  2048x2048 RGBA atlas PNG
                   ->  ftex_io.py repack  ->  u<team>p<slot>.ftex  ->  livecpk

What makes a kit "this club's"
------------------------------
* **Colours** come from clubs.csv, which is measured from the game's own kit
  textures (see measure_clubs.py) - never invented, never jittered.
* **Pattern** is chosen from the modern taxonomy (kit_patterns.py) so no kit is
  a 1990s hoop/check.  The family is matched to the club's *real* shirt
  structure (a striped club keeps stripes, a plain club gets a modern graphic)
  and the exact code is a deterministic roll of (club, slot, sponsor, season), so
  every club is different and no club repeats the same pattern two seasons
  running.
* **Badge** is the club's real crest (content/anthem-jacket/cache/emblem/<id>.png).
* **Supplier** mark is lifted from the club's own loose kit texture when one
  exists.
* **Sponsor** logo is stamped only for a signed deal, on a transparent plate.

Nothing is copied from another club's kit: the emblem cache and the supplier
lift are the club's own artwork.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import kit_gen
import kit_patterns
import kit_regions
from kg_core import h32

ATLAS = 2048
TILE = 1024                      # legacy tile size (kept for callers)
_SS = 2                          # pattern supersample before it is scaled up

# kit_gen's preview draws the shirt inside a 480x500 canvas; this is the body
# bounding box of that preview shirt (see kit_gen.shirt_masks()).
_PREVIEW_SHIRT_BOX = (108, 40, 372, 470)

DEFAULT_DESIGN = {
    "body": (3, 70, 148),         # main shirt colour
    "second": (255, 255, 255),    # pattern colour
    "accent": (200, 168, 75),     # accent colour
    "third": None,                # optional 3rd colour for pattern hairlines
    "pattern": "plain",
    "var": 0,                     # pattern variant (0..2)
    "width_step": 0,              # stripe width step (0..7)
    "gradient": 0,                # 0..3 vertical shading strength
    "sleeve": None,               # None = same as body
    "shorts": None,               # None = same as body
    "socks": None,                # None = same as body
    "collar": None,               # None = same as body
    "cuff": None,                 # None = accent (sleeve cuff / shorts stripe)
    "sponsor": None,              # path to a PNG logo (chest)
    "badge": None,                # path to a PNG logo (club badge)
    "supplier": None,             # path to a PNG logo (kit maker)
    "sponsor_white": False,       # True = flatten the logo to white
    "badge_white": False,
    "supplier_white": False,
    "sponsor_plate": False,       # transparent by default: no plate behind the logo
}


def _hexc(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in range(0, 6, 2))


def _mask_img(mask):
    """numpy bool mask -> single-channel PIL mask (255 where True)."""
    return Image.fromarray((mask * 255).astype("uint8"), "L")


# --------------------------------------------------------------------------- #
#  modern pattern selection
# --------------------------------------------------------------------------- #
# The 205-code taxonomy, bucketed by what each code *renders as* (measured by
# rendering every code and looking at its vertical/horizontal structure - see the
# classification in the repo history).  A club's measured shirt structure picks
# the family, so a striped club keeps stripes and a plain club gets a modern
# graphic instead of a 1990s check.
PATTERN_FAMILIES = {
    "vertical": [
        "004", "015", "017", "031", "046", "050", "066", "081", "082", "083",
        "085", "086", "092", "093", "096", "097", "101", "102", "105", "107",
        "108", "110", "111", "114", "116", "117", "118", "121", "124", "128",
        "134", "144", "161", "185", "191", "196", "202",
    ],
    "horizontal": [
        "003", "008", "009", "010", "011", "016", "018", "019", "021", "028",
        "029", "039", "049", "055", "059", "068", "070", "076", "084", "091",
        "095", "100", "109", "115", "120", "123", "126", "129", "130", "137",
        "141", "147", "148", "149", "150", "162", "163", "164", "171", "173",
        "177", "178", "188", "192", "193", "200", "201", "204",
    ],
    "split": [
        "001", "007", "023", "025", "043", "057", "060", "078", "088", "106",
        "112",
    ],
    "plain": [
        "013", "014", "026", "040", "053", "061", "074", "079", "119", "125",
        "139", "142", "156", "160", "165", "174", "180", "194", "195", "203",
        "205",
    ],
    "graphic": [
        "002", "005", "006", "012", "020", "022", "024", "027", "030", "032",
        "033", "034", "035", "036", "037", "038", "041", "042", "044", "045",
        "047", "048", "051", "052", "054", "056", "058", "062", "063", "064",
        "065", "067", "069", "071", "072", "073", "075", "077", "080", "087",
        "089", "090", "094", "098", "099", "103", "104", "113", "122", "127",
        "131", "132", "133", "135", "136", "138", "140", "143", "145", "146",
        "151", "152", "153", "154", "155", "157", "158", "159", "166", "167",
        "168", "169", "170", "172", "175", "176", "179", "181", "182", "183",
        "184", "186", "187", "189", "190", "197", "198", "199",
    ],
}

# measured shirt structure -> pattern family
_STRUCTURE_FAMILY = {
    "vertical": "vertical",
    "horizontal": "horizontal",
    "halves": "split",
    "check": "graphic",
    "plain": "graphic",
}

# legacy named patterns -> an equivalent modern catalog code
_LEGACY_PATTERN = {
    "plain": "plain",
    "vstripes": "083", "hoops": "084", "sash": "103", "halves": "088",
    "chestband": "095", "pinstripe": "085", "checks": "094",
    "chevron": "011", "diagonal": "005", "fadegeo": "044", "blocks": "007",
}

_MODIFIERS = ("", "E", "B", "C", "D")


def _family_pool(structure):
    fam = _STRUCTURE_FAMILY.get((structure or "plain").lower(), "graphic")
    return PATTERN_FAMILIES[fam]


def pattern_spec(club_id, structure, slot, season, sponsor=None, pool=None):
    """Deterministic modern pattern spec for one kit.

    * the club's real structure picks the family (unless `pool` is given, e.g. by
      KIT_STYLE);
    * home / away / third draw from disjoint slices, so the three kits never
      share a pattern;
    * the index advances by 1 every season, so a multi-year deal still gives a
      new kit each year and no club repeats two seasons running;
    * a signed sponsor re-rolls the choice and is guaranteed to differ from the
      no-sponsor look.
    """
    src = list(pool) if pool else _family_pool(structure)
    if not src:
        src = PATTERN_FAMILIES["graphic"]
    n = len(src)
    slot = max(1, min(3, int(slot)))
    if pool is None:
        start = ((slot - 1) * n) // 3
        end = (slot * n) // 3
        sl = src[start:end] or src
    else:
        sl = src

    base = h32("pat:%d:%d:%s:" % (int(club_id), slot, str(structure or "")))
    plain_idx = (base + int(season)) % len(sl)
    if sponsor:
        choices = [p for i, p in enumerate(sl) if i != plain_idx] or sl
        idx = (h32("pat:%d:%d:%s" % (int(club_id), slot, sponsor)) + int(season)) % len(choices)
        code = choices[idx]
    else:
        code = sl[plain_idx]

    code = _LEGACY_PATTERN.get(code, code) if not str(code)[0].isdigit() else code
    mod = _MODIFIERS[h32("mod:%d:%d:%d" % (int(club_id), slot, int(season))) % len(_MODIFIERS)]
    return code + ("-" + mod if mod else "")


# --------------------------------------------------------------------------- #
#  shirt-local design tile
# --------------------------------------------------------------------------- #
def _pattern_canvas(spec, body, second, third, var, ws, scale=_SS):
    """Render a catalog spec on a supersampled 480x500 canvas."""
    ow, oh = kit_patterns.W, kit_patterns.H
    try:
        kit_patterns.W, kit_patterns.H = ow * scale, oh * scale
        canvas = Image.new("RGBA", (ow * scale, oh * scale), (0, 0, 0, 0))
        kit_patterns.draw(canvas, spec, body, second, third=third, var=var, ws=ws)
    finally:
        kit_patterns.W, kit_patterns.H = ow, oh
    return canvas


def _apply_gradient(img, strength):
    """Capped vertical shading (top lighter -> hem darker); never a colour shift."""
    if strength <= 0:
        return img
    a = np.asarray(img).astype(np.float32)
    h = a.shape[0]
    k = (1.0 - float(strength) * (np.arange(h, dtype=np.float32) / max(1, h))).reshape(h, 1, 1)
    a[:, :, :3] = np.clip(a[:, :, :3] * k, 0, 255)
    return Image.fromarray(a.astype("uint8"), "RGBA")


def build_tile(design):
    """Return the shirt-local design (u across the shirt, v shoulder -> hem)."""
    d = dict(DEFAULT_DESIGN)
    d.update(design)
    body = tuple(d["body"])
    second = tuple(d["second"])
    third = tuple(d["third"]) if d.get("third") else None

    spec = d.get("pattern") or "plain"
    if isinstance(spec, str) and spec and not spec[0].isdigit():
        spec = _LEGACY_PATTERN.get(spec, "graphic")

    if spec in ("plain", None) or spec == "graphic":
        canvas = Image.new("RGBA", (kit_gen.W * _SS, kit_gen.H * _SS), body + (255,))
    else:
        canvas = _pattern_canvas(spec, body, second, third,
                                 int(d.get("var", 0)), int(d.get("width_step", 0)))
    canvas = _apply_gradient(canvas, [0.0, 0.06, 0.10, 0.16][int(d.get("gradient", 0)) % 4])

    x0, y0, x1, y1 = _PREVIEW_SHIRT_BOX
    return canvas.crop((x0 * _SS, y0 * _SS, x1 * _SS, y1 * _SS))


def _paint_shirt(atlas, tile, masks):
    """Paste the tile into the front (upright) and back (180deg) islands."""
    boxes = kit_regions.region_boxes(masks)
    for name, rotate in (("body_front", False), ("body_back", True)):
        x0, y0, x1, y1 = boxes[name]
        w, h = x1 - x0 + 1, y1 - y0 + 1
        panel = (tile.rotate(180) if rotate else tile).resize((w, h), Image.LANCZOS)
        mask = _mask_img(masks[name][y0:y1 + 1, x0:x1 + 1])
        atlas.paste(panel, (x0, y0), mask)


def _band_mask(mask, side, frac):
    """A bool mask of the `side` edge band (top/bottom/left/right) of `mask`."""
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    out = np.zeros_like(mask)
    if side in ("top", "bottom"):
        lo, hi = int(ys.min()), int(ys.max())
        span = max(1, hi - lo)
        cut = lo + span * frac if side == "top" else hi - span * frac
        sel = ys <= cut if side == "top" else ys >= cut
    else:
        lo, hi = int(xs.min()), int(xs.max())
        span = max(1, hi - lo)
        cut = lo + span * frac if side == "left" else hi - span * frac
        sel = xs <= cut if side == "left" else xs >= cut
    out[ys[sel], xs[sel]] = True
    return out


def _fill_region(atlas, masks, region, colour, band=None, frac=0.0):
    if masks.get(region) is None:
        return
    if band is None:
        atlas.paste(Image.new("RGBA", (ATLAS, ATLAS), tuple(colour) + (255,)),
                    (0, 0), _mask_img(masks[region]))
        return
    m = _band_mask(masks[region], band, frac)
    if m is not None:
        atlas.paste(Image.new("RGBA", (ATLAS, ATLAS), tuple(colour) + (255,)),
                    (0, 0), _mask_img(m))


# --------------------------------------------------------------------------- #
#  main painter
# --------------------------------------------------------------------------- #
def render_kit(design, out_png, masks=None, sider=None, team_id=None):
    """Paint `design` into the UV atlas and save a 2048x2048 RGBA PNG."""
    masks = masks if masks is not None else kit_regions.build_masks()

    d = dict(DEFAULT_DESIGN)
    d.update(design)
    body = tuple(d["body"])
    sleeve = tuple(d["sleeve"] or d["body"])
    shorts = tuple(d["shorts"] or d["body"])
    socks = tuple(d["socks"] or d["body"])
    collar = tuple(d["collar"] or d["body"])
    cuff = tuple(d["cuff"] or d["accent"] or d["second"])

    # fill everything with the body colour first so mip bleeding at island edges
    # stays on-colour (the real kits are fully opaque too)
    atlas = Image.new("RGBA", (ATLAS, ATLAS), body + (255,))

    # shirt front + back
    _paint_shirt(atlas, build_tile(d), masks)

    # sleeves / shorts / socks / collar
    _fill_region(atlas, masks, "sleeve", sleeve)
    _fill_region(atlas, masks, "shorts", shorts)
    _fill_region(atlas, masks, "socks", socks)
    _fill_region(atlas, masks, "collar", collar)

    # modern accent trims: sleeve cuffs, shorts side stripes, sock tops
    _fill_region(atlas, masks, "sleeve", cuff, band="bottom", frac=0.16)
    _fill_region(atlas, masks, "shorts", cuff, band="left", frac=0.055)
    _fill_region(atlas, masks, "shorts", cuff, band="right", frac=0.055)
    _fill_region(atlas, masks, "socks", cuff, band="top", frac=0.15)

    # logos
    _stamp_sponsor(atlas, d, body)
    _stamp_badge(atlas, d, sider, team_id)
    _stamp_supplier(atlas, d, sider, team_id)

    atlas.save(out_png)
    return out_png


def _stamp_sponsor(atlas, d, body):
    path = d.get("sponsor")
    if not path:
        return
    _stamp_area(atlas, path, *kit_regions.CHEST_SPONSOR,
                white=d.get("sponsor_white"),
                plate=body if d.get("sponsor_plate") else None)


def _stamp_badge(atlas, d, sider, team_id):
    img = None
    if d.get("badge"):
        img = Image.open(d["badge"]).convert("RGBA")
    elif sider is not None and team_id is not None:
        import kit_source
        img = kit_source.load_emblem(sider, team_id)
    if img is None:
        return
    _stamp_image(atlas, img, *kit_regions.CLUB_BADGE, white=d.get("badge_white"))


def _stamp_supplier(atlas, d, sider, team_id):
    img = None
    if d.get("supplier"):
        img = Image.open(d["supplier"]).convert("RGBA")
    elif sider is not None and team_id is not None:
        import kit_source
        img = kit_source.supplier_mark(sider, team_id)
    if img is None:
        return
    _stamp_image(atlas, img, *kit_regions.KIT_SUPPLIER, white=d.get("supplier_white"))


def _stamp_image(atlas, img, cx, cy, area, white=False, fit=0.94):
    """Place an RGBA mark centred on (cx, cy) scaled to fit a box of `area` px^2."""
    if white:
        img = _flatten_white(img)
    w, h = img.size
    if w <= 0 or h <= 0:
        return
    side = (float(area) ** 0.5) * fit
    scale = min(side / w, side / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    atlas.alpha_composite(img, (int(cx - nw / 2), int(cy - nh / 2)))


def _stamp_area(atlas, logo_path, cx, cy, area, white=False, plate=None):
    """Place a sponsor logo centred on (cx, cy), scaled so w*h ~= area.

    `plate` optionally paints a flat rectangle behind the logo; by default the
    plate is None, so the logo keeps its own transparent background (no solid
    block is ever drawn behind a sponsor).
    """
    logo = Image.open(logo_path).convert("RGBA")
    if white:
        logo = _flatten_white(logo)
    w, h = logo.size
    scale = (float(area) / max(1, w * h)) ** 0.5
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    logo = logo.resize((nw, nh), Image.LANCZOS)
    x, y = int(cx - nw / 2), int(cy - nh / 2)

    if plate is not None:
        pad_x, pad_y = int(nw * 0.10), int(nh * 0.35)
        draw = ImageDraw.Draw(atlas)
        draw.rectangle([x - pad_x, y - pad_y, x + nw + pad_x, y + nh + pad_y],
                       fill=tuple(plate) + (255,))

    atlas.alpha_composite(logo, (x, y))


def _flatten_white(logo):
    """Return a white silhouette keeping the logo's alpha."""
    a = np.asarray(logo).copy()
    a[:, :, 0] = 255
    a[:, :, 1] = 255
    a[:, :, 2] = 255
    return Image.fromarray(a, "RGBA")


# --------------------------------------------------------------------------- #
#  generator -> design
# --------------------------------------------------------------------------- #
# Home / away / third ("p1/p2/p3") each get an independent, deterministic roll.
_SLOT_SALT = {1: "kitgen-v2", 2: "kitgen-v2-away", 3: "kitgen-v2-third"}

# Which of the club's three colours leads each kit: (body, pattern, accent).
_SLOT_ROLES = {
    1: ("primary", "secondary", "accent"),      # home   - club colour dominant
    2: ("secondary", "primary", "accent"),      # away   - the second colour leads
    3: ("accent", "primary", "secondary"),      # third  - the accent colour leads
}

# Explicit per-club styling, keyed by team id then slot.  It pins the palette per
# kit (so a club's real identity is honoured even before a deal) and gives a POOL
# of on-brand, non-basic patterns.  Pool entries are kit_patterns catalog codes
# ("127", "127-B", ...) - see generator/kit_patterns.py.
KIT_STYLE = {
    102: {  # Chelsea
        # home: mainly blue with a touch of white and a tiny red
        1: {
            "body": (0, 76, 165), "second": (245, 245, 245), "third": (200, 32, 48),
            "patterns": ["085", "093", "097", "105", "118", "124",
                         "005", "022", "087", "090", "095", "098", "103", "113", "119",
                         "067", "102", "114", "084", "091"],
            "sleeve": (245, 245, 245), "shorts": (0, 76, 165),
            "socks": (245, 245, 245), "collar": (200, 32, 48),
            "gradient": 0,
        },
        # away: black with a touch of blue, bold angular geometry
        2: {
            "body": (16, 17, 21), "second": (10, 63, 133), "third": (0, 76, 165),
            "patterns": ["001", "002", "003", "004", "008", "020", "021", "023", "029", "038",
                         "010", "011", "017", "024", "030", "039",
                         "013", "019", "027", "033", "037", "040"],
            "sleeve": (16, 17, 21), "shorts": (16, 17, 21),
            "socks": (10, 63, 133), "collar": (10, 63, 133),
            "gradient": 1,
        },
        # third: mainly white with a touch of blue and a tiny red
        3: {
            "body": (245, 246, 248), "second": (10, 63, 133), "third": (200, 32, 48),
            "patterns": ["041", "043", "044", "047", "052", "064", "074", "195", "204", "205",
                         "127", "131", "142", "153",
                         "168", "169", "170", "176", "177", "178", "179", "189", "197"],
            "sleeve": (10, 63, 133), "shorts": (245, 246, 248),
            "socks": (245, 246, 248), "collar": (200, 32, 48),
            "gradient": 0,
        },
    },
}


def _slot_colours(palette, slot):
    primary, secondary, accent = palette["primary"], palette["secondary"], palette["accent"]
    if slot == 1:
        return primary, secondary, accent, primary, primary, secondary, secondary
    if slot == 2:
        return secondary, primary, accent, secondary, secondary, primary, primary
    return accent, primary, secondary, accent, accent, accent, primary


def design_from_club(club, season, logo_dir, slot=1, brand_files=None, style=None,
                     sponsor=None):
    """Turn a club row + season + kit slot (1=home, 2=away, 3=third) into a design.

    Colours come from the club row (measured from the game's own kits); the
    pattern is a modern catalog code rolled deterministically from the club, the
    slot, the sponsor and the season.  `sponsor` is the signed brand name: when
    given it puts that brand's logo on the chest and re-rolls the pattern, so a
    deal is always a visible change.

    `style` (or KIT_STYLE[club_id][slot]) may override any of body/second/third/
    patterns/sleeve/shorts/socks/collar/gradient/var/width_step.
    """
    if slot not in _SLOT_ROLES:
        raise ValueError("slot must be 1 (home), 2 (away) or 3 (third)")

    palette = {"primary": _hexc(club["primary"]),
               "secondary": _hexc(club["secondary"]),
               "accent": _hexc(club["accent"])}

    st = KIT_STYLE.get(club["id"], {}).get(slot) if style is None else style
    st = dict(st) if st else {}
    pool = st.pop("patterns", None)

    body, second, accent, sleeve, shorts, socks, collar = _slot_colours(palette, slot)

    # a per-(club, slot) roll for the small styling choices (variant, width,
    # gradient) - no colour jitter, so the kit is always the club's real colour
    r = h32("style:%d:%d" % (int(club["id"]), slot))
    var = r % 3
    ws = (r // 3) % 8
    gradient = [0, 1, 2][(r // 7) % 3]

    design = {
        "body": body, "second": second, "accent": accent, "third": accent,
        "pattern": pattern_spec(club["id"], club.get("structure"), slot, season,
                                sponsor=sponsor, pool=pool),
        "var": var, "width_step": ws, "gradient": gradient,
        "sleeve": sleeve, "shorts": shorts, "socks": socks, "collar": collar,
        "cuff": accent,
        "sponsor": None, "sponsor_white": False,
        "slot": slot, "_brand": sponsor, "_sponsor": sponsor,
    }

    # style overrides win over everything above (per-club identity)
    for k in ("body", "second", "third", "accent", "sleeve", "shorts", "socks",
              "collar", "cuff", "gradient", "var", "width_step"):
        if k in st and st[k] is not None:
            design[k] = st[k]

    # the chest logo is stamped only for a signed sponsor, and chosen from the
    # FINAL body colour so a dark kit gets the white logo variant
    if sponsor:
        white = kit_gen.lum(design["body"]) < 120
        design["sponsor_white"] = white
        design["sponsor"] = str(Path(logo_dir) / (sponsor + ("_white" if white else "") + ".png"))
    return design


def render_club(club, season, out_png, logo_dir, masks=None, brand_files=None, slot=1,
                style=None, sponsor=None, sider=None):
    """Render one club's generated kit for a season; returns (path, design)."""
    design = design_from_club(club, season, logo_dir, slot=slot,
                              brand_files=brand_files, style=style, sponsor=sponsor)
    render_kit(design, str(out_png), masks=masks, sider=sider, team_id=club["id"])
    return str(out_png), design


def render_kit_set(club, season, outdir, logo_dir, masks=None, brand_files=None,
                   sponsor=None, sider=None, previews=True):
    """Render the full home/away/third set for a club + season.

    Returns a list of (slot, atlas_png, preview_png, design).
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    masks = masks if masks is not None else kit_regions.build_masks()
    if brand_files is None:
        logo_dir = Path(logo_dir)
        brand_files = sorted(p.stem for p in logo_dir.glob("*.png")
                             if not p.stem.endswith("_white")
                             and not p.stem.startswith("_"))

    team = club["id"]
    out = []
    for slot in (1, 2, 3):
        atlas = outdir / f"u{team:04d}p{slot}_atlas.png"
        _, design = render_club(club, season, atlas, logo_dir, masks=masks,
                                brand_files=brand_files, slot=slot, sponsor=sponsor,
                                sider=sider)
        preview = None
        if previews:
            preview = uv_contact_sheet(atlas, outdir / f"u{team:04d}p{slot}_preview.png",
                                       masks=masks)
        out.append((slot, str(atlas), str(preview) if preview else None, design))
    return out


def mockup(design, club_name, season, out_png):
    """A flat 'on the shirt' mockup of a design (front), for eyeballing."""
    W, H = kit_gen.W, kit_gen.H
    bmask, smask = kit_gen.shirt_masks()
    body = tuple(design["body"])
    second = tuple(design["second"])
    canvas = Image.new("RGBA", (W, H + 44), (28, 30, 36, 255))

    spec = design.get("pattern") or "plain"
    if spec in ("plain", "graphic"):
        pat = Image.new("RGBA", (W, H), body + (255,))
    else:
        pat = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        kit_patterns.draw(pat, spec, body, second,
                          third=tuple(design["third"]) if design.get("third") else None,
                          var=design.get("var", 0), ws=design.get("width_step", 0))
    pat = _apply_gradient(pat, [0.0, 0.06, 0.10, 0.16][design.get("gradient", 0) % 4])
    canvas.paste(pat, (0, 0), bmask)

    sleeve = tuple(design.get("sleeve") or body)
    canvas.paste(Image.new("RGBA", (W, H), sleeve + (255,)), (0, 0), smask)

    d = ImageDraw.Draw(canvas)
    trim = tuple(design.get("collar") or body)
    d.polygon([(200, 40), (240, 86), (280, 40), (268, 40), (240, 70), (212, 40)],
              fill=trim + (255,))

    if design.get("sponsor"):
        logo = Image.open(design["sponsor"]).convert("RGBA")
        if design.get("sponsor_white"):
            logo = _flatten_white(logo)
        s = min(190 / logo.width, 60 / logo.height)
        logo = logo.resize((max(1, int(logo.width * s)), max(1, int(logo.height * s))))
        canvas.alpha_composite(logo, (240 - logo.width // 2, 170))

    def swatch(x, colour, label):
        d.rectangle([x, H + 6, x + 44, H + 38], fill=tuple(colour) + (255,))
        d.text((x + 50, H + 14), label, fill=(210, 212, 218, 255))

    swatch(10, design.get("shorts") or body, "shorts")
    swatch(150, design.get("socks") or body, "socks")
    swatch(290, design.get("collar") or body, "collar")
    d.text((10, 10), f"{club_name} S{season} p{design.get('slot', 1)} {design['pattern']}",
           fill=(240, 240, 245, 255))

    canvas.convert("RGB").save(out_png)
    return out_png


def uv_contact_sheet(atlas_png, out_png, masks=None, cell=460):
    """Save a labelled contact sheet of the UV islands, for eyeballing."""
    masks = masks if masks is not None else kit_regions.build_masks()
    boxes = kit_regions.region_boxes(masks)
    atlas = Image.open(atlas_png).convert("RGB")

    order = ["body_front", "body_back", "sleeve", "shorts", "socks", "collar"]
    cols = 3
    rows = (len(order) + cols - 1) // cols
    pad, label_h = 16, 34
    sheet = Image.new("RGB", (cols * (cell + pad) + pad,
                              rows * (cell + label_h + pad) + pad), (28, 30, 36))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arialbd.ttf", 22)
    except Exception:  # pragma: no cover
        font = ImageFont.load_default()

    for i, name in enumerate(order):
        x0, y0, x1, y1 = boxes[name]
        crop = atlas.crop((x0, y0, x1 + 1, y1 + 1))
        crop.thumbnail((cell, cell), Image.LANCZOS)
        cx = pad + (i % cols) * (cell + pad)
        cy = pad + (i // cols) * (cell + label_h + pad)
        draw.text((cx, cy), name, fill=(230, 230, 235), font=font)
        sheet.paste(crop, (cx + (cell - crop.width) // 2, cy + label_h))

    sheet.save(out_png)
    return out_png


# --------------------------------------------------------------------------- #
#  demo / CLI
# --------------------------------------------------------------------------- #
def _describe(design, season, club):
    return (f"  {club['name']} S{season} p{design['slot']}: pattern={design['pattern']} "
            f"var={design['var']} body={design['body']} sleeve={design['sleeve']} "
            f"shorts={design['shorts']} socks={design['socks']} collar={design['collar']} "
            f"sponsor={design['_brand']}")


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent
    logo_dir = root / "content" / "KitGenerator" / "logos"
    sider = root.parent  # best-effort for the demo
    masks = kit_regions.build_masks()

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print("usage:")
        print("  python kit_render.py <club_id> <season> <outdir>            # full p1/p2/p3 set")
        print("  python kit_render.py <club_id> <season> <outdir> --slot N   # just one slot")
        print("  python kit_render.py <club_id> <season> <outdir> --sider <SiderAddons>")
        sys.exit(1)

    club_id, season, outdir = int(args[0]), int(args[1]), Path(args[2])
    if "--sider" in sys.argv:
        sider = Path(sys.argv[sys.argv.index("--sider") + 1])
    outdir.mkdir(parents=True, exist_ok=True)

    clubs = kit_gen.load_clubs(root / "generator" / "clubs.csv")
    club = next((c for c in clubs if c["id"] == club_id), None)
    if club is None:
        import club_resolver
        club = club_resolver.resolve(club_id, sider)

    if "--slot" in sys.argv:
        slot = int(sys.argv[sys.argv.index("--slot") + 1])
        name = "u%04dp%d" % (club_id, slot)
        out = outdir / f"{name}_atlas.png"
        _, design = render_club(club, season, out, logo_dir, masks=masks, slot=slot, sider=sider)
        sheet = uv_contact_sheet(out, outdir / f"{name}_preview.png", masks=masks)
        print(f"wrote {out}")
        print(f"wrote {sheet}")
        print(_describe(design, season, club))
        sys.exit(0)

    for slot, atlas, preview, design in render_kit_set(club, season, outdir,
                                                       logo_dir, masks=masks, sider=sider):
        print(f"wrote {atlas}")
        if preview:
            print(f"wrote {preview}")
        print(_describe(design, season, club))
