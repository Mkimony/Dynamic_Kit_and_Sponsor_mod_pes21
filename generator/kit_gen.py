"""Kit design engine + preview renderer. UNTESTED IN GAME.
Writes PREVIEW images only. Real .ftex output is locked until a real FL27 kit
header has been read (see ftex_io.py)."""
import csv, sys, math
from pathlib import Path
from PIL import Image, ImageDraw
from kg_core import season_rng, pick_design, jitter_body, oklab

W, H = 480, 500

def hexc(h):
    h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def lum(rgb):
    return 0.2126*rgb[0] + 0.7152*rgb[1] + 0.0722*rgb[2]

# ---- pattern engine: draws on a plain RGBA canvas (reusable on the real UV body region)
# NOTE: the first 8 are the original seeded set (their order matters - the Lua
# mod indexes them with layout % 8).  Extra "styled" patterns are appended so
# a hand-picked design can be non-basic without disturbing the seeded roll.
PATTERNS = ['plain', 'vstripes', 'hoops', 'sash', 'halves', 'chestband', 'pinstripe', 'checks',
            'chevron', 'diagonal', 'fadegeo', 'blocks']

def draw_pattern(img, kind, body, second, var, width_step, third=None):
    # A catalog spec ("127", "127-B", ...) is handled by kit_patterns, which owns
    # the 205-archetype "Modern Football Kit Pattern" taxonomy.
    if isinstance(kind, str) and kind and kind[0].isdigit():
        try:
            import kit_patterns
            if kit_patterns.is_code(kind):
                return kit_patterns.draw(img, kind, body, second, third=third,
                                         var=var, ws=width_step)
        except ImportError:
            pass
    d = ImageDraw.Draw(img); w, h = img.size
    d.rectangle([0, 0, w, h], fill=body + (255,))
    sw = int(w * (0.05 + 0.012 * width_step))      # stripe width grows with the variant
    if kind == 'vstripes':
        for x in range(0, w, sw * 2): d.rectangle([x, 0, x + sw, h], fill=second + (255,))
    elif kind == 'hoops':
        for y in range(0, h, sw * 2): d.rectangle([0, y, w, y + sw], fill=second + (255,))
    elif kind == 'sash':
        pts = [(0, h * .15), (w * .30, 0), (w, h * .70), (w * .70, h)] if var % 2 else \
              [(w, h * .15), (w * .70, 0), (0, h * .70), (w * .30, h)]
        d.polygon(pts, fill=second + (255,))
    elif kind == 'halves':
        d.rectangle([w // 2, 0, w, h], fill=second + (255,)) if var % 2 else d.rectangle([0, 0, w // 2, h], fill=second + (255,))
    elif kind == 'chestband':
        y0 = int(h * (0.28 + 0.04 * (var % 3))); d.rectangle([0, y0, w, y0 + int(h * .14)], fill=second + (255,))
    elif kind == 'pinstripe':
        for x in range(8, w, 28): d.rectangle([x, 0, x + 3, h], fill=second + (255,))
    elif kind == 'checks':
        c = 44
        for yy in range(0, h, c):
            for xx in range(0, w, c):
                if ((xx // c) + (yy // c)) % 2: d.rectangle([xx, yy, xx + c, yy + c], fill=second + (255,))
    # ---- styled (non-basic) patterns ------------------------------------- #
    elif kind == 'chevron':
        # bold down-pointing V bands across the shirt
        thick = max(8, int(h * (0.055 + 0.010 * width_step)))
        step = max(26, int(h * 0.22))
        for y in range(-step, h + step, step * 2):
            d.polygon([(0, y), (w // 2, y + step), (w, y),
                       (w, y + thick), (w // 2, y + step + thick), (0, y + thick)],
                      fill=second + (255,))
            if third:
                d.line([(0, y + thick), (w // 2, y + step + thick), (w, y + thick)],
                       fill=third + (255,), width=max(2, thick // 5))
    elif kind == 'diagonal':
        # bold slanted stripes
        sw2 = max(12, int(w * (0.055 + 0.012 * width_step)))
        for x in range(-h, w + h, sw2 * 2):
            d.polygon([(x, 0), (x + sw2, 0), (x + sw2 - h, h), (x - h, h)],
                      fill=second + (255,))
            if third:
                d.line([(x + sw2, 0), (x + sw2 - h, h)], fill=third + (255,),
                       width=max(2, sw2 // 6))
    elif kind == 'fadegeo':
        # option-b look: mostly the body colour, a soft fade into `second` over
        # the lower part, plus an angular accent and a hairline third colour -
        # deliberately not a basic stripe/hoop
        for y in range(h):
            k = max(0.0, (y / max(1, h - 1) - 0.55) / 0.45)
            c = tuple(int(body[i] * (1 - k) + second[i] * k) for i in range(3))
            d.rectangle([0, y, w, y + 1], fill=c + (255,))
        y0 = int(h * 0.70)
        d.polygon([(0, y0 + int(h * 0.14)), (w, y0), (w, h), (0, h)],
                  fill=second + (255,))
        if third:
            d.line([(0, y0 + int(h * 0.14)), (w, y0)], fill=third + (255,),
                   width=max(3, h // 90))
    elif kind == 'blocks':
        # bold angular colour blocks (option "a" family)
        c = max(40, int(w * 0.24))
        for yy in range(-c, h + c, c * 2):
            for xx in range(-c, w + c, c * 2):
                d.polygon([(xx, yy), (xx + c, yy), (xx + c, yy + c),
                           (xx, yy + c)], fill=second + (255,))
        if third:
            d.rectangle([0, int(h * 0.62), w, int(h * 0.62) + max(3, h // 110)],
                        fill=third + (255,))
    return img


def apply_gradient(img, strength):
    if strength <= 0: return img
    w, h = img.size; px = img.load()
    for y in range(h):
        k = 1 - strength * (y / h)
        for x in range(w):
            r, g, b, a = px[x, y]; px[x, y] = (int(r*k), int(g*k), int(b*k), a)
    return img

def render_body(design, body, second):
    kind = PATTERNS[design['layout'] % 8]; var = design['layout'] // 8
    img = Image.new('RGBA', (W, H))
    draw_pattern(img, kind, body, second, var, design['stripe_width'])
    return apply_gradient(img, [0, .15, .28, .40][design['gradient']])

def shirt_masks():
    body = Image.new('L', (W, H), 0); sl = Image.new('L', (W, H), 0)
    ImageDraw.Draw(body).polygon([(140,40),(200,40),(240,86),(280,40),(340,40),(372,70),(372,470),(108,470),(108,70)], fill=255)
    d = ImageDraw.Draw(sl)
    d.polygon([(108,70),(140,40),(70,130),(24,250),(88,282),(108,200)], fill=255)
    d.polygon([(372,70),(340,40),(410,130),(456,250),(392,282),(372,200)], fill=255)
    return body, sl

def preview(club, season, logo_dir, brand_files):
    rng = season_rng(club['id'], season)
    design = pick_design(rng)
    anchor, second, accent = hexc(club['primary']), hexc(club['secondary']), hexc(club['accent'])
    body = jitter_body(rng, anchor)
    canvas = Image.new('RGBA', (W, H + 40), (34, 36, 42, 255))
    bmask, smask = shirt_masks()
    bodyimg = render_body(design, body, second)
    canvas.paste(bodyimg, (0, 0), bmask)
    sleeve_col = [body, second, accent, anchor][design['overlay'] % 4]
    canvas.paste(Image.new('RGBA', (W, H), sleeve_col + (255,)), (0, 0), smask)
    d = ImageDraw.Draw(canvas)
    trim = [second, accent, (20, 20, 24), (245, 245, 245), body, accent][design['trim']]
    d.polygon([(200,40),(240,86),(280,40),(268,40),(240,70),(212,40)], fill=trim + (255,))
    brand = brand_files[rng.randrange(len(brand_files))]
    # reserved logo zone: stripes and overlays stay out, so the plate is flat body colour
    d.rounded_rectangle([118, 160, 362, 236], radius=10, fill=body + (255,))
    logo = Image.open(Path(logo_dir) / (brand + ('_white' if lum(body) < 120 else '') + '.png')).convert('RGBA')
    s = min(190 / logo.width, 60 / logo.height); logo = logo.resize((int(logo.width*s), int(logo.height*s)))
    canvas.alpha_composite(logo, (240 - logo.width // 2, 170))
    d.text((10, H + 12), f"{club['name']}  S{season}  {PATTERNS[design['layout']%8]}", fill=(220, 220, 225, 255))
    return canvas, design, brand

def load_clubs(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        return [dict(r, id=int(r['team_id'])) for r in csv.DictReader(f)]

if __name__ == '__main__':
    root = Path(__file__).resolve().parent.parent
    clubs = load_clubs(root / 'generator' / 'clubs.csv')
    logo_dir = root / 'content' / 'KitGenerator' / 'logos'
    brands = sorted({p.stem for p in logo_dir.glob('*.png')
                     if not p.stem.endswith('_white') and not p.stem.startswith('_')})
    seasons = [int(a) for a in sys.argv[1:]] or [2027, 2028, 2029]
    sheet = Image.new('RGB', (len(seasons) * W, len(clubs) * (H + 40)))
    for r, c in enumerate(clubs):
        for k, s in enumerate(seasons):
            im, design, brand = preview(c, s, logo_dir, brands)
            sheet.paste(im.convert('RGB'), (k * W, r * (H + 40)))
    out = root / 'content' / 'KitGenerator' / 'staging' / 'kit_preview_sheet.png'
    sheet.save(out); print('wrote', out)
