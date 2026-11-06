"""Adiciona o link da loja aos produtos que os agentes criaram (status pendente) e os ativa no bot.

Fluxo: os agentes gravam o produto em data/products.json SEM link -> você publica na loja ->
roda este script, cola o link -> ele valida, completa a imagem (se faltar), ativa e faz push.

Uso:
    python scripts/add_links.py                    # modo interativo (lista os pendentes e pede os links)
    python scripts/add_links.py --list             # só lista os pendentes
    python scripts/add_links.py --set <id> <url>   # define um link direto (útil para automação)
    python scripts/add_links.py --no-git           # não faz pull/commit/push
No Windows também dá para dar dois cliques em adicionar_links.bat.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import PLATFORMS, PRODUCTS_FILE, normalize_platform  # noqa: E402
from src.ingestion import make_id  # noqa: E402
from src.store_sync import sync as sync_stores  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DOMAINS = {
    "tpt": ("teacherspayteachers.com",),
    "tes": ("tes.com",),
    "etsy": ("etsy.com",),
    "notion": ("notion.so", "notion.com", "notion.site"),
}
URL_KEYS = ("url", "link", "product_url", "product_link")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------------------
def load_raw() -> tuple[object, list[dict]]:
    data = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    items = data["products"] if isinstance(data, dict) else data
    return data, items


def save_raw(data: object) -> None:
    PRODUCTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def title_of(item: dict) -> str:
    return str(item.get("title") or item.get("name") or item.get("product_title") or "").strip()


def platform_of(item: dict) -> str:
    return normalize_platform(str(item.get("platform") or item.get("source") or item.get("store") or "etsy"))


def is_pending(item: dict) -> bool:
    has_url = any(str(item.get(k) or "").strip() for k in URL_KEYS)
    return item.get("active", True) is not False and (str(item.get("status", "")).lower() == "pending" or not has_url)


def clean_url(url: str) -> str:
    """Remove parâmetros de rastreio (?ref=, click_key, etc.) e fragmentos."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme or "https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def meta(html_text: str, prop: str) -> str:
    for pattern in (
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(prop)}["\']',
    ):
        m = re.search(pattern, html_text, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def inspect_page(url: str) -> dict:
    """Confere se a página existe e extrai título/imagem principal (og:title / og:image)."""
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    except requests.RequestException as exc:
        return {"ok": None, "note": f"não foi possível acessar ({exc.__class__.__name__}); link aceito sem verificação"}
    if r.status_code == 404 or r.status_code == 410:
        return {"ok": False, "note": f"página não encontrada ({r.status_code})"}
    if r.status_code in (403, 406, 429, 503):  # Etsy/TPT às vezes bloqueiam robôs: não é erro do link
        return {"ok": None, "note": f"a loja bloqueou a verificação automática ({r.status_code}); link aceito"}
    if r.status_code >= 400:
        return {"ok": False, "note": f"erro HTTP {r.status_code}"}
    return {"ok": True, "title": meta(r.text, "og:title"), "image": meta(r.text, "og:image"), "note": "página OK"}


def ask(prompt: str) -> str:
    """input() que trata janela fechada / fim da entrada como 'q' (sair)."""
    try:
        return input(prompt)
    except EOFError:
        return "q"


def apply_link(item: dict, raw_url: str, interactive: bool, verified: bool = False) -> bool:
    url = raw_url.strip()
    if not re.match(r"^https?://", url):
        print("   ✗ Link inválido (precisa começar com http:// ou https://).")
        return False
    url = clean_url(url) if "notion" not in url else url.strip()
    platform = platform_of(item)
    host = urlsplit(url).netloc.lower()
    if not any(host == d or host.endswith("." + d) for d in DOMAINS[platform]):
        print(f"   ⚠ O link é de '{host}', mas o produto é de {PLATFORMS[platform]['label']}.")
        if interactive and ask("   Usar mesmo assim? [s/N] ").strip().lower() not in {"s", "sim", "y"}:
            return False

    info = {"ok": True, "note": "link encontrado na sua loja"} if verified else inspect_page(url)
    print(f"   → {info['note']}")
    if info["ok"] is False:
        if not interactive or ask("   Salvar mesmo assim? [s/N] ").strip().lower() not in {"s", "sim", "y"}:
            return False
    if info.get("title"):
        print(f"   → Título na loja: {info['title']}")

    item.setdefault("id", make_id(platform, title_of(item)))  # fixa o ID para o histórico nunca se perder
    for k in URL_KEYS[1:]:
        item.pop(k, None)
    item["url"] = url
    item["status"] = "active"
    item["linked_at"] = date.today().isoformat()
    has_image = any(str(item.get(k) or "").strip() for k in ("image_url", "mockup_url", "image", "mockup"))
    if not has_image and info.get("image"):
        item["image_url"] = info["image"]
        print("   → Sem mockup do agente: usando a imagem principal do anúncio.")
    print("   ✓ Produto ativado no bot.")
    return True


# ---------------------------------------------------------------------------
def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def commit_and_push(count: int) -> None:
    git("add", str(PRODUCTS_FILE.relative_to(ROOT)))
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", f"chore(products): add store links to {count} product(s)")
    for _ in range(3):
        git("pull", "--rebase", "--autostash")
        if git("push").returncode == 0:
            print(f"\n✓ Enviado ao GitHub: {count} produto(s) entram na rotação na próxima execução do bot.")
            return
    print("\n⚠ Não foi possível fazer push agora; o commit ficou salvo e vai na próxima vez.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--set", nargs=2, metavar=("ID", "URL"))
    parser.add_argument("--no-git", action="store_true")
    parser.add_argument("--no-sync", action="store_true", help="não varrer as lojas antes")
    args = parser.parse_args()

    use_git = not args.no_git and (ROOT / ".git").exists()
    if use_git:
        git("pull", "--rebase", "--autostash")
    auto_linked = 0
    if not args.no_sync and not args.set:
        print("Verificando suas lojas (TES, Notion, Etsy)...")
        summary = sync_stores()
        auto_linked = len(summary["linked"]) + len(summary["imported"])
        for title, _ in summary["linked"]:
            print(f"  ✓ ligado automaticamente: {title}")
        for title, _ in summary["imported"]:
            print(f"  + importado da loja: {title}")

    data, items = load_raw()
    pending = sorted((i for i in items if is_pending(i)), key=lambda i: str(i.get("created_at", "")))

    if args.set:
        pid, url = args.set
        match = [i for i in items if str(i.get("id")) == pid or make_id(platform_of(i), title_of(i)) == pid]
        if not match:
            print(f"Produto '{pid}' não encontrado.")
            return 1
        print(f"• {title_of(match[0])}")
        if not apply_link(match[0], url, interactive=False):
            return 1
        save_raw(data)
        if use_git:
            commit_and_push(1)
        return 0

    if not pending:
        print("Nenhum produto aguardando link. 🎉")
        if auto_linked and use_git:
            commit_and_push(auto_linked)
        return 0

    print(f"\n{len(pending)} produto(s) aguardando link:\n")
    for n, item in enumerate(pending, 1):
        pid = item.get("id") or make_id(platform_of(item), title_of(item))
        print(f"  [{n}] {PLATFORMS[platform_of(item)]['label']:<22} {title_of(item)}  (id: {pid})")
    if args.list:
        return 0

    print("\nCole o link de cada produto já publicado. Enter = pular (ou aceitar a sugestão) | - = pular | q = sair\n")
    done = 0
    for n, item in enumerate(pending, 1):
        print(f"[{n}/{len(pending)}] {title_of(item)}  ({PLATFORMS[platform_of(item)]['label']})")
        suggestion = str(item.get("suggested_url") or "")
        if suggestion:
            print(f"   Sugestão da loja: {item.get('suggested_title', '')}")
            print(f"   {suggestion}")
        while True:
            answer = ask("   Link (Enter aceita a sugestão): " if suggestion else "   Link: ").strip()
            if answer.lower() == "q":
                break
            if answer == "-" or (not answer and not suggestion):
                print("   (pulado)")
                break
            answer = answer or suggestion
            if apply_link(item, answer, interactive=True, verified=bool(suggestion) and answer == suggestion):
                item.pop("suggested_url", None)
                item.pop("suggested_title", None)
                done += 1
                save_raw(data)  # salva a cada produto: nada se perde se fechar a janela
                break
        if answer.lower() == "q":
            break

    if (done or auto_linked) and use_git:
        commit_and_push(done + auto_linked)
    elif done:
        print(f"\n✓ {done} produto(s) ativado(s) (sem git).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
