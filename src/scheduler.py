"""Seleção de quais produtos/variações publicar: rotação aleatória ponderada por desempenho.

Regras:
- Nunca publica o mesmo produto em sequência (exclui os últimos N produtos publicados).
- Respeita um cooldown mínimo por produto (relaxado se não houver alternativas).
- Produtos novos ou nunca publicados recebem impulso para entrar rápido na rotação.
- Com o tempo, produtos com mais visualizações/vendas/cliques ganham mais peso.
- Cada produto tem de 3 a 5 variações; quando todas são usadas, abre-se um novo ciclo
  (novas copies + novo sorteio de templates/paletas), evitando repetir Pins idênticos.
"""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from .config import settings
from .history import History, now_utc
from .ingestion import Product
from .logger import get_logger
from .seo_copywriter import copy_ready

log = get_logger("scheduler")
_rng = random.SystemRandom()


@dataclass
class PinJob:
    product: Product
    cycle: int
    variant_index: int
    variant_count: int

    @property
    def variant_id(self) -> str:
        return f"{self.product.id}:c{self.cycle}:v{self.variant_index}"


def variant_count_for(product_id: str) -> int:
    lo, hi = sorted((max(1, settings.variants_min), max(1, settings.variants_max)))
    h = int(hashlib.md5(product_id.encode()).hexdigest(), 16)
    return lo + h % (hi - lo + 1)


def _performance_scores(products: list[Product]) -> dict[str, float]:
    """Score 0..1 combinando vendas (peso maior), cliques e visualizações em escala logarítmica."""
    raw = {}
    for p in products:
        s = p.stats
        raw[p.id] = 3.0 * math.log1p(s.sales) + 1.5 * math.log1p(s.clicks + s.favorites) + 1.0 * math.log1p(s.views)
    top = max(raw.values(), default=0) or 1.0
    return {pid: v / top for pid, v in raw.items()}


def _weight(product: Product, perf: float, history: History) -> float:
    state = history.product_state(product.id)
    weight = 0.6 + 2.4 * perf  # desempenho domina conforme os dados chegam

    created = product.created_datetime()
    if state["posts"] == 0:
        weight *= 3.0  # nunca publicado: entra logo
    elif created and (now_utc() - created).days <= settings.new_product_boost_days:
        weight *= 1.8  # lançamento recente

    # Fadiga: quanto mais publicado, menor o peso (amortecido pelo desempenho)
    weight /= 1 + state["posts"] * 0.08 * (1 - 0.7 * perf)

    # Quanto mais tempo sem aparecer, maior o peso (até 3x)
    last = history.last_posted(product.id)
    if last:
        hours = (now_utc() - last).total_seconds() / 3600
        weight *= min(3.0, 0.5 + hours / 48)
    return max(weight, 0.01)


def upcoming_cycle(product_id: str, history: History) -> int:
    """Ciclo da próxima publicação do produto, sem alterar o histórico."""
    state = history.product_state(product_id)
    used = set(state["variants_used"])
    return state["cycle"] + (1 if used >= set(range(variant_count_for(product_id))) else 0)


def _has_ready_copy(product: Product, history: History) -> bool:
    return copy_ready(product.id, upcoming_cycle(product.id, history), variant_count_for(product.id))


def _next_variant(product: Product, history: History) -> tuple[int, int, int]:
    count = variant_count_for(product.id)
    state = history.product_state(product.id)
    unused = [i for i in range(count) if i not in state["variants_used"]]
    if not unused:
        cycle = history.start_new_cycle(product.id)
        log.info("Produto '%s': todas as %d variações usadas, iniciando ciclo %d.", product.title, count, cycle)
        unused = list(range(count))
    return state["cycle"], _rng.choice(unused), count


def select_jobs(products: list[Product], history: History, n: int) -> list[PinJob]:
    if not products:
        return []

    eligible = [
        p for p in products
        if history.product_state(p.id).get("failures", 0) < settings.max_consecutive_failures
    ]
    if not eligible:
        log.error("Todos os produtos atingiram o limite de falhas consecutivas. Verifique os logs/links.")
        return []

    # Evita repetir os últimos produtos (mas nunca bloqueia tudo quando o catálogo é pequeno)
    avoid_n = min(settings.avoid_last_n_products, max(0, len(eligible) - n))
    recent = set(history.recent_product_ids(avoid_n)) if avoid_n else set()

    def cooled(p: Product) -> bool:
        last = history.last_posted(p.id)
        return not last or (now_utc() - last).total_seconds() / 3600 >= settings.product_cooldown_hours

    # Fila de copies gerada pelo seu PC: com política "skip", só entra quem tem copy pronta
    if settings.empty_queue_policy == "skip":
        eligible = [p for p in eligible if _has_ready_copy(p, history)]
        if not eligible:
            log.warning("Nenhum produto com copy pronta na fila. Ligue o PC para o refill (scripts/local_refill.py).")
            return []

    pool = [p for p in eligible if p.id not in recent and cooled(p)]
    if len(pool) < n:
        pool = [p for p in eligible if p.id not in recent] or eligible
        log.info("Poucos produtos fora do cooldown; regra de cooldown relaxada nesta execução.")

    perf = _performance_scores(eligible)
    weights = {p.id: _weight(p, perf[p.id], history) for p in pool}
    for p in pool:  # sem copy da IA pronta: ainda pode sair (template), mas com bem menos chance
        if not _has_ready_copy(p, history):
            weights[p.id] *= 0.25

    chosen: list[Product] = []
    candidates = list(pool)
    while candidates and len(chosen) < n:
        pick = _rng.choices(candidates, weights=[weights[c.id] for c in candidates], k=1)[0]
        chosen.append(pick)
        candidates.remove(pick)

    jobs = []
    for product in chosen:
        cycle, idx, count = _next_variant(product, history)
        jobs.append(PinJob(product=product, cycle=cycle, variant_index=idx, variant_count=count))
        log.info(
            "Selecionado: '%s' [%s] variação %d/%d (ciclo %d, peso %.2f, perf %.2f)",
            product.title, product.platform, idx + 1, count, cycle, weights[product.id], perf[product.id],
        )
    return jobs
