"""Copywriting + SEO para Pinterest com Claude (Anthropic SDK), com cache e fallback por template."""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date

from .config import COPY_CACHE_ACTIONS_FILE, COPY_CACHE_FILE, settings
from .ingestion import Product
from .logger import get_logger

log = get_logger("copywriter")

TITLE_MAX, DESC_MAX, ALT_MAX = 100, 500, 500

# Ângulos distintos para cada variação: evita Pins "clones" e testa ganchos diferentes
ANGLES = [
    "pain point -> solution (open with the frustration the buyer feels, then the relief this product brings)",
    "benefit-led (lead with the main outcome/transformation and the time it saves)",
    "keyword-rich search intent (mirror exactly how people search on Pinterest for this kind of resource)",
    "listicle / ideas angle (e.g. '5 ways to...', 'ideas for...', what's inside the product)",
    "seasonal / timely angle (tie it to the current season, term, holiday or planning moment)",
]


@dataclass
class PinCopy:
    title: str
    description: str
    hashtags: list[str]
    alt_text: str
    overlay_headline: str
    overlay_subline: str
    bullets: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    def full_description(self) -> str:
        tags = " ".join(h if h.startswith("#") else f"#{h}" for h in self.hashtags[:5])
        desc = self.description.strip()
        room = DESC_MAX - len(tags) - 2
        if len(desc) > room:
            desc = desc[: room - 1].rsplit(" ", 1)[0] + "…"
        return f"{desc}\n\n{tags}".strip()


COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "alt_text": {"type": "string"},
                    "overlay_headline": {"type": "string"},
                    "overlay_subline": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "title", "description", "hashtags", "alt_text",
                    "overlay_headline", "overlay_subline", "bullets", "keywords",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["variants"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior Pinterest SEO strategist and direct-response copywriter for digital products \
(teaching resources, Notion templates, printables and planners).

Pinterest is a visual search engine: titles and descriptions must contain the exact phrases people type into \
Pinterest search, placed naturally at the start. Write for clicks and saves, never clickbait. Be specific about \
what the buyer gets.

Hard rules for every variant:
- title: max 100 characters, starts with the primary keyword phrase, compelling, no hashtags, no emojis.
- description: 250-450 characters, 2-3 short sentences: hook on the pain point, the solution/benefit, then a clear \
call to action that points to the link (e.g. "Tap the link to download"). Weave in 3-5 long-tail keywords naturally. \
No hashtags inside the description.
- hashtags: 3-5 strategic hashtags (niche + broad), no spaces, each starting with #.
- alt_text: max 400 characters, a literal, accessible description of what the pin image shows (the product mockup \
and the headline text), including the main keyword.
- overlay_headline: 2-6 punchy words printed big on the image (title case).
- overlay_subline: 3-9 words supporting the headline.
- bullets: exactly 3 ultra-short benefits (max 4 words each) to print on the image.
- keywords: the 5 highest-intent search phrases you targeted.
- Each variant must use a different angle and a different hook; never repeat the same title opening.
- Never invent facts not supported by the product info (no fake ratings, prices, sales numbers or standards)."""


def _locale_rule(locale: str) -> str:
    if locale == "en-GB":
        return ("Write EVERYTHING in British English (en-GB): British spelling (colour, organise, favourite, "
                "personalised, centre), UK school vocabulary (pupils, maths, Year 3, KS1/KS2/KS3, GCSE, term, "
                "lesson planning). Never use American spellings.")
    return ("Write EVERYTHING in American English (en-US): American spelling (color, organize, favorite, "
            "personalized, center) and US vocabulary (students, math, grade levels, K-12, back to school).")


def _season_hint(today: date) -> str:
    m = today.month
    return {
        1: "New Year planning, fresh start, winter term", 2: "Valentine's Day, winter, mid-year",
        3: "spring, St. Patrick's Day, test prep", 4: "spring, Easter, end-of-year planning begins",
        5: "end of school year, summer prep, exams", 6: "summer, end of year, planning next year",
        7: "summer planning, back-to-school prep", 8: "back to school, new academic year setup",
        9: "back to school, autumn/fall term, routines", 10: "fall/autumn, Halloween, organisation",
        11: "Thanksgiving (US) / autumn term (UK), holiday planning, gifts", 12: "Christmas, holidays, year-end review, New Year goals",
    }[m]


# ---------------------------------------------------------------------------
# Cache de copies
#   data/copy_cache.json          -> escrito SOMENTE pelo seu PC (refill local com Claude Code)
#   data/copy_cache_actions.json  -> escrito SOMENTE pelo GitHub Actions (política "api")
# Cada arquivo tem um único escritor, então commits dos dois lados nunca conflitam.
# ---------------------------------------------------------------------------
TEMPLATE_FALLBACKS: list[str] = []  # produtos que saíram com copy de template nesta execução


class CopyNotReady(Exception):
    """Não há copy pronta na fila para esta variação (e a política é 'skip')."""


def _read_json(path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("%s inválido; ignorado.", path.name)
    return {}


def load_cache(path=COPY_CACHE_FILE) -> dict:
    return _read_json(path)


def save_cache(cache: dict, path=COPY_CACHE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def cache_key(product_id: str, cycle: int) -> str:
    return f"{product_id}:c{cycle}"


def cached_variants(product_id: str, cycle: int) -> list[dict]:
    key = cache_key(product_id, cycle)
    for path in (COPY_CACHE_FILE, COPY_CACHE_ACTIONS_FILE):  # o cache do PC tem prioridade
        entry = _read_json(path).get(key)
        if entry and entry.get("variants"):
            return entry["variants"]
    return []


def copy_ready(product_id: str, cycle: int, variant_count: int) -> bool:
    return len(cached_variants(product_id, cycle)) >= variant_count


def _sanitize(v: dict, product: Product) -> PinCopy:
    def clip(text: str, n: int) -> str:
        text = (text or "").strip().replace("\n", " ")
        return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"

    hashtags = []
    for h in v.get("hashtags", []):
        h = "#" + "".join(ch for ch in h.lstrip("#") if ch.isalnum() or ch == "_")
        if len(h) > 1 and h.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(h)
    bullets = [clip(b, 32) for b in v.get("bullets", []) if b.strip()][:3]
    return PinCopy(
        title=clip(v.get("title") or product.title, TITLE_MAX),
        description=clip(v.get("description") or product.description, DESC_MAX),
        hashtags=hashtags[:5],
        alt_text=clip(v.get("alt_text") or f"Pinterest pin showing {product.title}", ALT_MAX),
        overlay_headline=clip(v.get("overlay_headline") or product.title, 60),
        overlay_subline=clip(v.get("overlay_subline") or "", 70),
        bullets=bullets,
        keywords=[k.strip() for k in v.get("keywords", []) if k.strip()][:6],
    )


def build_prompt(product: Product, n: int, cycle: int) -> str:
    profile = product.profile
    angles = random.sample(ANGLES, k=min(n, len(ANGLES))) + ANGLES[: max(0, n - len(ANGLES))]
    refresh = ("write completely fresh hooks, different from obvious first-take copy" if cycle
               else "start with the strongest, most searchable angles")
    angle_list = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(angles[:n]))
    return f"""Create {n} distinct Pinterest pin copy variants for this digital product.

<product>
Platform: {profile['label']}
Title: {product.title}
Description: {product.description or '(not provided - infer only from the title and tags)'}
Tags: {', '.join(product.tags) or '(none)'}
Price: {product.price or '(not provided)'}
</product>

Language: {_locale_rule(product.locale)}
Brand voice: {profile['tone']}.
Today is {date.today().isoformat()}; timely themes right now: {_season_hint(date.today())}.
This is refresh cycle #{cycle}: {refresh}.

Use one angle per variant, in this order:
{angle_list}

Return exactly {n} items in "variants"."""


def call_anthropic_api(product: Product, n: int, cycle: int) -> list[dict]:
    """Backend opcional via API (usado apenas com EMPTY_QUEUE_POLICY=api)."""
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, max_retries=3)
    request = dict(
        model=settings.claude_model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(product, n, cycle)}],
        output_config={"effort": settings.claude_effort, "format": {"type": "json_schema", "schema": COPY_SCHEMA}},
    )
    response = None
    if settings.claude_fallbacks:
        try:
            # Fallback no servidor: se o modelo recusar por política, outro modelo continua na mesma chamada
            response = client.beta.messages.create(
                **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        except (TypeError, anthropic.BadRequestError) as exc:
            log.warning("Fallback server-side indisponível (%s); usando chamada padrão.", exc)
    if response is None:
        response = client.messages.create(**request)
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude recusou a solicitação para este produto.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Resposta truncada (max_tokens).")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["variants"]


def _template_copy(product: Product, n: int) -> list[dict]:
    """Fallback sem IA: garante que o bot nunca pare por falta de copy."""
    gb = product.locale == "en-GB"
    base = product.title.split("|")[0].split("-")[0].strip()
    tags = [t.replace(" ", "") for t in product.tags[:3]] or [product.platform]
    niche = product.profile["niche"]
    extra = ["#TeacherResources", "#ClassroomIdeas"] if niche == "education" else ["#NotionTemplate", "#GetOrganized"]
    if product.platform == "etsy":
        extra = ["#DigitalDownload", "#Printable"]
    favourite = "Favourite" if gb else "Favorite"
    variants = []
    hooks = [
        f"{base}: Ready to Use Today",
        f"Save Hours with this {base}",
        f"{base} | Instant Digital Download",
        f"The {favourite} {base} for Busy People",
        f"{base}: Simple, Beautiful, Done",
    ]
    for i in range(n):
        variants.append({
            "title": hooks[i % len(hooks)],
            "description": (
                f"{(product.description or product.title)[:280]} "
                f"Tap the link to get your instant download and start today."
            ),
            "hashtags": [f"#{t}" for t in tags] + extra,
            "alt_text": f"Pinterest pin showing a mockup of {product.title} with the headline text overlaid.",
            "overlay_headline": base if len(base) <= 40 else base[:40].rsplit(" ", 1)[0],
            "overlay_subline": "Instant digital download",
            "bullets": ["Instant download", "Easy to use", "Save time"],
            "keywords": product.tags[:5],
        })
    return variants


def get_copy(product: Product, cycle: int, variant_index: int, variant_count: int) -> PinCopy:
    """Copy da variação. No GitHub Actions só LÊ a fila gerada pelo seu PC.

    Fila vazia para esta variação -> EMPTY_QUEUE_POLICY:
      template (padrão): copy por template, sem IA (determinística, não é gravada)
      skip:              levanta CopyNotReady e o Pin fica para depois
      api:               gera com a API Anthropic (requer ANTHROPIC_API_KEY) e grava em copy_cache_actions.json
    """
    variants = cached_variants(product.id, cycle)
    if len(variants) < variant_count:
        policy = settings.empty_queue_policy
        if policy == "skip":
            raise CopyNotReady(f"Sem copy na fila para {product.id} (ciclo {cycle}).")
        if policy == "api" and settings.anthropic_api_key:
            try:
                variants = call_anthropic_api(product, variant_count, cycle)
                cache = load_cache(COPY_CACHE_ACTIONS_FILE)
                cache[cache_key(product.id, cycle)] = {
                    "source": f"api:{settings.claude_model}",
                    "generated_at": date.today().isoformat(),
                    "variants": variants,
                }
                save_cache(cache, COPY_CACHE_ACTIONS_FILE)
            except Exception as exc:  # noqa: BLE001
                log.warning("API falhou para '%s' (%s); usando template.", product.title, exc)
                variants = []
        if len(variants) < variant_count:
            log.warning("Fila de copies vazia para '%s' (ciclo %d): usando template. Ligue o PC para o refill.",
                        product.title, cycle)
            TEMPLATE_FALLBACKS.append(product.title)
            variants = _template_copy(product, variant_count)
    return _sanitize(variants[variant_index % len(variants)], product)


def copy_to_dict(copy: PinCopy) -> dict:
    return asdict(copy)
