"""Artes criativas feitas pelo Claude: cada variação é um Pin desenhado em HTML/CSS/SVG e
convertido em imagem 1000x1500 por um navegador headless (Chrome/Chromium/Edge).

- O seu PC (refill) pede ao Claude Code os designs e grava em data/designs/<produto>/c<ciclo>_v<n>.html
- O GitHub Actions só renderiza o HTML na hora de publicar (o Chrome já vem instalado no runner)
- Sem design disponível, o bot usa os templates Pillow (image_generator) como reserva
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageStat

from .config import DATA_DIR, PIN_HEIGHT, PIN_WIDTH
from .ingestion import Product
from .logger import get_logger

log = get_logger("designer")
DESIGNS_DIR = DATA_DIR / "designs"

DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "designs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "html": {"type": "string"},
                },
                "required": ["concept", "html"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["designs"],
    "additionalProperties": False,
}

DESIGN_SYSTEM_PROMPT = f"""You are a senior graphic designer who creates Pinterest pins that get saved and clicked.
You design each pin as a single self-contained HTML document that is screenshotted at exactly \
{PIN_WIDTH}x{PIN_HEIGHT}px.

What performs on Pinterest (apply it):
- One clear focal point and a bold, benefit-driven headline readable on a phone at thumbnail size: \
headline 90-170px, max ~8 words, very high contrast with its background.
- Native, "saveable" look (an idea worth keeping), not a banner ad. Generous whitespace, 1-2 typefaces, \
a tight palette of 3-4 colors, strong hierarchy: headline > visual > supporting line > small CTA.
- Formats that win: bold typographic posters, listicles ("5 no-prep lessons..."), checklists, \
problem -> solution splits, stylized product previews (pages, worksheets, planner spreads, app dashboards \
drawn with CSS), flat-lay desk scenes drawn in SVG, sticky notes / corkboards, editorial magazine covers, \
seasonal themes.
- Education (TpT/Tes): bright, warm, friendly but professional. Productivity/aesthetic (Notion/Etsy): \
refined, calm, minimal or soft aesthetic palettes; Notion = clean UI/dashboard illustrations.

Hard technical rules:
- Full document: <!doctype html>, <meta charset="utf-8">, one <style> block. No JavaScript.
- html, body {{ margin:0; width:{PIN_WIDTH}px; height:{PIN_HEIGHT}px; overflow:hidden; }}
- Fonts: only Google Fonts through ONE <link> to fonts.googleapis.com/css2 with &display=block.
- Visuals: CSS shapes/gradients and inline SVG that you draw. No external images EXCEPT the product image \
URL if one is provided (use it as a real element, e.g. inside a device/frame/card). No emoji characters.
- Every piece of text must fit inside the canvas with at least 60px side margins: use explicit widths, \
font sizes and line-heights; prefer absolutely positioned blocks; never let text overflow or overlap.
- Use the exact text provided for the headline, supporting line, bullets and CTA (you may shorten, never \
invent new claims). Never invent ratings, reviews, sales numbers, "best seller", prices or awards.
- If you draw a preview of the product's pages, keep it stylized/generic (lines, headings, boxes, \
checkboxes) and only label sections that the product info actually mentions."""


def design_path(product_id: str, cycle: int, index: int) -> Path:
    return DESIGNS_DIR / product_id / f"c{cycle}_v{index}.html"


def designs_ready(product_id: str, cycle: int, count: int) -> bool:
    return all(design_path(product_id, cycle, i).exists() for i in range(count))


def build_design_prompt(product: Product, copies: list[dict], cycle: int) -> str:
    profile = product.profile
    image_line = (f"Product image URL (optional to use, include it in at most half of the designs): {product.image_url}"
                  if product.image_url.startswith("http") else "No product image available: the art must be fully designed.")
    blocks = []
    for i, c in enumerate(copies):
        bullets = "; ".join(c.get("bullets", [])[:3])
        blocks.append(
            f"Design {i + 1}:\n  headline: {c.get('overlay_headline')}\n  supporting line: {c.get('overlay_subline')}\n"
            f"  bullets: {bullets}\n  pin title (context only): {c.get('title')}"
        )
    language = "British English (en-GB)" if product.locale == "en-GB" else "American English (en-US)"
    return f"""Design {len(copies)} Pinterest pins for this digital product. Each design must use a DIFFERENT \
visual concept and layout (no two alike), and all of them must look like they belong to a talented designer.

<product>
Platform: {profile['label']} ({profile['niche']})
Title: {product.title}
Description: {product.description or '(not provided)'}
{image_line}
</product>

All text on the pins in {language}. Small CTA options: {', '.join(profile['ctas'][:4])}.
Factual badge ideas you may use: {', '.join(b for b in profile['badges'] if b not in ('Top Rated', 'Best Seller', 'Teacher Favorite', 'Teacher Favourite'))}.
Refresh cycle #{cycle}{': explore new concepts and palettes' if cycle else ''}.

{chr(10).join(blocks)}

Return one item per design in "designs", in the same order, each with a short "concept" name and the full "html"."""


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------
def find_browser() -> str | None:
    env = os.getenv("CHROME_BIN", "").strip()
    if env:
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(path).exists():
            return path
    return None


def render_html(html_file: Path, out_file: Path) -> Path:
    """HTML -> imagem 1000x1500. Levanta RuntimeError se o navegador falhar ou a imagem sair vazia."""
    browser = find_browser()
    if not browser:
        raise RuntimeError("Nenhum Chrome/Chromium/Edge encontrado para renderizar o design (defina CHROME_BIN).")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as profile_dir:
        png = Path(profile_dir) / "shot.png"
        cmd = [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={profile_dir}", "--force-device-scale-factor=1",
            f"--window-size={PIN_WIDTH},{PIN_HEIGHT}", "--virtual-time-budget=10000",
            f"--screenshot={png}", html_file.resolve().as_uri(),
        ]
        if os.name != "nt":
            cmd.insert(1, "--no-sandbox")  # necessário em containers/CI Linux
        subprocess.run(cmd, capture_output=True, timeout=90)
        if not png.exists():
            raise RuntimeError(f"O navegador não gerou a imagem de {html_file.name}.")
        img = Image.open(png).convert("RGB")
    if img.size != (PIN_WIDTH, PIN_HEIGHT):
        img = img.resize((PIN_WIDTH, PIN_HEIGHT), Image.LANCZOS)
    if max(ImageStat.Stat(img).stddev) < 6:  # praticamente uma cor só: design quebrado
        raise RuntimeError(f"Design {html_file.name} renderizou em branco/sem conteúdo.")
    if out_file.suffix.lower() in (".jpg", ".jpeg"):
        img.save(out_file, "JPEG", quality=92, optimize=True, progressive=True)
    else:
        img.save(out_file)
    return out_file


def render_design(product_id: str, cycle: int, index: int, out_file: Path) -> Path | None:
    path = design_path(product_id, cycle, index)
    if not path.exists():
        return None
    return render_html(path, out_file)
