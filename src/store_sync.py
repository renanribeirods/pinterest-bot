"""Sincronização com as suas lojas: encontra os links dos produtos publicados e os liga aos pendentes.

Configuração em data/stores.json. Por plataforma:
- tes:    lê a página pública da sua loja (títulos, links e capas)
- notion: lê o perfil de criador (dados estruturados da própria página: nome, link, descrição, imagem)
- etsy:   API oficial do Etsy (a página da loja bloqueia leitura automática) -> requer ETSY_API_KEY
- tpt:    o TPT bloqueia leitura automática e não tem API pública -> links continuam via adicionar_links.bat

Para cada produto pendente (sem link), procura na loja da mesma plataforma o anúncio de título mais
parecido: se for praticamente igual, ativa sozinho; se for parecido, deixa como sugestão para
você confirmar no adicionar_links.bat. Anúncios da loja que não existem no products.json podem ser
importados automaticamente (import_unknown), para que todo o catálogo seja divulgado.
"""
from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import requests

from .config import DATA_DIR, PRODUCTS_FILE, ROOT, normalize_platform
from .ingestion import make_id
from .logger import get_logger

log = get_logger("store_sync")

STORES_FILE = DATA_DIR / "stores.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
AUTO_MATCH = 0.86   # títulos praticamente iguais -> ativa sozinho
SUGGEST = 0.55      # parecidos -> sugestão para confirmar
STOPWORDS = {"the", "a", "an", "and", "or", "for", "of", "to", "with", "in", "on", "by", "your", "pack", "bundle"}


@dataclass
class StoreItem:
    platform: str
    title: str
    url: str
    image: str = ""
    description: str = ""
    price: str = ""


