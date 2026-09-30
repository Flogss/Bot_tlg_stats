"""Génération de l'image de la cagnotte (podium + classement)."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH = 1080
SS = 3  # facteur de supersampling pour des cercles bien lisses

GOLD = (255, 204, 77)
SILVER = (205, 214, 230)
BRONZE = (222, 148, 92)
WHITE = (255, 255, 255)
MUTED = (168, 160, 200)
MEDAL_COLORS = [GOLD, SILVER, BRONZE]

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
BOLD_CANDIDATES = [
    os.path.join(FONT_DIR, "bold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
BLACK_CANDIDATES = [
    os.path.join(FONT_DIR, "black.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
] + BOLD_CANDIDATES


@dataclass
class Entry:
    name: str
    amount: float
    avatar: bytes | None = None


def _font(size: int, candidates: list[str] = BOLD_CANDIDATES) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def format_eur(amount: float) -> str:
    txt = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
    if txt.endswith(",00"):
        txt = txt[:-3]
    return f"{txt} €"


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    base = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((w, h), Image.BICUBIC)


def _glow(img: Image.Image, center: tuple[int, int], radius: int, color: tuple, alpha: int) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = center
    d.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(layer, (0, 0), layer)


def _initials_avatar(name: str, size: int, color: tuple) -> Image.Image:
    img = _gradient(size, size, color, tuple(max(0, c - 90) for c in color)).convert("RGBA")
    d = ImageDraw.Draw(img, "RGBA")
    letters = "".join(p[0] for p in name.split()[:2] if p).upper() or "?"
    f = _font(int(size * 0.4))
    d.text((size / 2, size / 2), letters, font=f, fill=WHITE, anchor="mm")
    return img


def _circle_avatar(entry: Entry, size: int, ring: tuple, ring_w: int) -> Image.Image:
    """Avatar rond avec anneau coloré, anti-aliasé."""
    big = size * SS
    src = None
    if entry.avatar:
        try:
            src = Image.open(io.BytesIO(entry.avatar)).convert("RGBA")
        except Exception:
            src = None
    if src is None:
        src = _initials_avatar(entry.name, big, (124, 92, 255))
    # recadrage carré centré
    s = min(src.size)
    left, top = (src.width - s) // 2, (src.height - s) // 2
    src = src.crop((left, top, left + s, top + s)).resize((big, big), Image.LANCZOS)

    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ring_mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(ring_mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    out.paste(Image.new("RGBA", (big, big), ring + (255,)), (0, 0), ring_mask)

    inset = ring_w * SS
    inner = big - 2 * inset
    photo = src.resize((inner, inner), Image.LANCZOS)
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
    out.paste(photo, (inset, inset), mask)
    return out.resize((size, size), Image.LANCZOS)


def _shadow(img: Image.Image, box: tuple, radius: int, blur: int = 18, alpha: int = 110) -> None:
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, fill=(0, 0, 0, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.paste(layer, (0, 0), layer)


def _crown(draw: ImageDraw.ImageDraw, cx: int, bottom: int, w: int) -> None:
    h = int(w * 0.62)
    x0, x1 = cx - w // 2, cx + w // 2
    top = bottom - h
    pts = [
        (x0, bottom), (x0, top + h * 0.28), (x0 + w * 0.25, top + h * 0.62),
        (cx, top), (x1 - w * 0.25, top + h * 0.62), (x1, top + h * 0.28), (x1, bottom),
    ]
    draw.polygon(pts, fill=GOLD)
    r = max(3, w // 14)
    for px, py in [(x0, top + h * 0.28), (cx, top), (x1, top + h * 0.28)]:
        draw.ellipse((px - r, py - r, px + r, py + r), fill=GOLD)
    draw.rectangle((x0, bottom - h * 0.16, x1, bottom), fill=(232, 170, 40))


def render(entries: list[Entry], total: float, title: str = "CAGNOTTE") -> bytes:
    entries = sorted(entries, key=lambda e: e.amount, reverse=True)
    podium = entries[:3]
    rest = entries[3:10]

    header_h = 360
    podium_h = 620 if podium else 160
    row_h = 104
    rest_h = (len(rest) * row_h + 40) if rest else 0
    footer_h = 90
    height = header_h + podium_h + rest_h + footer_h

    img = _gradient(WIDTH, height, (30, 16, 64), (10, 10, 28))
    _glow(img, (160, 120), 260, (140, 82, 255), 120)
    _glow(img, (WIDTH - 120, 420), 280, (255, 90, 170), 70)
    _glow(img, (WIDTH // 2, header_h + 200), 320, (255, 200, 80), 45)
    d = ImageDraw.Draw(img, "RGBA")

    # --- En-tête ---
    f_title = _font(44)
    spaced = "  ".join(title.upper())
    d.text((WIDTH / 2, 88), spaced, font=f_title, fill=MUTED, anchor="mm")
    f_total = _font(132, BLACK_CANDIDATES)
    total_txt = format_eur(total)
    while d.textlength(total_txt, font=f_total) > WIDTH - 120 and f_total.size > 60:
        f_total = _font(f_total.size - 6, BLACK_CANDIDATES)
    # léger halo doré derrière le montant
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).text((WIDTH / 2, 205), total_txt, font=f_total, fill=GOLD + (150,), anchor="mm")
    halo = halo.filter(ImageFilter.GaussianBlur(22))
    img.paste(halo, (0, 0), halo)
    d = ImageDraw.Draw(img, "RGBA")
    d.text((WIDTH / 2, 205), total_txt, font=f_total, fill=GOLD, anchor="mm")
    n = len(entries)
    sub = f"{n} participant{'s' if n > 1 else ''}"
    f_sub = _font(34)
    sw = d.textlength(sub, font=f_sub) + 60
    d.rounded_rectangle((WIDTH / 2 - sw / 2, 285, WIDTH / 2 + sw / 2, 339), radius=27,
                        fill=(255, 255, 255, 28), outline=(255, 255, 255, 60), width=2)
    d.text((WIDTH / 2, 312), sub, font=f_sub, fill=WHITE, anchor="mm")

    # --- Podium ---
    y0 = header_h
    if not podium:
        d.text((WIDTH / 2, y0 + 70), "Aucune contribution pour l'instant", font=_font(38), fill=MUTED, anchor="mm")
    else:
        # ordre visuel : 2e - 1er - 3e
        slots = {0: (WIDTH // 2, 240, 0), 1: (WIDTH // 2 - 330, 190, 70), 2: (WIDTH // 2 + 330, 170, 95)}
        block_heights = {0: 190, 1: 140, 2: 110}
        base_y = y0 + podium_h - 20
        for rank, entry in enumerate(podium):
            cx, size, offset = slots[rank]
            color = MEDAL_COLORS[rank]
            # marche du podium
            bw = 280
            bh = block_heights[rank]
            box = (cx - bw // 2, base_y - bh, cx + bw // 2, base_y)
            _shadow(img, box, 26)
            d = ImageDraw.Draw(img, "RGBA")
            step = _gradient(bw, bh, tuple(min(255, c + 10) for c in color), tuple(int(c * 0.55) for c in color)).convert("RGBA")
            m = Image.new("L", (bw * SS, bh * SS), 0)
            ImageDraw.Draw(m).rounded_rectangle((0, 0, bw * SS - 1, bh * SS + 60 * SS), radius=26 * SS, fill=255)
            img.paste(step, (box[0], box[1]), m.resize((bw, bh), Image.LANCZOS))
            d = ImageDraw.Draw(img, "RGBA")
            d.text((cx, box[1] + bh / 2), str(rank + 1), font=_font(84 if rank == 0 else 70, BLACK_CANDIDATES),
                   fill=(255, 255, 255, 235), anchor="mm")

            # montant + nom au-dessus de la marche
            f_amt = _font(44 if rank == 0 else 38)
            d.text((cx, box[1] - 34), format_eur(entry.amount), font=f_amt, fill=color, anchor="mm")
            f_name = _font(34 if rank == 0 else 30)
            d.text((cx, box[1] - 82), _fit(d, entry.name, f_name, 300), font=f_name, fill=WHITE, anchor="mm")

            # avatar
            av_bottom = box[1] - 118
            av_top = av_bottom - size
            _glow(img, (cx, av_top + size // 2), size // 2 + 30, color, 90)
            av = _circle_avatar(entry, size, color, 8 if rank == 0 else 6)
            img.paste(av, (cx - size // 2, av_top), av)
            d = ImageDraw.Draw(img, "RGBA")
            if rank == 0:
                _crown(d, cx, av_top + 6, 96)

    # --- Suite du classement ---
    if rest:
        y = header_h + podium_h + 20
        top_amount = entries[0].amount or 1
        f_rank = _font(34, BLACK_CANDIDATES)
        f_name = _font(34)
        f_amt = _font(34)
        for i, entry in enumerate(rest, start=4):
            box = (60, y, WIDTH - 60, y + row_h - 18)
            d.rounded_rectangle(box, radius=24, fill=(255, 255, 255, 20), outline=(255, 255, 255, 38), width=2)
            cy = (box[1] + box[3]) // 2
            d.text((112, cy), f"{i}", font=f_rank, fill=MUTED, anchor="mm")
            av = _circle_avatar(entry, 62, (124, 92, 255), 3)
            img.paste(av, (160, cy - 31), av)
            d = ImageDraw.Draw(img, "RGBA")
            amt = format_eur(entry.amount)
            amt_w = d.textlength(amt, font=f_amt)
            d.text((WIDTH - 96, cy - 10), amt, font=f_amt, fill=WHITE, anchor="rm")
            d.text((244, cy - 10), _fit(d, entry.name, f_name, int(WIDTH - 96 - amt_w - 290)), font=f_name, fill=WHITE, anchor="lm")
            # barre de progression relative au premier
            bar_x0, bar_x1, bar_y = 244, WIDTH - 96, cy + 22
            d.rounded_rectangle((bar_x0, bar_y, bar_x1, bar_y + 8), radius=4, fill=(255, 255, 255, 30))
            ratio = max(0.02, min(1.0, entry.amount / top_amount)) if top_amount > 0 else 0.02
            d.rounded_rectangle((bar_x0, bar_y, bar_x0 + (bar_x1 - bar_x0) * ratio, bar_y + 8), radius=4, fill=(150, 110, 255))
            y += row_h

    # --- Pied de page ---
    extra = len(entries) - 10
    footer = f"+ {extra} autre{'s' if extra > 1 else ''} participant{'s' if extra > 1 else ''}" if extra > 0 else "/top <montant> pour participer"
    d.text((WIDTH / 2, height - footer_h / 2), footer, font=_font(28), fill=MUTED, anchor="mm")

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
