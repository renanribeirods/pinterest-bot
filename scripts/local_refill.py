"""Refill LOCAL da fila de copies usando o Claude Code da sua conta (assinatura), no seu PC.

Roda pelo Agendador de Tarefas do Windows (ver scripts/setup_windows_task.ps1):
  1. git pull (pega produtos novos e o histórico atualizado pelo GitHub Actions)
  2. mede quantos dias de Pins ainda há na fila de copies prontas
  3. só se a fila estiver baixa (ou houver produto novo) chama `claude -p` em modo headless
  4. grava data/copy_cache.json, faz commit e push

Uso:
    python scripts/local_refill.py            # execução normal (verifica a fila antes)
    python scripts/local_refill.py --force    # gera tudo o que estiver faltando, ignorando o limiar
    python scripts/local_refill.py --status   # só mostra o tamanho da fila
    python scripts/local_refill.py --no-git   # não faz pull/commit/push
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import COPY_CACHE_FILE, PRODUCTS_FILE, settings  # noqa: E402
from src.store_sync import sync as sync_stores  # noqa: E402
from src.pin_designer import (  # noqa: E402
    DESIGN_SCHEMA, DESIGN_SYSTEM_PROMPT, DESIGNS_DIR, build_design_prompt, design_path, designs_ready, render_html,
)
from src.history import History  # noqa: E402
from src.ingestion import Product, load_products  # noqa: E402
from src.logger import get_logger  # noqa: E402
from src.scheduler import upcoming_cycle, variant_count_for  # noqa: E402
from src.seo_copywriter import (  # noqa: E402
    COPY_SCHEMA, SYSTEM_PROMPT, build_prompt, cache_key, copy_ready, load_cache, save_cache,
)

LOG_DIR = ROOT / "logs"
LOCK_FILE = ROOT / "logs" / "refill.lock"
PREVIEW_DIR = ROOT / "output" / "previews"   # prévias das artes (não vão para o Git)
log = get_logger("refill")


class UsageLimitReached(RuntimeError):
    pass


def _pid_alive(pid: str) -> bool:
    """A trava só vale se o processo que a criou ainda existe (PC desligado/processo morto deixa trava órfã)."""
    if not pid.isdigit():
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return pid in r.stdout
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _setup_file_log() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "refill.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------
def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def git_pull() -> None:
    r = git("pull", "--rebase", "--autostash", check=False)
    if r.returncode != 0:
        log.warning("git pull falhou: %s", (r.stderr or r.stdout).strip()[:300])


def git_push_cache() -> None:
    git("add", str(COPY_CACHE_FILE.relative_to(ROOT)), str(PRODUCTS_FILE.relative_to(ROOT)),
        str(DESIGNS_DIR.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("Nada novo para enviar.")
        return
    git("commit", "-m", "chore(refill): store sync + new pin copies from local Claude Code")
    for attempt in range(3):
        git("pull", "--rebase", "--autostash", check=False)
        if git("push", check=False).returncode == 0:
            log.info("Fila de copies enviada para o GitHub.")
            return
        log.warning("Push falhou (tentativa %d).", attempt + 1)
    log.error("Não foi possível fazer push. O commit ficou local e será enviado na próxima execução.")


# ---------------------------------------------------------------------------
# Claude Code (headless)
# ---------------------------------------------------------------------------
def find_claude_bin() -> str:
    if settings.claude_bin:
        return settings.claude_bin
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    exe = "claude.exe" if os.name == "nt" else "claude"
    candidates = [home / ".local" / "bin" / exe]
    # binário que vem junto com a extensão do VS Code (pega a versão mais nova)
    for ext_dir in (home / ".vscode" / "extensions", home / ".cursor" / "extensions"):
        candidates += sorted(
            ext_dir.glob(f"anthropic.claude-code-*/resources/native-binary/{exe}"),
            key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parts[-4].split("-")[2].split(".")],
            reverse=True,
        )
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError("Claude Code não encontrado. Instale-o ou defina CLAUDE_BIN no .env.")


def run_claude(claude_bin: str, prompt: str, system: str, schema: dict, timeout: int = 600) -> dict:
    """Chamada headless ao Claude Code da sua conta, com saída JSON validada pelo schema."""
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--tools", "",                     # sem ferramentas: só escrever
        "--no-session-persistence",
        "--model", settings.local_claude_model,
    ]
    with tempfile.TemporaryDirectory() as tmp:  # pasta vazia: sem CLAUDE.md/projeto no contexto
        r = subprocess.run(cmd, cwd=tmp, input="", capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        msg = (r.stderr or r.stdout).strip()[:400]
        if "limit" in msg.lower():
            raise UsageLimitReached(msg)
        raise RuntimeError(f"Saída inesperada do Claude Code (rc={r.returncode}): {msg}")

    if data.get("is_error") or data.get("subtype") != "success":
        msg = str(data.get("result") or data.get("api_error_status") or data)[:400]
        if "limit" in msg.lower() or data.get("api_error_status") == 429:
            raise UsageLimitReached(msg)
        raise RuntimeError(f"Claude Code retornou erro: {msg}")
    return data.get("structured_output") or json.loads(data.get("result") or "{}")


def generate_copies(claude_bin: str, product: Product, n: int, cycle: int) -> list[dict]:
    variants = run_claude(claude_bin, build_prompt(product, n, cycle), SYSTEM_PROMPT, COPY_SCHEMA).get("variants") or []
    if len(variants) < n:
        raise RuntimeError(f"Claude Code retornou {len(variants)} variações (esperado {n}).")
    return variants[:n]


def generate_designs(claude_bin: str, product: Product, copies: list[dict], cycle: int) -> int:
    """Pede ao Claude os designs (HTML) das variações, valida renderizando cada um e grava os que passarem."""
    out = run_claude(claude_bin, build_design_prompt(product, copies, cycle), DESIGN_SYSTEM_PROMPT,
                     DESIGN_SCHEMA, timeout=900)
    designs = out.get("designs") or []
    saved = 0
    for i, d in enumerate(designs[: len(copies)]):
        html = (d.get("html") or "").strip()
        if "<html" not in html.lower():
            log.warning("  design %d de '%s' veio sem HTML válido.", i + 1, product.title)
            continue
        path = design_path(product.id, cycle, i)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        try:  # renderiza já no PC: design quebrado nunca vai para a fila
            render_html(path, PREVIEW_DIR / f"{product.id}_c{cycle}_v{i}.jpg")
            saved += 1
            log.info("  v%d: %s", i + 1, d.get("concept", "")[:60])
        except RuntimeError as exc:
            path.unlink(missing_ok=True)
            log.warning("  design %d de '%s' descartado: %s", i + 1, product.title, exc)
    return saved


# ---------------------------------------------------------------------------
# Fila
# ---------------------------------------------------------------------------
def analyse_queue(products: list[Product], history: History):
    """Retorna (dias_de_fila, pendências[(produto, ciclo, n, é_novo)])."""
    ready_pins, needs = 0, []
    for p in products:
        n = variant_count_for(p.id)
        state = history.product_state(p.id)
        cycle = upcoming_cycle(p.id, history)
        used = set(state["variants_used"]) if cycle == state["cycle"] else set()
        remaining = n - len(used)

        if copy_ready(p.id, cycle, n):
            ready_pins += remaining
        if not (copy_ready(p.id, cycle, n) and (designs_ready(p.id, cycle, n) or not settings.ai_designs)):
            needs.append((p, cycle, n, state["posts"] == 0 and cycle == 0))
        # adianta o próximo ciclo quando o atual está acabando
        if remaining <= 1:
            if copy_ready(p.id, cycle + 1, n):
                ready_pins += n
            if not (copy_ready(p.id, cycle + 1, n) and (designs_ready(p.id, cycle + 1, n) or not settings.ai_designs)):
                needs.append((p, cycle + 1, n, False))

    per_day = max(1, settings.pins_per_run * settings.runs_per_day)
    return ready_pins / per_day, needs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--no-sync", action="store_true", help="não varrer as lojas (data/stores.json)")
    args = parser.parse_args()
    _setup_file_log()

    if LOCK_FILE.exists():
        if _pid_alive(LOCK_FILE.read_text().strip()):
            log.info("Outro refill já está rodando (%s). Saindo.", LOCK_FILE)
            return 0
        log.info("Trava antiga de um refill interrompido encontrada; removendo.")
    LOCK_FILE.write_text(str(os.getpid()))
    use_git, generated, synced = not args.no_git and (ROOT / ".git").exists(), 0, False
    try:
        if use_git:
            git_pull()
        if not args.no_sync and not args.status:
            # Liga produtos pendentes aos anúncios das suas lojas e importa os que faltam
            summary = sync_stores()
            synced = bool(summary["linked"] or summary["suggested"] or summary["imported"] or summary["noted"])

        products, history = load_products(include_pending=True), History()  # adianta copies dos pendentes
        days, needs = analyse_queue(products, history)
        new_products = [x for x in needs if x[3]]
        log.info("Fila: %.1f dias de Pins prontos | %d conjuntos pendentes (%d produtos novos).",
                 days, len(needs), len(new_products))
        if args.status:
            for p, cycle, n, is_new in needs:
                log.info("  pendente: %s ciclo %d (%d variações)%s", p.id, cycle, n, " [NOVO]" if is_new else "")
            return 0
        if not needs:
            log.info("Nada a gerar.")
            return 0
        if days >= settings.refill_min_days and not new_products and not args.force:
            log.info("Fila acima de %.0f dias: nada a fazer agora.", settings.refill_min_days)
            return 0

        # Prioridade: produtos novos primeiro, depois quem tem menos fila (ciclo atual antes do próximo)
        needs.sort(key=lambda x: (not x[3], x[1] - history.product_state(x[0].id)["cycle"]))
        claude_bin = find_claude_bin()
        log.info("Usando Claude Code: %s (modelo %s)", claude_bin, settings.local_claude_model)

        cache = load_cache()
        for product, cycle, n, _ in needs[: settings.refill_max_products]:
            key = cache_key(product.id, cycle)
            for attempt in range(2):
                try:
                    if not copy_ready(product.id, cycle, n):
                        variants = generate_copies(claude_bin, product, n, cycle)
                        cache[key] = {
                            "source": f"claude-code:{settings.local_claude_model}",
                            "generated_at": date.today().isoformat(),
                            "variants": variants,
                        }
                        save_cache(cache)
                        generated += 1
                        log.info("✓ %s (ciclo %d): %d copies [%s]", product.title, cycle, n, product.locale)
                    if settings.ai_designs and not designs_ready(product.id, cycle, n):
                        saved = generate_designs(claude_bin, product, cache[key]["variants"][:n], cycle)
                        generated += 1 if saved else 0
                        log.info("✓ %s (ciclo %d): %d/%d artes criadas", product.title, cycle, saved, n)
                    break
                except UsageLimitReached as exc:
                    log.warning("Limite de uso da assinatura atingido; continuo na próxima execução. (%s)", exc)
                    raise
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    log.warning("Falha em '%s' (tentativa %d): %s", product.title, attempt + 1, exc)
    except UsageLimitReached:
        pass
    finally:
        if use_git and (generated or synced):
            git_push_cache()
        LOCK_FILE.unlink(missing_ok=True)

    log.info("Refill concluído: %d conjuntos gerados.", generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