class StoreBlocked(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Leitores por plataforma
# ---------------------------------------------------------------------------
def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    if r.status_code in (403, 406, 429):
        raise StoreBlocked(f"{urlsplit(url).netloc} bloqueou a leitura automática (HTTP {r.status_code}).")
    r.raise_for_status()
    return r


def _tes_full_cover(src: str) -> str:
    """A busca do TES serve miniaturas de 336px; o mesmo arquivo existe em 800x600 no CDN original."""
    m = re.match(r"https://l\.imgt\.es/resource-preview-imgs/([0-9a-f-]+)%2F([^?]+)", src)
    return f"https://d1e4pidl3fu268.cloudfront.net/{m.group(1)}/{m.group(2)}" if m else src


def scan_tes(store_url: str) -> list[StoreItem]:
    # A página da loja mostra só os 4 mais recentes; o botão "View all" leva à busca por autor (catálogo todo)
    shop = _get(store_url.split("?")[0]).text
    m = re.search(r'href="(/resources/search/?\?authorId=\d+)"', shop)
    base = urljoin("https://www.tes.com", html.unescape(m.group(1))) if m else store_url.split("?")[0]
    sep = "&" if "?" in base else "?"
    items: dict[str, StoreItem] = {}
    for page in range(1, 21):
        text = shop if (page == 1 and not m) else _get(base if page == 1 else f"{base}{sep}p={page}").text
        covers = {}
        for tag in re.findall(r'<img[^>]*resource-card__media-object[^>]*>', text):  # alt/src em qualquer ordem
            alt, src = re.search(r'alt="([^"]*)"', tag), re.search(r'src="([^"]+)"', tag)
            if alt and src:
                covers[html.unescape(alt.group(1)).strip()] = _tes_full_cover(html.unescape(src.group(1)))
        new = 0
        for href, inner in re.findall(
                r'<a[^>]+href="(?:https://www\.tes\.com)?(/teaching-resource/[^"?#]+)"[^>]*>(.*?)</a>', text, re.S):
            title = html.unescape(" ".join(re.sub(r"<[^>]+>", " ", inner).split()))
            url = urljoin("https://www.tes.com", href)
            if not title:
                continue
            if url not in items:
                new += 1
                items[url] = StoreItem("tes", title, url, image=covers.get(title, ""))
        if not new:  # página sem novidades = fim da paginação
            break
    return list(items.values())


def scan_notion(store_url: str) -> list[StoreItem]:
    text = _get(store_url).text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if not m:
        raise RuntimeError("Formato da página da Notion mudou (sem __NEXT_DATA__).")
    props = json.loads(m.group(1))["props"]["pageProps"]
    items = []
    for t in (props.get("pinnedTemplates") or []) + (props.get("nonPinnedTemplates") or []):
        if not t.get("slug"):
            continue
        attrs = t.get("attributes") or {}
        image = (attrs.get("image") or {}).get("url", "") if isinstance(attrs, dict) else ""
        price = str(t.get("price") or "")
        items.append(StoreItem(
            "notion", t.get("name", ""), f"https://www.notion.com/templates/{t['slug']}",
            image=image, description=t.get("description", ""),
            price=f"${price}" if price and price not in {"0", "-1"} else "",
        ))
    return items


def scan_etsy(store_url: str) -> list[StoreItem]:
    """API oficial do Etsy v3. Crie a chave em https://www.etsy.com/developers/your-apps ."""
    key = os.getenv("ETSY_API_KEY", "").strip()
    if not key:
        raise StoreBlocked("Etsy exige a API oficial: defina ETSY_API_KEY (veja o README).")
    secret = os.getenv("ETSY_SHARED_SECRET", "").strip()
    headers = {"x-api-key": f"{key}:{secret}" if secret else key}
    shop_name = urlsplit(store_url).path.rstrip("/").split("/")[-1]
    api = "https://openapi.etsy.com/v3/application"
    r = requests.get(f"{api}/shops", params={"shop_name": shop_name}, headers=headers, timeout=30)
    if r.status_code in (401, 403):
        raise StoreBlocked(f"Etsy recusou a chave ({r.status_code}): {r.text[:200]}")
    r.raise_for_status()
    shops = r.json().get("results") or []
    if not shops:
        raise RuntimeError(f"Loja Etsy '{shop_name}' não encontrada na API.")
    shop_id = shops[0]["shop_id"]

    items, offset = [], 0
    while True:
        r = requests.get(f"{api}/shops/{shop_id}/listings/active", headers=headers, timeout=30,
                         params={"limit": 100, "offset": offset, "includes": "Images"})
        r.raise_for_status()
        results = r.json().get("results") or []
        for it in results:
            images = it.get("images") or []
            price = it.get("price") or {}
            amount = price.get("amount") and price.get("divisor") and price["amount"] / price["divisor"]
            items.append(StoreItem(
                "etsy", html.unescape(it.get("title", "")),
                (it.get("url") or f"https://www.etsy.com/listing/{it['listing_id']}").split("?")[0],
                image=images[0].get("url_fullxfull", "") if images else "",
                description=html.unescape(it.get("description", ""))[:600],
                price=f"${amount:.2f}" if amount else "",
            ))
        if len(results) < 100:
            break
        offset += 100
    return items


def scan_tpt(store_url: str) -> list[StoreItem]:
    raise StoreBlocked("TPT bloqueia leitura automática e não tem API pública: use adicionar_links.bat.")


SCANNERS = {"tes": scan_tes, "notion": scan_notion, "etsy": scan_etsy, "tpt": scan_tpt}


# ---------------------------------------------------------------------------
# Comparação de títulos
# ---------------------------------------------------------------------------
def _norm(title: str) -> str:
    title = html.unescape(title).lower().replace("&", " and ")
    return " ".join(w for w in re.findall(r"[a-z0-9]+", title) if w not in STOPWORDS)


def similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    containment = len(ta & tb) / min(len(ta), len(tb))   # um título "dentro" do outro
    # "contido" sozinho nunca passa de AUTO_MATCH: vira só sugestão
    return max(SequenceMatcher(None, na, nb).ratio(), 0.84 * containment if min(len(ta), len(tb)) >= 3 else 0)


# ---------------------------------------------------------------------------
def load_stores() -> dict[str, str]:
    if not STORES_FILE.exists():
        return {}
    cfg = json.loads(STORES_FILE.read_text(encoding="utf-8"))
    return {normalize_platform(k): v for k, v in cfg.get("stores", {}).items() if v}


# ---------------------------------------------------------------------------
# Bloco de notas PRODUTOS.txt: você anota "Nome | link | observações" ao publicar um produto
# ---------------------------------------------------------------------------
NOTES_FILE = ROOT / "PRODUTOS.txt"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
DOMAIN_PLATFORM = {
    "teacherspayteachers.com": "tpt", "tes.com": "tes", "etsy.com": "etsy",
    "notion.so": "notion", "notion.com": "notion", "notion.site": "notion",
}


def normalize_store_url(url: str) -> str:
    """Link canônico: sem ?ref=/#, sem idioma na URL (/pt/, /pt-BR/...) e, no Etsy, só /listing/<id>.
    Assim cada visitante do Pinterest vê a página no próprio idioma e nada é duplicado."""
    url = url.strip().split("?")[0].split("#")[0].rstrip("/")
    parts = urlsplit(url)
    path = re.sub(r"^/[a-z]{2}(?:-[a-zA-Z]{2})?(?=/)", "", parts.path)  # remove /pt, /pt-BR, /de...
    host = parts.netloc.lower()
    m = re.match(r"^/listing/(\d+)", path)
    if "etsy.com" in host and m:
        return f"https://www.etsy.com/listing/{m.group(1)}"
    return f"https://{host}{path}"


def url_of(p: dict) -> str:
    raw = str(p.get("url") or p.get("link") or p.get("product_url") or "")
    return normalize_store_url(raw) if raw else ""


def platform_from_url(url: str) -> str | None:
    host = urlsplit(url).netloc.lower()
    return next((plat for dom, plat in DOMAIN_PLATFORM.items() if host == dom or host.endswith("." + dom)), None)


def title_from_url(url: str) -> str:
    """Último recurso quando a loja bloqueia leitura: o endereço costuma conter o nome do produto."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    slug = max(parts, key=lambda p: sum(c == "-" for c in p)) if parts else ""
    words = [w for w in slug.split("-") if w and not (w.isdigit() and len(w) >= 4)]
    return " ".join(w if w.isupper() else w.capitalize() for w in words)


def read_notes() -> list[dict]:
    if not NOTES_FILE.exists():
        return []
    entries = []
    for n, line in enumerate(NOTES_FILE.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|")]
        url_idx = next((i for i, f in enumerate(fields) if re.match(r"^https?://", f)), None)
        if url_idx is None:
            if "|" in line:  # parecia um produto, mas faltou o link; títulos de seção ("TPT:") são ignorados
                log.warning("PRODUTOS.txt linha %d sem link (ignorada): %s", n, line[:80])
            continue
        url = fields[url_idx]
        platform = platform_from_url(url)
        if not platform:
            log.warning("PRODUTOS.txt linha %d: link de loja desconhecida (%s).", n, urlsplit(url).netloc)
            continue
        extras = fields[url_idx + 1:]
        image = next((f for f in extras if f.lower().split("?")[0].endswith(IMAGE_EXT)), "")
        notes = " ".join(f for f in extras if f and f != image)
        entries.append({
            "title": " ".join(fields[:url_idx]).strip(),
            "raw_url": url,  # guarda o slug (nome) que o Etsy tira do link canônico
            "url": normalize_store_url(url),
            "platform": platform, "image": image, "notes": notes,
        })
    return entries


def invalidate_copies(product_id: str) -> None:
    """Título real chegou depois (antes era deduzido do link): descarta copies antigas para o refill refazer."""
    from .config import COPY_CACHE_FILE
    if not COPY_CACHE_FILE.exists():
        return
    cache = json.loads(COPY_CACHE_FILE.read_text(encoding="utf-8"))
    stale = [k for k in cache if k.startswith(f"{product_id}:c")]
    for k in stale:
        del cache[k]
    if stale:
        COPY_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Copies de %s serão refeitas com o título real.", product_id)


def retitle(product: dict, title: str, summary: dict) -> None:
    product["title"] = title
    product.pop("title_from_url", None)
    summary["noted"].append(title)
    invalidate_copies(str(product.get("id", "")))


def apply_notes(items: list[dict], summary: dict) -> None:
    known = {url_of(p): p for p in items if url_of(p)}
    for e in read_notes():
        product = known.get(e["url"])
        if product:  # já existe: só completa nome/observações/imagem que faltarem
            if e["title"] and product.get("title_from_url"):
                retitle(product, e["title"], summary)
            if e["notes"] and not product.get("notes"):
                product["notes"] = e["notes"]
                summary["noted"].append(product.get("title", ""))
            if e["image"] and not str(product.get("image_url") or "").strip():
                product["image_url"] = e["image"]
                summary["noted"].append(product.get("title", ""))
            continue

        title = e["title"] or title_from_url(e["raw_url"])
        pending = [p for p in items if not url_of(p)
                   and normalize_platform(str(p.get("platform") or "")) == e["platform"]]
        best = max(pending, key=lambda p: similarity(title, str(p.get("title") or "")), default=None)
        if best is not None and similarity(title, str(best.get("title") or "")) >= 0.6:
            product = best  # é um produto que o agente criou e estava aguardando o link
            product.setdefault("id", make_id(e["platform"], str(product.get("title"))))
            summary["linked"].append((product.get("title", ""), e["url"]))
        else:
            product = {
                "id": make_id(e["platform"], title), "title": title, "description": "",
                "platform": e["platform"], "image_url": "", "tags": [],
                "created_at": date.today().isoformat() + "T00:00:00Z", "link_source": "notes",
            }
            if not e["title"]:
                product["title_from_url"] = True  # a varredura da loja troca pelo título real, se puder
            items.append(product)
            summary["imported"].append((title, e["url"]))
        product.update({"url": e["url"], "status": "active", "linked_at": date.today().isoformat()})
        for k in ("suggested_url", "suggested_title"):
            product.pop(k, None)
        if e["notes"]:
            product["notes"] = e["notes"]
        if e["image"] and not str(product.get("image_url") or "").strip():
            product["image_url"] = e["image"]
        known[e["url"]] = product
        log.info("[notas] ✓ %s -> %s", product["title"], e["url"])


def sync(import_unknown: bool | None = None) -> dict:
    """Lê o PRODUTOS.txt, varre as lojas e atualiza data/products.json. Retorna um resumo do que mudou."""
    cfg = json.loads(STORES_FILE.read_text(encoding="utf-8")) if STORES_FILE.exists() else {}
    if import_unknown is None:
        import_unknown = bool(cfg.get("import_unknown", True))
    stores = load_stores()
    data = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    items = data["products"] if isinstance(data, dict) else data
    summary = {"linked": [], "suggested": [], "imported": [], "noted": [], "blocked": {}}

    apply_notes(items, summary)  # suas anotações primeiro: têm prioridade sobre a varredura

    for platform, store_url in stores.items():
        try:
            listing = SCANNERS[platform](store_url)
        except StoreBlocked as exc:
            summary["blocked"][platform] = str(exc)
            log.info("[%s] %s", platform, exc)
            continue
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as exc:
            summary["blocked"][platform] = f"falha na leitura: {exc}"
            log.warning("[%s] falha ao ler a loja: %s", platform, exc)
            continue
        log.info("[%s] %d anúncios encontrados na loja.", platform, len(listing))

        same = [p for p in items if normalize_platform(str(p.get("platform") or "")) == platform]
        known_urls = {url_of(p) for p in same if url_of(p)}
        available = [s for s in listing if normalize_store_url(s.url) not in known_urls]

        # 0) completa produtos já conhecidos (ex.: anotados no PRODUTOS.txt) com capa e título da loja
        by_url = {normalize_store_url(s.url): s for s in listing}
        for p in same:
            s = by_url.get(url_of(p))
            if not s:
                continue
            if not str(p.get("image_url") or "").strip() and s.image:
                p["image_url"] = s.image
                summary["noted"].append(p.get("title", ""))
            if p.get("title_from_url") and s.title:
                retitle(p, s.title, summary)
            if not p.get("description") and s.description:
                p["description"] = s.description

        # 1) liga produtos pendentes ao anúncio mais parecido
        for p in same:
            if url_of(p) and str(p.get("status", "")).lower() != "pending":
                continue
            if not available:
                break
            title = str(p.get("title") or p.get("name") or "")
            ranked = sorted(available, key=lambda s: similarity(title, s.title), reverse=True)
            best, score = ranked[0], similarity(title, ranked[0].title)
            runner_up = similarity(title, ranked[1].title) if len(ranked) > 1 else 0.0
            ambiguous = score - runner_up < 0.06  # dois anúncios quase empatados: melhor você confirmar
            if score >= AUTO_MATCH and not ambiguous:
                p.setdefault("id", make_id(platform, title))
                p.update({"url": best.url, "status": "active", "linked_at": date.today().isoformat(),
                          "link_source": "store_sync"})
                p.pop("suggested_url", None)
                if not str(p.get("image_url") or "").strip() and best.image:
                    p["image_url"] = best.image
                available.remove(best)
                summary["linked"].append((title, best.url))
                log.info("[%s] ✓ ligado: '%s' -> %s (%.0f%%)", platform, title, best.url, score * 100)
            elif score >= SUGGEST:
                p["suggested_url"] = best.url
                p["suggested_title"] = best.title
                summary["suggested"].append((title, best.title, best.url))
                log.info("[%s] ? sugestão: '%s' ~ '%s' (%.0f%%)", platform, title, best.title, score * 100)

        # 2) importa anúncios da loja que ainda não estão no catálogo do bot
        if import_unknown:
            pending_titles = [str(p.get("title") or "") for p in same if not url_of(p)]
            for s in available:
                if any(similarity(t, s.title) >= SUGGEST for t in pending_titles):
                    continue  # provavelmente é um pendente aguardando sua confirmação
                items.append({
                    "id": make_id(platform, s.title), "title": s.title, "description": s.description,
                    "url": s.url, "platform": platform, "image_url": s.image, "price": s.price,
                    "status": "active", "created_at": date.today().isoformat() + "T00:00:00Z",
                    "tags": [], "link_source": "store_import",
                })
                summary["imported"].append((s.title, s.url))
                log.info("[%s] + importado da loja: '%s'", platform, s.title)

    if summary["linked"] or summary["suggested"] or summary["imported"] or summary["noted"]:
        PRODUCTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary
