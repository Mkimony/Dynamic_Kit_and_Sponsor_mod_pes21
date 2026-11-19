#!/usr/bin/env python3
r"""build_kit.py - render a club's home/away/third kits and pack them to .ftex.

Pipeline
--------
    club + season + slot  ->  kit_render.render_kit_set   (2048x2048 atlas PNGs)
                          ->  ftex_io.image_to_ftex        (BC3/DXT5, 12 mips)
                          ->  u<team>p<slot>.ftex
                          ->  (optional) content\kits\_KitGenerator\ store

The .ftex header (version / texture_type / hashes / size / format) is inherited
from a real PES kit ("base"), so the output is byte-for-byte the same *kind* of
file the game already loads.  Any real kit works as the base: the three Chelsea
slots share an identical 64-byte header.

Where the kits are installed
----------------------------
NOT under the original file names in livecpk (that would override the kit in
every game mode).  They go to a private store,

    <sider>\content\kits\_KitGenerator\u<team>p<slot>.ftex

and KitGenerator.lua hands Sider that path through the ``livecpk_make_key`` /
``livecpk_get_filepath`` hooks, but ONLY while a career (Master League) is
loaded.  In exhibition and every other mode the game's own kits load untouched.

Usage
-----
    python build_kit.py <club_id> <season> <outdir> [--slots 1,2,3] [--install]
    python build_kit.py <club_id> <season> <outdir> --install --sider "C:\\...\\SiderAddons"

Notes
-----
* Installing backs up any file it replaces with a timestamped .bak next to it.
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

import ftex_io
import kit_gen
import kit_render

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DEFAULT_GAME_ROOT = Path(r"C:\Program Files (x86)\SP Football Life 2027")
# Any real PES kit works as a header template; the three Chelsea slots share one.
DEFAULT_BASE = Path(
    r"C:\Program Files (x86)\SP Football Life 2027\SiderAddons\content\kits"
    r"\TheGaffer_Chelsea_FC\p1\u0102p1.ftex")

INSTALL_SUBDIR = Path("content/kits/_KitGenerator")


def _load_club(club_id, sider=None):
    """A club row for `club_id`: the curated clubs.csv row if there is one, else
    a row resolved from the game's own kit data (see club_resolver.py)."""
    clubs = kit_gen.load_clubs(ROOT / "generator" / "clubs.csv")
    for c in clubs:
        if c["id"] == club_id:
            return c
    import club_resolver
    return club_resolver.resolve(club_id, sider)


def pack_atlas(atlas_png, out_ftex, template):
    """Encode a 2048x2048 atlas PNG into a .ftex using `template`'s header."""
    img = Image.open(atlas_png).convert("RGBA").resize(
        (template["width"], template["height"]), Image.LANCZOS)
    data = ftex_io.image_to_ftex(img, fmt=template["pixel_format"], template=template)
    ftex_io.write_ftex(out_ftex, data)
    return out_ftex


def build_set(club, season, outdir, base_ftex=DEFAULT_BASE, slots=(1, 2, 3),
              install=False, game_root=DEFAULT_GAME_ROOT, quiet=False, sponsor=None,
              logo_dir=None, sider=None):
    """Render + pack a club's kit set.  Returns [(slot, atlas, ftex, design)].

    When `sponsor` is given (a brand name from content/KitGenerator/logos), the
    kits are regenerated with that sponsor's logo on the chest and a fresh design
    drawn from the club's on-brand pattern pool.

    `logo_dir` overrides where the sponsor logos live (defaults to this mod's
    own content/KitGenerator/logos).  A deployed watcher points it at the game's
    SiderAddons/content/KitGenerator/logos instead.

    `sider` is the game's SiderAddons folder: it is where the club badge and the
    kit-supplier mark are sourced from, and (with `install`) where the generated
    .ftex set is written.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if sider is None:
        sider = Path(game_root) / "SiderAddons"
    sider = Path(sider)

    base_ftex = Path(base_ftex)
    if not base_ftex.is_file():
        raise SystemExit(f"base .ftex not found: {base_ftex}")
    template = ftex_io.read_ftex(base_ftex)

    if logo_dir is None:
        logo_dir = ROOT / "content" / "KitGenerator" / "logos"
    logo_dir = Path(logo_dir)
    masks = kit_regions_build()

    rendered = kit_render.render_kit_set(club, season, outdir, logo_dir,
                                         masks=masks, sponsor=sponsor, sider=sider)
    install_dir = (sider / INSTALL_SUBDIR) if install else None
    if install_dir:
        install_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for slot, atlas, _preview, design in rendered:
        if slot not in slots:
            continue
        name = f"u{club['id']:04d}p{slot}"
        out_ftex = outdir / f"{name}.ftex"
        pack_atlas(atlas, out_ftex, template)
        if not quiet:
            print(f"packed {out_ftex.name}  ({out_ftex.stat().st_size} bytes)  "
                  f"<- {Path(atlas).name}")

        if install_dir:
            dst = install_dir / f"{name}.ftex"
            if dst.exists():
                bak = dst.with_name(dst.name + f".bak-{stamp}")
                shutil.copy2(dst, bak)
                if not quiet:
                    print(f"  backup {bak.name}")
            shutil.copy2(out_ftex, dst)
            if not quiet:
                print(f"  installed -> {dst}")
        results.append((slot, str(atlas), str(out_ftex), design))
    return results


def kit_regions_build():
    import kit_regions
    return kit_regions.build_masks()


def _parse_slots(text):
    slots = tuple(int(x) for x in text.replace(" ", "").split(",") if x)
    bad = [s for s in slots if s not in (1, 2, 3)]
    if bad:
        raise SystemExit(f"slots must be 1,2,3 (got {bad})")
    return slots


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("club_id", type=int)
    p.add_argument("season", type=int)
    p.add_argument("outdir")
    p.add_argument("--slots", default="1,2,3",
                   help="which kits to build, e.g. 1 or 1,2,3 (default 1,2,3)")
    p.add_argument("--install", action="store_true",
                   help="copy the .ftex files into the game's "
                        "content\\kits\\_KitGenerator store (served only in ML)")
    p.add_argument("--base", default=str(DEFAULT_BASE),
                   help="a real .ftex used as the header template")
    p.add_argument("--game-root", default=str(DEFAULT_GAME_ROOT),
                   help="game install root (for --install)")
    p.add_argument("--sponsor", default=None,
                   help="signed sponsor brand (logo file stem, e.g. dovrin); "
                        "regenerates the kits with that sponsor and a fresh design")
    p.add_argument("--logos", default=None,
                   help="folder holding the sponsor logo PNGs (default: this mod's "
                        "content/KitGenerator/logos)")
    p.add_argument("--sider", default=None,
                   help="the game's SiderAddons folder, used to resolve a club that "
                        "is not in clubs.csv (default: <game-root>\\SiderAddons)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    sider = args.sider or (Path(args.game_root) / "SiderAddons")
    club = _load_club(args.club_id, sider)
    results = build_set(club, args.season, args.outdir,
                        base_ftex=args.base, slots=_parse_slots(args.slots),
                        install=args.install, game_root=args.game_root,
                        quiet=args.quiet, sponsor=args.sponsor,
                        logo_dir=args.logos, sider=sider)

    print()
    for slot, atlas, ftex, d in results:
        label = {1: "home", 2: "away", 3: "third"}[slot]
        print(f"{label:5s} p{slot}: {Path(ftex).name}  pattern={d['pattern']} "
              f"body={d['body']} sleeve={d['sleeve']} shorts={d['shorts']} "
              f"socks={d['socks']} collar={d['collar']} sponsor={d['_brand']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
