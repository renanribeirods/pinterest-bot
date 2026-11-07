"""Configuração central: caminhos, variáveis de ambiente e perfis por plataforma."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"
FONTS_DIR = TEMPLATES_DIR / "fonts"
ASSETS_DIR = TEMPLATES_DIR / "assets"
OUTPUT_DIR = ROOT / "output" / "pins"

PRODUCTS_FILE = DATA_DIR / "products.json"
STATS_FILE = DATA_DIR / "stats.json"
HISTORY_FILE = DATA_DIR / "history.json"
COPY_CACHE_FILE = DATA_DIR / "copy_cache.json"
COPY_CACHE_ACTIONS_FILE = DATA_DIR / "copy_cache_actions.json"
BOARDS_FILE = DATA_DIR / "boards.json"

PIN_WIDTH, PIN_HEIGHT = 1000, 1500

# Carrega .env apenas em execução local (no GitHub Actions os Secrets já viram env vars)
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on", "sim"}


# ---------------------------------------------------------------------------
# Perfis de plataforma: idioma, tom de voz, nicho visual, badges e CTAs
# ---------------------------------------------------------------------------
PLATFORM_ALIASES = {
    "tpt": "tpt",
    "teacherspayteachers": "tpt",
    "teachers pay teachers": "tpt",
    "teacherspayteachers.com": "tpt",
    "notion": "notion",
    "notion marketplace": "notion",
    "notion.so": "notion",
    "etsy": "etsy",
    "etsy.com": "etsy",
    "tes": "tes",
    "tes.com": "tes",
    "tes resources": "tes",
}

PLATFORMS: dict[str, dict] = {
    "tpt": {
        "label": "Teachers Pay Teachers",
        "locale": "en-US",
        "niche": "education",
        "tone": (
            "warm, practical and pedagogical, written for busy K-12 teachers and homeschool parents in the US; "
            "highlight time saved, student engagement, standards alignment and 'no-prep / print and go' value"
        ),
        "badges": ["Print & Go", "Digital Download", "Ready to Use", "Instant Download"],
        "ctas": ["Grab Yours Today", "Instant Download", "Link in Description", "Shop Now on TpT", "Save for Later"],
        "eyebrows": ["Classroom Resource", "For Teachers", "Ready to Print", "Save Hours of Prep"],
    },
    "tes": {
        "label": "Tes",
        "locale": "en-GB",
        "niche": "education",
        "tone": (
            "warm, professional and pedagogical, written for UK teachers (primary and secondary); "
            "use British spelling and vocabulary (colour, organise, favourite, maths, Year 6, KS2, pupils, lesson plans)"
        ),
        "badges": ["Ready to Teach", "Digital Download", "Instant Download", "Print & Teach"],
        "ctas": ["Download Today", "Instant Download", "Link in the Description", "Get Yours Now", "Save for Later"],
        "eyebrows": ["Classroom Resource", "For UK Teachers", "Ready to Teach", "Save Hours of Planning"],
    },
    "notion": {
        "label": "Notion Marketplace",
        "locale": "en-US",
        "niche": "productivity",
        "tone": (
            "calm, aesthetic and aspirational, focused on organization, clarity and productivity; "
            "speak to students, creators and professionals who want a beautiful, plug-and-play system"
        ),
        "badges": ["Notion Template", "Plug & Play", "Aesthetic Setup", "Instant Access", "Duplicate in 1 Click", "Ready to Use"],
        "ctas": ["Get the Template", "Duplicate in 1 Click", "Link in Description", "Organize Your Life", "Save for Later"],
        "eyebrows": ["Notion Template", "Productivity System", "Aesthetic Workspace", "Get Organized"],
    },
    "etsy": {
        "label": "Etsy",
        "locale": "en-US",
        "niche": "productivity",
        "tone": (
            "friendly, aesthetic and gift-worthy, focused on beautiful design, organization and instant digital delivery; "
            "speak to planners, small business owners and aesthetic-lovers"
        ),
        "badges": ["Digital Download", "Instant Download", "Printable", "US Letter & A4", "Print at Home"],
        "ctas": ["Shop Now", "Instant Download", "Link in Description", "Get Yours Today", "Save for Later"],
        "eyebrows": ["Digital Download", "Printable", "Shop Small", "Aesthetic Planner"],
    },
}


def normalize_platform(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[key]
    for alias, canonical in PLATFORM_ALIASES.items():
        if alias in key:
            return canonical
    return "etsy"  # padrão seguro: en-US, nicho estético


@dataclass
class Settings:
    # Volume e ritmo
    pins_per_run: int = field(default_factory=lambda: _env_int("PINS_PER_RUN", 2))
    variants_min: int = field(default_factory=lambda: _env_int("VARIANTS_MIN", 3))
    variants_max: int = field(default_factory=lambda: _env_int("VARIANTS_MAX", 3))  # 3 variações: economiza o limite do Claude Pro
    jitter_max_minutes: int = field(default_factory=lambda: _env_int("JITTER_MAX_MINUTES", 10))
    delay_between_pins: tuple[int, int] = field(
        default_factory=lambda: (_env_int("DELAY_MIN_SECONDS", 45), _env_int("DELAY_MAX_SECONDS", 150))
    )
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    # Rotação / priorização
    product_cooldown_hours: float = field(default_factory=lambda: _env_float("PRODUCT_COOLDOWN_HOURS", 18))
    avoid_last_n_products: int = field(default_factory=lambda: _env_int("AVOID_LAST_N_PRODUCTS", 3))
    new_product_boost_days: int = field(default_factory=lambda: _env_int("NEW_PRODUCT_BOOST_DAYS", 10))
    max_consecutive_failures: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_FAILURES", 3))

    # Copy: o PC gera a fila com Claude Code (sua assinatura); o Actions só consome
    empty_queue_policy: str = field(default_factory=lambda: _env("EMPTY_QUEUE_POLICY", "template").lower())
    claude_bin: str = field(default_factory=lambda: _env("CLAUDE_BIN"))
    local_claude_model: str = field(default_factory=lambda: _env("LOCAL_CLAUDE_MODEL", "sonnet"))  # Sonnet: bem mais leve no limite que o Opus
    refill_min_days: float = field(default_factory=lambda: _env_float("REFILL_MIN_DAYS", 5))
    refill_max_products: int = field(default_factory=lambda: _env_int("REFILL_MAX_PRODUCTS", 5))  # no máximo 5 produtos por refill
    runs_per_day: int = field(default_factory=lambda: _env_int("RUNS_PER_DAY", 4))
    ai_designs: bool = field(default_factory=lambda: _env_bool("AI_DESIGNS", True))  # artes criadas pelo Claude

    # API Anthropic (opcional: só usada com EMPTY_QUEUE_POLICY=api)
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: _env("CLAUDE_EFFORT", "medium"))
    claude_fallbacks: bool = field(default_factory=lambda: _env_bool("CLAUDE_FALLBACKS", True))

    # Publicação
    publish_channels: list[str] = field(
        default_factory=lambda: [c.strip().lower() for c in _env("PUBLISH_CHANNELS", "webhook,api").split(",") if c.strip()]
    )
    pinterest_access_token: str = field(default_factory=lambda: _env("PINTEREST_ACCESS_TOKEN"))
    pinterest_refresh_token: str = field(default_factory=lambda: _env("PINTEREST_REFRESH_TOKEN"))
    pinterest_client_id: str = field(default_factory=lambda: _env("PINTEREST_APP_ID"))
    pinterest_client_secret: str = field(default_factory=lambda: _env("PINTEREST_APP_SECRET"))
    pinterest_api_base: str = field(default_factory=lambda: _env("PINTEREST_API_BASE", "https://api.pinterest.com/v5"))
    pinterest_default_board_id: str = field(default_factory=lambda: _env("PINTEREST_BOARD_ID"))
    board_map_raw: str = field(default_factory=lambda: _env("PINTEREST_BOARD_MAP"))

    webhook_url: str = field(default_factory=lambda: _env("PIN_WEBHOOK_URL"))
    webhook_secret: str = field(default_factory=lambda: _env("PIN_WEBHOOK_SECRET"))
    webhook_include_base64: bool = field(default_factory=lambda: _env_bool("WEBHOOK_INCLUDE_BASE64", False))

    # Hospedagem de imagem (necessária para o webhook, que precisa de URL pública)
    image_host: str = field(default_factory=lambda: _env("IMAGE_HOST", "github").lower())
    imgbb_api_key: str = field(default_factory=lambda: _env("IMGBB_API_KEY"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_repository: str = field(default_factory=lambda: _env("GITHUB_REPOSITORY"))
    github_assets_branch: str = field(default_factory=lambda: _env("PIN_ASSETS_BRANCH", "pin-assets"))

    # Ingestão remota opcional (URLs separadas por vírgula: JSON ou RSS)
    product_feed_urls: list[str] = field(
        default_factory=lambda: [u.strip() for u in _env("PRODUCT_FEED_URLS").split(",") if u.strip()]
    )

    def board_for(self, platform: str, product_board: str | None = None) -> str:
        """Resolve o board: produto > PINTEREST_BOARD_MAP > data/boards.json > board padrão."""
        if product_board:
            return product_board
        mapping: dict = {}
        if BOARDS_FILE.exists():
            try:
                mapping.update(json.loads(BOARDS_FILE.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        if self.board_map_raw:
            try:
                mapping.update(json.loads(self.board_map_raw))
            except json.JSONDecodeError:
                pass
        return str(mapping.get(platform) or mapping.get("default") or self.pinterest_default_board_id or "")


settings = Settings()
