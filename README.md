# Dynamic Kit & Sponsor Mod for PES 2021 / Football Life 2027

A **Master League kit & sponsorship mod**. In the pre-season a *Sponsor Office* offers
sponsorship deals for the club you manage; signing one **rebuilds your club's
home / away / third kits** with the sponsor's logo and a fresh design, and **credits the
deal's money** into your in-game transfer and salary budgets.

The club you manage is **detected automatically**, so a career can be started with *any*
team.

```
Sponsor Office  ──sign──►  kit_request.txt  ──►  Python watcher  ──►  new .ftex kits
      │                                                                     │
      └──────────── budget credit (transfer + salary) ◄─────────  content\kits\_KitGenerator
                                                                    (served in Master League only)
```

The generated kits are installed into a private store and handed to Sider through
the `livecpk_make_key` / `livecpk_get_filepath` hooks **only while a Master League
career is loaded**, so exhibition, edit mode and every other mode keep the game's
own kits untouched.

---

## Features

- **Sponsor Office** — a clean, executive dark-mode Sider Mod Menu page (with a classic
  text-overlay fallback): this season's offers, a dashboard card for the active deal,
  financial summary and a contract progress bar.
- **Auto-detected club** — no hard-coded team id. Manual override, saved club, career
  learner and edit-mode fallback, most authoritative first.
- **Real kit colours** — each club's colours are **measured off its own kit texture**
  (`measure_clubs.py` → `clubs.csv`), with hand-checked palettes for the big clubs whose
  kits live inside a CPK.
- **140 sponsor brands** with colour + white logos; offers are synthesised for *any* club.
- **Multi-year deals**, signing bonuses, a termination clause, and a per-season payment
  ledger so money is never paid twice.
- **Two independent halves** — use it as a kit mod, a sponsorship mod, or both.

## Repository layout

```
modules/                  Sider Lua modules  -> SiderAddons/modules/
  KitGenerator.lua          the mod (Sponsor Office + auto-detect + budget)
  KGProbe.lua               read-only diagnostic probe
content/                  Sider content      -> SiderAddons/content/
  KitGenerator/
    deals.txt               curated offers (7 clubs, 2022-2045)
    brands.txt              the brand pool used to synthesise offers
    career.txt              stopgap career id
    logos/                  280 sponsor logos (colour + white)
generator/                Python kit generator + background watcher
  build_kit.py              club + season -> home/away/third .ftex
  club_resolver.py          team id -> club row (name, colours, structure, tier)
  kit_render.py             generated design -> 2048x2048 UV atlas PNG (+ badge/supplier/sponsor)
  kit_source.py             club crest (emblem cache) + kit-supplier mark extraction
  measure_clubs.py          rebuilds clubs.csv from the clubs' own kit textures
  ftex_io.py                real FL27 .ftex read/write (BC3/DXT5, 12 mips, zlib)
  watch_signed.py           background watcher: a signed deal rebuilds the kits
  setup_watcher.py          one-time deploy into the game
  deals.py, kit_gen.py, kit_regions.py, kit_patterns.py, logomaker.py, ...
  clubs.csv                 MEASURED team colours (team_id,name,primary,secondary,accent,tier,src,structure)
  uv_masks/                 PES Master Kit Creator UV masks
content/kits/_KitGenerator/ private store the generated .ftex set is installed into
                            (served in Master League only, via the livecpk hooks)
tests/                    offline test harnesses (lupa)
docs/                     install notes and the original readme
```

## Install (short version)

1. Copy `modules/` and `content/` into your `SiderAddons` folder.
2. Run `python generator/setup_watcher.py` once — it deploys the generator into the game,
   writes `content/KitGenerator/watcher.cmd` and `brands.txt`, adds
   `cpk.root = ".\content\kits"` to `sider.ini`, and removes any old static kit override.
3. In `sider.ini` make sure `luajit.ext.enabled = 1` and add
   `lua.module = "KitGenerator.lua"` (the Mod Menu, if used, must come first).
4. Start a Master League with any team. Press **F8** to switch the office on, then open the
   **Sponsor Office** (Mod Menu **F1**, or **F7** for the classic overlay).

Full, step-by-step instructions (including a safe first-run test session) are in
[`docs/INSTALL.md`](docs/INSTALL.md).

## Configuration

In `modules/KitGenerator.lua`:

```lua
CFG.ENABLE_KITS        = true   -- rebuild kits through the Python watcher
CFG.ENABLE_SPONSORSHIP = true   -- offers, signing, budget money
CFG.MANAGED_CLUB       = nil    -- manual team id override (nil = auto-detect)
```

`generator/clubs.csv` is **measured** from the clubs' own kit textures. To refresh it run
`python generator/measure_clubs.py`; to override a prominent club whose kit lives inside a
CPK, add it to `CURATED` in `generator/club_resolver.py`.

## Tests

```
pip install lupa pillow
python tests/parity_test.py     # Lua <-> Python roll parity
python tests/smoke_test.py      # init, auto-detect, offers, sign, counter
python tests/resolver_test.py   # team id -> club row
python tests/ftex_test.py       # .ftex round-trip
```

## Notes

- The mod never edits the game's own files, your save, or your Mod Studio project. It only
  writes inside `content/KitGenerator` and `kgprobe.log`.
- Back up your save folder and Sider folder before installing.

## License

MIT — see [`LICENSE`](LICENSE).
