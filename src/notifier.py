"""Avisos de ERRO no celular via ntfy (https://ntfy.sh). Nada é enviado quando tudo vai bem.

- NTFY_TOPIC: nome do tópico (funciona como senha; assine o mesmo nome no app ntfy)
- Cada problema tem uma "chave": o mesmo problema só é avisado de novo depois de 24 h,
  para não encher o celular enquanto ele não é resolvido.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .config import DATA_DIR, ROOT
from .logger import get_logger

log = get_logger("notifier")

# Cada ambiente grava o próprio controle (sem conflito de Git entre PC e GitHub)
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
STATE_FILE = DATA_DIR / "notify_state.json" if IN_ACTIONS else ROOT / "logs" / "notify_state.json"
REPEAT_AFTER = timedelta(hours=24)


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def notify_error(key: str, title: str, message: str, click: str = "", priority: str = "high") -> bool:
    """Envia o aviso (uma vez a cada 24 h por chave). Retorna True se enviou."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        log.debug("NTFY_TOPIC não configurado; aviso não enviado: %s", title)
        return False

    state = _load_state()
    now = datetime.now(timezone.utc)
    last = state.get(key)
    if last and now - datetime.fromisoformat(last) < REPEAT_AFTER:
        return False

    origin = "GitHub" if IN_ACTIONS else "PC"
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": f"Pinterest Bot ({origin}): {title}".encode("utf-8"),
        "Priority": priority,
        "Tags": "warning",
    }
    if click:
        headers["Click"] = click
    elif IN_ACTIONS and os.getenv("GITHUB_RUN_ID"):
        headers["Click"] = (f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
                            f"{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.getenv('GITHUB_RUN_ID')}")
    try:
        r = requests.post(f"{server}/{topic}", data=message[:3500].encode("utf-8"), headers=headers, timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Falha ao enviar aviso ntfy: %s", exc)
        return False

    if IN_ACTIONS:  # o workflow não manda o aviso genérico de falha se já houve um específico
        Path(os.getenv("RUNNER_TEMP", "/tmp"), "pinbot_notified").touch()
    state[key] = now.isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    log.info("Aviso de erro enviado ao celular: %s", title)
    return True


def clear(key: str) -> None:
    """Problema resolvido: permite avisar de novo na próxima vez que acontecer."""
    state = _load_state()
    if state.pop(key, None) is not None:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
