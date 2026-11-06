"""Gerador visual de Pins 1000x1500 (2:3) com Pillow: 5 templates bem diferentes + paletas por nicho."""
from __future__ import annotations

import hashlib
import io
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from .config import FONTS_DIR, OUTPUT_DIR, PIN_HEIGHT as H, PIN_WIDTH as W, ROOT
from .ingestion import Product
from .logger import get_logger
from .pin_designer import design_path, render_design
from .seo_copywriter import PinCopy

log = get_logger("image")

FONT_FILES = {
    "display": "Anton-Regular.ttf",
    "bold": "Poppins-Bold.ttf",
    "semibold": "Poppins-SemiBold.ttf",
    "regular": "Poppins-Regular.ttf",
    "serif": "DMSerifDisplay-Regular.ttf",
    "hand": "PatrickHand-Regular.ttf",
    "condensed": "BebasNeue-Regular.ttf",
}

# Paletas: educação (vibrantes, amigáveis) x produtividade/estética (neutras, sofisticadas)
PALETTES = {
    "education": [
        {"name": "sunny", "bg": "#FFF6E0", "primary": "#1F3C88", "accent": "#FF6B35", "highlight": "#FFD23F", "text": "#1B1B1B"},
        {"name": "mint", "bg": "#E8F6EF", "primary": "#0B6E4F", "accent": "#F25F5C", "highlight": "#FFE066", "text": "#14213D"},
        {"name": "berry", "bg": "#FDEFF4", "primary": "#5B2A86", "accent": "#FF5DA2", "highlight": "#7AE7C7", "text": "#2B2D42"},
        {"name": "sky", "bg": "#EAF4FF", "primary": "#0057B8", "accent": "#FB8500", "highlight": "#FFB703", "text": "#0B1F33"},
    ],
    "productivity": [
        {"name": "sage", "bg": "#EEF0E5", "primary": "#3A4D39", "accent": "#B7895B", "highlight": "#D9CBB0", "text": "#2C2C2C"},
        {"name": "blush", "bg": "#F8EDEB", "primary": "#6D4C41", "accent": "#D8737F", "highlight": "#FFCDB2", "text": "#3D2C2E"},
        {"name": "latte", "bg": "#F5EFE6", "primary": "#2F2F2F", "accent": "#A47148", "highlight": "#E8DFCA", "text": "#2F2F2F"},
        {"name": "noir", "bg": "#1E1E24", "primary": "#2B2B33", "accent": "#C9ADA7", "highlight": "#F2E9E4", "text": "#F2E9E4"},
    ],
}

WHITE = "#FFFFFF"


# ---------------------------------------------------------------------------
# Utilitários de tipografia e imagem
# ---------------------------------------------------------------------------
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key not in _font_cache:
        path = FONTS_DIR / FONT_FILES[kind]
        try:
            _font_cache[key] = ImageFont.truetype(str(path), size)
        except OSError:
            log.debug("Fonte %s ausente; usando fonte padrão do Pillow.", path.name)
            _font_cache[key] = ImageFont.load_default(size=size)
    return _font_cache[key]


def text_w(f: ImageFont.FreeTypeFont, text: str) -> int:
    l, _, r, _ = f.getbbox(text)
    return r - l


