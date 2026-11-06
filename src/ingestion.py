"""Ingestão de produtos: data/products.json (principal) + feeds remotos opcionais (JSON ou RSS)."""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import PLATFORMS, PRODUCTS_FILE, STATS_FILE, normalize_platform, settings
from .logger import get_logger

log = get_logger("ingestion")


@dataclass
class ProductStats:
    views: int = 0
    sales: int = 0
    clicks: int = 0      # cliques/saves vindos do Pinterest ou da loja
    favorites: int = 0


@dataclass
class Product:
    id: str
    title: str
    description: str
    url: str
    platform: str
    image_url: str
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    price: str = ""
    board_id: str = ""
    active: bool = True
    stats: ProductStats = field(default_factory=ProductStats)

    @property
    def pending(self) -> bool:
        """Criado pelos agentes, mas ainda sem link (você ainda não publicou na loja)."""
        return not self.url

    @property
    def locale(self) -> str:
        return PLATFORMS[self.platform]["locale"]

    @property
    def profile(self) -> dict:
        return PLATFORMS[self.platform]

    def created_datetime(self) -> datetime | None:
        if not self.created_at:
            return None
        try:
            dt = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def to_dict(self) -> dict:
        return asdict(self)


def _first(d: dict, *keys: str, default=""):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def make_id(platform: str, title: str) -> str:
    """ID estável (não muda quando o link é adicionado depois)."""
    return hashlib.sha1(f"{normalize_platform(platform)}|{title.strip().lower()}".encode("utf-8")).hexdigest()[:12]


def _parse_product(raw: dict) -> Product | None:
    """Aceita nomes de campos flexíveis, pois cada agente pode gravar de um jeito."""
    title = str(_first(raw, "title", "name", "product_title")).strip()
    url = str(_first(raw, "url", "link", "product_url", "product_link")).strip()
    image = str(_first(raw, "image_url", "mockup_url", "image", "mockup", "thumbnail")).strip()
    if not title:
        log.warning("Produto ignorado (sem título): %s", raw)
        return None
    if str(raw.get("status", "")).lower() == "pending":
        url = ""
    if not image and url:
        log.debug("Produto '%s' sem imagem: a arte será 100%% criada.", title)

    stats_raw = raw.get("stats") or {}
    stats = ProductStats(
        views=int(_first(stats_raw, "views", default=0) or raw.get("views") or 0),
        sales=int(_first(stats_raw, "sales", default=0) or raw.get("sales") or 0),
        clicks=int(_first(stats_raw, "clicks", "saves", default=0) or 0),
        favorites=int(_first(stats_raw, "favorites", "wishlist", default=0) or 0),
    )
    tags = raw.get("tags") or raw.get("keywords") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return Product(
        id=str(_first(raw, "id", "sku", "product_id",
                      default=make_id(str(_first(raw, "platform", "source", "store", default="etsy")), title))),
        title=title,
        description=" ".join(filter(None, [
            str(_first(raw, "description", "summary", "desc")).strip(),
            f"Seller notes: {raw['notes']}" if raw.get("notes") else "",  # observações do PRODUTOS.txt
        ])),
        url=url,
        platform=normalize_platform(str(_first(raw, "platform", "source", "store", default="etsy"))),
        image_url=image,
        created_at=str(_first(raw, "created_at", "date", "published_at")),
        tags=list(tags),
        price=str(_first(raw, "price")),
        board_id=str(_first(raw, "board_id", "pinterest_board_id")),
        active=bool(raw.get("active", True)),
        stats=stats,
    )


def _load_local(path: Path) -> list[dict]:
    if not path.exists():
        log.warning("Arquivo %s não encontrado.", path)
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("products", [])
    return data if isinstance(data, list) else []


def _load_rss(xml_text: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    media_ns = "{http://search.yahoo.com/mrss/}"
    for item in root.iter("item"):
        image = ""
        enclosure = item.find("enclosure")
        if enclosure is not None:
            image = enclosure.get("url", "")
        media = item.find(f"{media_ns}content")
        if media is None:
            media = item.find(f"{media_ns}thumbnail")
        if not image and media is not None:
            image = media.get("url", "")
        items.append({
            "title": item.findtext("title", ""),
            "url": item.findtext("link", ""),
            "description": item.findtext("description", ""),
            "image_url": image,
            "created_at": item.findtext("pubDate", ""),
            "platform": item.findtext("category", ""),
        })
    return items


def _load_remote(url: str) -> list[dict]:
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "pinterest-bot/1.0"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Falha ao ler feed %s: %s", url, exc)
        return []
    text = resp.text.lstrip()
    if text.startswith("<"):
        items = _load_rss(text)
        # RSS sem categoria: tenta deduzir a plataforma pelo domínio do feed
        for it in items:
            it["platform"] = it.get("platform") or url
        return items
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("products", [])
    return data if isinstance(data, list) else []


def _merge_stats(products: dict[str, Product]) -> None:
    """data/stats.json opcional: {"<product_id>": {"views": 120, "sales": 4}} — sobrescreve os números."""
    if not STATS_FILE.exists():
        return
    try:
        stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("stats.json inválido; ignorado.")
        return
    for pid, values in stats.items():
        if pid in products and isinstance(values, dict):
            s = products[pid].stats
            s.views = int(values.get("views", s.views))
            s.sales = int(values.get("sales", s.sales))
            s.clicks = int(values.get("clicks", s.clicks))
            s.favorites = int(values.get("favorites", s.favorites))


def load_products(path: Path = PRODUCTS_FILE, include_pending: bool = False) -> list[Product]:
    """Produtos ativos. Com include_pending=True inclui os que ainda aguardam link (usado pelo refill
    para adiantar as copies; o bot de publicação nunca publica um produto sem link)."""
    raw_items = _load_local(path)
    for feed in settings.product_feed_urls:
        raw_items.extend(_load_remote(feed))

    products: dict[str, Product] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        product = _parse_product(raw)
        if product and product.active:
            products.setdefault(product.id, product)  # o arquivo local tem prioridade

    _merge_stats(products)
    pending = [p for p in products.values() if p.pending]
    if pending:
        log.info("%d produto(s) aguardando link (rode scripts/add_links.py após publicar na loja): %s",
                 len(pending), ", ".join(p.title[:40] for p in pending))
    result = [p for p in products.values() if include_pending or not p.pending]
    log.info("%d produtos carregados%s.", len(result), " (incluindo pendentes)" if include_pending else "")
    return result
