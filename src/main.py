"""Orquestrador: seleciona produtos -> gera copy -> gera Pin -> hospeda -> publica -> grava histórico.

Uso:
    python -m src.main                 # execução normal (agendada no GitHub Actions)
    python -m src.main --dry-run       # gera imagens/copies sem publicar
    python -m src.main --count 3 --no-jitter
    python -m src.main --preview <product_id>   # renderiza todas as variações de um produto
"""
from __future__ import annotations

import argparse
import random
import sys
import time

from .config import settings
from .history import History
from .image_generator import generate_pin
from .image_host import host_image
from .ingestion import load_products
from .logger import get_logger, write_step_summary
from .pinterest_publisher import PinPayload, Publisher
from .scheduler import select_jobs, variant_count_for
from . import seo_copywriter
from .notifier import clear as clear_alert, notify_error
from .seo_copywriter import CopyNotReady, get_copy

log = get_logger("main")
_rng = random.SystemRandom()


def preview(product_id: str) -> int:
    products = {p.id: p for p in load_products()}
    product = products.get(product_id)
    if not product:
        log.error("Produto '%s' não encontrado. IDs: %s", product_id, ", ".join(products))
        return 1
    n = variant_count_for(product.id)
    for idx in range(n):
        copy = get_copy(product, 0, idx, n)
        path, template = generate_pin(product, copy, 0, idx)
        print(f"[{idx + 1}/{n}] {template}: {path}\n    {copy.title}")
    return 0


def run(count: int, dry_run: bool, jitter: bool) -> int:
    if jitter and settings.jitter_max_minutes > 0:
        wait = _rng.randint(0, settings.jitter_max_minutes * 60)
        log.info("Aguardando %d s de jitter para um padrão de postagem mais orgânico.", wait)
        time.sleep(wait)

    history = History()
    products = load_products()
    if not products:
        log.warning("Nenhum produto em data/products.json. Nada a fazer.")
        return 0

    publisher = Publisher(dry_run=dry_run)
    jobs = select_jobs(products, history, count)
    published, failed = [], []

    for i, job in enumerate(jobs):
        if publisher.exhausted:
            log.error("Todos os canais de publicação estão indisponíveis. Pins restantes ficam para a próxima execução.")
            break
        product = job.product
        if history.is_published(job.variant_id):
            log.info("Variação %s já publicada; pulando.", job.variant_id)
            continue
        try:
            copy = get_copy(product, job.cycle, job.variant_index, job.variant_count)
            image_path, template = generate_pin(product, copy, job.cycle, job.variant_index)
        except CopyNotReady as exc:
            log.info("%s Fica para a próxima execução.", exc)
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha ao preparar o Pin de '%s': %s", product.title, exc)
            history.record_failure(product.id, str(exc))
            history.save()
            failed.append((product.title, str(exc)))
            continue

        image_url = host_image(image_path) if (publisher.needs_hosted_image and not dry_run) else None
        payload = PinPayload(
            board_id=settings.board_for(product.platform, product.board_id),
            title=copy.title,
            description=copy.full_description(),
            link=product.url,
            alt_text=copy.alt_text,
            image_path=image_path,
            image_url=image_url,
            platform=product.platform,
            locale=product.locale,
            product_id=product.id,
            variant_id=job.variant_id,
            extra={"template": template, "keywords": copy.keywords},
        )
        result = publisher.publish(payload)

        if result.ok and not dry_run:
            history.record_post(
                product.id, job.variant_id, job.variant_index,
                platform=product.platform, template=template, channel=result.channel,
                pin_id=result.pin_id, pin_url=result.pin_url, title=copy.title, image_url=image_url,
            )
            published.append((product.title, template, result.channel, result.pin_url))
        elif result.ok:
            published.append((product.title, template, "dryrun", str(image_path)))
        else:
            # Falha de canal não é culpa do produto: só conta falha se nenhum canal estiver fora do ar
            if not publisher.disabled:
                history.record_failure(product.id, result.error)
            failed.append((product.title, result.error))
        if not dry_run:
            history.save()

        if i < len(jobs) - 1 and not dry_run and result.ok:
            pause = _rng.randint(*sorted(settings.delay_between_pins))
            log.info("Pausa de %d s antes do próximo Pin.", pause)
            time.sleep(pause)

    _summary(published, failed, publisher.disabled, dry_run)
    if not dry_run:
        _alerts(published, failed, publisher.disabled, history, products)
    if jobs and not published and not dry_run:
        return 1  # faz o workflow ficar vermelho e o GitHub enviar e-mail de alerta
    return 0


def _alerts(published, failed, disabled, history, products) -> None:
    """Só avisa no celular quando algo dá errado (cada problema no máximo 1x a cada 24 h)."""
    hints = {
        "webhook": "Abra o Make e confira se o cenário 'Pinterest Bot - publicar Pins' está ativo e sem erros.",
        "api": "Token da API do Pinterest expirado ou sem permissão.",
    }
    for name, reason in disabled.items():
        notify_error(f"channel:{name}", f"canal '{name}' fora do ar",
                     f"{reason}\n\nO que fazer: {hints.get(name, 'veja o log da execução.')}")
    if failed:
        notify_error("pin_failures", f"{len(failed)} Pin(s) não publicado(s)",
                     "\n".join(f"- {t[:60]}: {e[:160]}" for t, e in failed)
                     + "\n\nEles voltam para a fila e serão tentados de novo.")
    elif published:
        clear_alert("pin_failures")
        for name in ("webhook", "api"):
            if name not in disabled:
                clear_alert(f"channel:{name}")

    if seo_copywriter.TEMPLATE_FALLBACKS:
        notify_error("queue_empty", "fila de copies do PC acabou",
                     "Pins saíram com copy simples (sem IA): "
                     + ", ".join(t[:40] for t in seo_copywriter.TEMPLATE_FALLBACKS)
                     + "\n\nO que fazer: ligue o PC (o refill roda sozinho a cada 4 h) ou veja logs/refill.log.")
    else:
        clear_alert("queue_empty")

    by_id = {p.id: p for p in products}
    for pid, state in history.data["products"].items():
        if pid in by_id and state.get("failures", 0) >= settings.max_consecutive_failures:
            notify_error(f"product_disabled:{pid}", "produto pausado após falhas seguidas",
                         f"{by_id[pid].title}\n{by_id[pid].url}\n\nÚltimo erro: {state.get('last_error', '')[:200]}"
                         "\n\nO que fazer: confira se o link do produto ainda funciona.")


def _summary(published, failed, disabled, dry_run) -> None:
    lines = [f"## 📌 Pinterest Bot {'(dry-run)' if dry_run else ''}", ""]
    lines += [f"**Publicados:** {len(published)} · **Falhas:** {len(failed)}", ""]
    if published:
        lines += ["| Produto | Template | Canal | Pin |", "|---|---|---|---|"]
        lines += [f"| {t} | {tpl} | {ch} | {url} |" for t, tpl, ch, url in published]
    if failed:
        lines += ["", "### Falhas"] + [f"- **{t}**: {e}" for t, e in failed]
    if disabled:
        lines += ["", "### Canais desativados nesta execução"] + [f"- `{k}`: {v}" for k, v in disabled.items()]
    md = "\n".join(lines)
    write_step_summary(md)
    log.info("Resumo: %d publicados, %d falhas.", len(published), len(failed))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot autônomo de Pins para Pinterest")
    parser.add_argument("--count", type=int, default=settings.pins_per_run)
    parser.add_argument("--dry-run", action="store_true", default=settings.dry_run)
    parser.add_argument("--no-jitter", action="store_true")
    parser.add_argument("--preview", metavar="PRODUCT_ID")
    args = parser.parse_args()

    if args.preview:
        return preview(args.preview)
    try:
        return run(args.count, args.dry_run, jitter=not args.no_jitter)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro inesperado: %s", exc)
        notify_error("crash", "o bot parou com um erro inesperado", f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