def wrap(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if text_w(f, trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_text(text: str, kind: str, max_w: int, max_h: int, max_size: int, min_size: int = 28,
             spacing: float = 1.12, max_lines: int = 5) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Maior fonte em que o texto cabe na caixa. Retorna (fonte, linhas, altura_da_linha)."""
    size = max_size
    while size >= min_size:
        f = font(kind, size)
        lines = wrap(text, f, max_w)
        line_h = int(size * spacing)
        if (len(lines) <= max_lines and len(lines) * line_h <= max_h
                and all(text_w(f, ln) <= max_w for ln in lines)):
            return f, lines, line_h
        size -= 4
    f = font(kind, min_size)
    return f, wrap(text, f, max_w)[:max_lines], int(min_size * spacing)


def draw_lines(draw: ImageDraw.ImageDraw, lines: list[str], f, x: int, y: int, w: int, line_h: int,
               fill, align: str = "center") -> int:
    """Desenha linhas a partir de y; retorna o y final."""
    for ln in lines:
        lw = text_w(f, ln)
        lx = x + (w - lw) // 2 if align == "center" else x
        top_offset = f.getbbox(ln)[1]
        draw.text((lx, y - top_offset + (line_h - (f.getbbox(ln)[3] - top_offset)) // 2), ln, font=f, fill=fill)
        y += line_h
    return y


def rounded(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *img.size), radius=radius, fill=255)
    img.putalpha(Image.composite(img.getchannel("A"), mask, mask))
    return img


def paste_with_shadow(canvas: Image.Image, img: Image.Image, xy: tuple[int, int], blur: int = 22,
                      offset: tuple[int, int] = (0, 18), opacity: int = 110) -> None:
    pad = blur * 3
    shadow = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    alpha = img.getchannel("A").point(lambda a: a * opacity // 255)
    shadow.paste((0, 0, 0, 255), (pad, pad), alpha)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow, (xy[0] - pad + offset[0], xy[1] - pad + offset[1]))
    canvas.alpha_composite(img, xy)


def contain(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    return ImageOps.contain(img, (max_w, max_h), Image.LANCZOS)


def cover(img: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.fit(img, (w, h), Image.LANCZOS)


def vertical_gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    grad = Image.new("RGBA", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(4)))
    return grad.resize((w, h))


def hex_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, f, bg, fg, pad_x: int = 28, pad_y: int = 14,
         anchor: str = "left") -> tuple[int, int, int, int]:
    tw = text_w(f, text)
    bbox = f.getbbox(text)
    th = bbox[3] - bbox[1]
    w, h = tw + pad_x * 2, th + pad_y * 2
    if anchor == "center":
        x -= w // 2
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=bg)
    draw.text((x + pad_x, y + pad_y - bbox[1]), text, font=f, fill=fg)
    return x, y, x + w, y + h


def rotated_layer(layer: Image.Image, angle: float) -> Image.Image:
    return layer.rotate(angle, resample=Image.BICUBIC, expand=True)


def circle_badge(text: str, diameter: int, bg: str, fg: str, angle: float = 0) -> Image.Image:
    layer = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((0, 0, diameter - 1, diameter - 1), fill=bg)
    inset = int(diameter * 0.07)
    d.ellipse((inset, inset, diameter - inset, diameter - inset), outline=fg, width=3)
    f, lines, lh = fit_text(text.upper(), "bold", int(diameter * 0.66), int(diameter * 0.5),
                            max_size=int(diameter * 0.2), min_size=16, spacing=1.05, max_lines=3)
    y = (diameter - lh * len(lines)) // 2
    draw_lines(d, lines, f, 0, y, diameter, lh, fg)
    return rotated_layer(layer, angle) if angle else layer


def check_icon(draw: ImageDraw.ImageDraw, x: int, y: int, size: int, bg: str, fg: str) -> None:
    draw.ellipse((x, y, x + size, y + size), fill=bg)
    pts = [(x + size * 0.27, y + size * 0.52), (x + size * 0.44, y + size * 0.68), (x + size * 0.74, y + size * 0.34)]
    draw.line(pts, fill=fg, width=max(3, size // 9), joint="curve")


# ---------------------------------------------------------------------------
# Mockup
# ---------------------------------------------------------------------------
def fetch_mockup(product: Product, palette: dict, headline: str = "") -> Image.Image:
    cache_dir = OUTPUT_DIR.parent / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if product.image_url:
        cached = cache_dir / (hashlib.md5(product.image_url.encode()).hexdigest() + ".img")
        try:
            if product.image_url.startswith(("http://", "https://")):
                if not cached.exists():
                    resp = requests.get(product.image_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                    resp.raise_for_status()
                    cached.write_bytes(resp.content)
                data = cached.read_bytes()
            else:
                local = Path(product.image_url)  # arquivo no repositório, ex.: data/mockups/x.png
                data = (local if local.is_absolute() else ROOT / local).read_bytes()
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGBA")
            return img
        except Exception as exc:  # noqa: BLE001
            log.warning("Não foi possível carregar o mockup de '%s' (%s). Usando placeholder.", product.title, exc)
    return _placeholder(product, palette, headline)


def _placeholder(product: Product, palette: dict, headline: str = "") -> Image.Image:
    img = vertical_gradient(900, 900, hex_rgba(palette["primary"]), hex_rgba(palette["accent"]))
    d = ImageDraw.Draw(img)
    # manchete da copy (já no idioma da loja); o título cru pode estar em outro idioma (ex.: slug /pt/ do Etsy)
    f, lines, lh = fit_text(headline or product.title, "bold", 760, 600, 90, 36)
    draw_lines(d, lines, f, 70, (900 - lh * len(lines)) // 2, 760, lh, WHITE)
    return img


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    product: Product
    copy: PinCopy
    palette: dict
    badge: str
    cta: str
    eyebrow: str
    mockup: Image.Image
    rng: random.Random

    @property
    def is_edu(self) -> bool:
        return self.product.profile["niche"] == "education"


def t_bold_header(c: Ctx) -> Image.Image:
    p = c.palette
    canvas = Image.new("RGBA", (W, H), p["bg"])
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, 0, W, 560), fill=p["primary"])
    # padrão sutil de bolinhas no topo
    for yy in range(30, 540, 46):
        for xx in range(30 + (yy // 46 % 2) * 23, W, 46):
            d.ellipse((xx, yy, xx + 5, yy + 5), fill=hex_rgba(WHITE, 28))

    pill(d, W // 2, 70, c.eyebrow.upper(), font("semibold", 30), p["highlight"], p["text"] if p["name"] != "noir" else "#1E1E24", anchor="center")
    f, lines, lh = fit_text(c.copy.overlay_headline.upper(), "display", 860, 330, 130, 50, spacing=1.05, max_lines=3)
    draw_lines(d, lines, f, 70, 150 + (330 - lh * len(lines)) // 2, 860, lh, WHITE)

    mock = rounded(contain(c.mockup, 800, 680), 28)
    paste_with_shadow(canvas, mock, ((W - mock.width) // 2, 520))
    badge_fg = "#1E1E24" if p["name"] == "noir" else WHITE
    badge = circle_badge(c.badge, 200, p["accent"], badge_fg, angle=c.rng.uniform(-14, 14))
    canvas.alpha_composite(badge, (W - badge.width - 30, 430))

    y = 520 + mock.height + 50
    d = ImageDraw.Draw(canvas)
    if c.copy.overlay_subline:
        f2, l2, lh2 = fit_text(c.copy.overlay_subline, "semibold", 860, 1350 - y - 10, 46, 26, max_lines=2)
        draw_lines(d, l2, f2, 70, y, 860, lh2, p["text"])
    cta_f = font("bold", 40)
    tw = text_w(cta_f, c.cta.upper())
    bx0, bx1 = (W - tw) // 2 - 50, (W + tw) // 2 + 50
    d.rounded_rectangle((bx0, 1370, bx1, 1460), radius=45, fill=p["accent"])
    draw_lines(d, [c.cta.upper()], cta_f, bx0, 1370, bx1 - bx0, 90, WHITE)
    return canvas


def t_full_bleed(c: Ctx) -> Image.Image:
    p = c.palette
    bg = cover(c.mockup, W, H).filter(ImageFilter.GaussianBlur(28))
    canvas = Image.alpha_composite(bg, Image.new("RGBA", (W, H), (0, 0, 0, 70)))
    mock = rounded(contain(c.mockup, 860, 820), 24)
    paste_with_shadow(canvas, mock, ((W - mock.width) // 2, 110 + (820 - mock.height) // 2), blur=30, opacity=150)
    canvas.alpha_composite(vertical_gradient(W, 760, (0, 0, 0, 0), (0, 0, 0, 235)), (0, H - 760))

    d = ImageDraw.Draw(canvas)
    pill(d, 50, 50, c.badge.upper(), font("bold", 30), p["accent"], WHITE)

    f, lines, lh = fit_text(c.copy.overlay_headline, "bold", 880, 300, 104, 44, spacing=1.08, max_lines=3)
    y = 1000 + (300 - lh * len(lines))
    y = draw_lines(d, lines, f, 60, y, 880, lh, WHITE, align="left")
    if c.copy.overlay_subline:
        f2, l2, lh2 = fit_text(c.copy.overlay_subline, "regular", 880, 100, 40, 24, max_lines=2)
        y = draw_lines(d, l2, f2, 60, y + 10, 880, lh2, p["highlight"], align="left")
    cta_f = font("semibold", 34)
    cta = f"{c.cta}  >"
    d.text((60, 1405), cta, font=cta_f, fill=WHITE)
    d.line((60, 1455, 60 + text_w(cta_f, cta), 1455), fill=p["accent"], width=5)
    return canvas


def t_minimal(c: Ctx) -> Image.Image:
    p = c.palette
    dark = p["name"] == "noir"
    canvas = Image.new("RGBA", (W, H), p["bg"])
    d = ImageDraw.Draw(canvas)
    line_color = p["highlight"] if dark else p["primary"]
    d.rectangle((36, 36, W - 36, H - 36), outline=line_color, width=3)
    d.rectangle((50, 50, W - 50, H - 50), outline=hex_rgba(line_color, 90), width=1)

    eb_f = font("regular", 28)
    eyebrow = "  ".join(c.eyebrow.upper())  # letter-spacing elegante
    if text_w(eb_f, eyebrow) > 800:
        eyebrow = c.eyebrow.upper()
    draw_lines(d, [eyebrow], eb_f, 0, 110, W, 40, p["accent"])
    f, lines, lh = fit_text(c.copy.overlay_headline, "serif", 820, 280, 110, 46, spacing=1.08, max_lines=3)
    draw_lines(d, lines, f, 90, 170 + (280 - lh * len(lines)) // 2, 820, lh, p["text"] if dark else p["primary"])

    # polaroid inclinada
    mock = contain(c.mockup, 620, 580)
    frame = Image.new("RGBA", (mock.width + 60, mock.height + 130), "#FFFFFF")
    frame.alpha_composite(mock.convert("RGBA"), (30, 30))
    fd = ImageDraw.Draw(frame)
    cap_f = font("hand", 40)
    caption = c.badge
    draw_lines(fd, [caption], cap_f, 0, mock.height + 45, frame.width, 60, "#444444")
    frame = rotated_layer(frame, c.rng.choice([-4, -3, 3, 4]))
    x = (W - frame.width) // 2
    y = 470 + max(0, (790 - frame.height) // 2)
    paste_with_shadow(canvas, frame, (x, y), blur=18, opacity=90)

    d = ImageDraw.Draw(canvas)
    if c.copy.overlay_subline:
        f2, l2, lh2 = fit_text(c.copy.overlay_subline, "regular", 780, 100, 38, 24, max_lines=2)
        draw_lines(d, l2, f2, 110, 1290, 780, lh2, p["text"])
    cta_f = font("semibold", 36)
    draw_lines(d, [f"— {c.cta} —"], cta_f, 0, 1395, W, 50, p["accent"])
    return canvas


def t_split(c: Ctx) -> Image.Image:
    p = c.palette
    canvas = Image.new("RGBA", (W, H), p["bg"])
    d = ImageDraw.Draw(canvas)
    d.polygon([(0, 0), (W, 0), (W, 640), (0, 860)], fill=p["primary"])
    d.polygon([(0, 860), (W, 640), (W, 660), (0, 880)], fill=p["accent"])

    f, lines, lh = fit_text(c.copy.overlay_headline.upper(), "display", 820, 360, 140, 52, spacing=1.02, max_lines=3)
    y = draw_lines(d, lines, f, 70, 90, 820, lh, WHITE, align="left")
    if c.copy.overlay_subline:
        f2, l2, lh2 = fit_text(c.copy.overlay_subline, "semibold", 760, 110, 40, 24, max_lines=2)
        draw_lines(d, l2, f2, 74, y + 16, 760, lh2, p["highlight"], align="left")

    mock = rounded(contain(c.mockup, 720, 640), 22)
    mx = W - mock.width - 60
    paste_with_shadow(canvas, mock, (mx, 560))
    sticker = circle_badge(c.badge, 190, p["highlight"], p["text"] if p["name"] != "noir" else "#1E1E24",
                           angle=c.rng.uniform(8, 16))
    canvas.alpha_composite(sticker, (max(20, mx - 110), 500))

    d = ImageDraw.Draw(canvas)
    bullets = c.copy.bullets or [c.cta]
    by = 560 + mock.height + 50
    bf = font("semibold", 38)
    for b in bullets[:3]:
        if by > 1330:
            break
        check_icon(d, 70, by, 48, p["accent"], WHITE)
        d.text((140, by + 24), b, font=bf, fill=p["text"], anchor="lm")
        by += 70
    cta_f = font("bold", 34)
    pill(d, W // 2, 1395, c.cta.upper(), cta_f, p["primary"], WHITE, pad_x=44, pad_y=18, anchor="center")
    return canvas


def t_notebook(c: Ctx) -> Image.Image:
    p = c.palette
    paper = "#FFFDF6" if p["name"] != "noir" else "#26262E"
    canvas = Image.new("RGBA", (W, H), paper)
    d = ImageDraw.Draw(canvas)
    if c.is_edu:  # papel pautado de caderno
        for yy in range(160, H, 52):
            d.line((0, yy, W, yy), fill="#BFD7EA", width=2)
        d.line((110, 0, 110, H), fill="#F4A6A6", width=3)
    else:  # dot grid estilo bullet journal
        dot = "#D6D0C4" if p["name"] != "noir" else "#44444F"
        for yy in range(40, H, 40):
            for xx in range(40, W, 40):
                d.ellipse((xx - 2, yy - 2, xx + 2, yy + 2), fill=dot)

    kind = "hand" if c.is_edu else "semibold"
    f, lines, lh = fit_text(c.copy.overlay_headline, kind, 780, 290, 120 if c.is_edu else 96, 44,
                            spacing=1.05, max_lines=3)
    noir = p["name"] == "noir"
    band = hex_rgba(p["accent"], 110) if noir else hex_rgba(p["highlight"], 170)
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    y0 = 90 + (290 - lh * len(lines)) // 2
    for i, ln in enumerate(lines):  # marca-texto atrás de cada linha
        lw = text_w(f, ln)
        lx = 150 + (780 - lw) // 2
        ly = y0 + i * lh
        hd.rectangle((lx - 16, ly + lh * 0.45, lx + lw + 16, ly + lh * 0.95), fill=band)
    canvas.alpha_composite(hl)
    d = ImageDraw.Draw(canvas)
    text_color = p["text"]
    draw_lines(d, lines, f, 150, y0, 780, lh, text_color)

    mock = contain(c.mockup, 700, 620).convert("RGBA")
    mock = rotated_layer(mock, c.rng.uniform(-2.5, 2.5))
    mx, my = (W - mock.width) // 2 + 30, 430
    paste_with_shadow(canvas, mock, (mx, my), blur=14, opacity=80)
    for tx, angle in ((mx - 30, 35), (mx + mock.width - 150, -35)):  # fitas washi
        tape = Image.new("RGBA", (190, 54), hex_rgba(p["accent"], 175))
        td = ImageDraw.Draw(tape)
        for sx in range(0, 190, 22):
            td.line((sx, 0, sx + 10, 54), fill=hex_rgba(WHITE, 60), width=6)
        tape = rotated_layer(tape, angle)
        canvas.alpha_composite(tape, (max(0, tx), my - 40))

    d = ImageDraw.Draw(canvas)
    by = my + mock.height + 45
    bf = font(kind, 46 if c.is_edu else 36)
    for b in (c.copy.bullets or [])[:3]:
        if by > 1300:
            break
        d.rectangle((160, by + 8, 196, by + 44), outline=text_color, width=3)
        d.line([(166, by + 26), (178, by + 38), (200, by + 4)], fill=p["accent"], width=6)
        d.text((220, by + 26), b, font=bf, fill=text_color, anchor="lm")
        by += 64

    note = Image.new("RGBA", (360, 170), p["highlight"])
    nd = ImageDraw.Draw(note)
    nf, nl, nlh = fit_text(c.cta, "hand", 310, 130, 54, 26, spacing=1.0, max_lines=2)
    draw_lines(nd, nl, nf, 25, (170 - nlh * len(nl)) // 2, 310, nlh, "#2B2B2B")
    note = rotated_layer(note, c.rng.uniform(-6, -2))
    paste_with_shadow(canvas, note, (W - note.width - 50, H - note.height - 50), blur=10, opacity=70)
    return canvas


TEMPLATES = {
    "bold_header": t_bold_header,
    "full_bleed": t_full_bleed,
    "minimal": t_minimal,
    "split": t_split,
    "notebook": t_notebook,
}


def template_plan(product_id: str, cycle: int) -> list[str]:
    """Ordem dos templates para o ciclo: determinística (re-render igual), mas muda a cada ciclo."""
    names = list(TEMPLATES)
    random.Random(f"{product_id}:{cycle}").shuffle(names)
    return names


def generate_pin(product: Product, copy: PinCopy, cycle: int, variant_index: int) -> tuple[Path, str]:
    """Renderiza a variação e retorna (caminho do arquivo, nome do template).

    Prioridade: arte criada pelo Claude (data/designs) -> templates Pillow como reserva."""
    if design_path(product.id, cycle, variant_index).exists():
        out = OUTPUT_DIR / f"{product.id}_c{cycle}_v{variant_index}_ai.jpg"
        try:
            render_design(product.id, cycle, variant_index, out)
            log.info("Pin gerado: %s (arte criada pelo Claude)", out.name)
            return out, "ai_design"
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Falha ao renderizar a arte do Claude (%s); usando template Pillow.", exc)

    rng = random.Random(f"{product.id}:{cycle}:{variant_index}")
    template_name = template_plan(product.id, cycle)[variant_index % len(TEMPLATES)]
    profile = product.profile
    palettes = PALETTES[profile["niche"]]
    palette = palettes[(variant_index + cycle * 2 + rng.randrange(len(palettes))) % len(palettes)]

    ctx = Ctx(
        product=product,
        copy=copy,
        palette=palette,
        badge=rng.choice(profile["badges"]),
        cta=rng.choice(profile["ctas"]),
        eyebrow=rng.choice(profile["eyebrows"]),
        mockup=fetch_mockup(product, palette, copy.overlay_headline),
        rng=rng,
    )
    canvas = TEMPLATES[template_name](ctx).convert("RGB")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"{product.id}_c{cycle}_v{variant_index}_{template_name}.jpg"
    canvas.save(out, "JPEG", quality=92, optimize=True, progressive=True)
    log.info("Pin gerado: %s (template=%s, paleta=%s)", out.name, template_name, palette["name"])
    return out, template_name
