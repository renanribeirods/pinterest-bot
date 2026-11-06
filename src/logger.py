"""Logging com anotações nativas do GitHub Actions (::warning:: / ::error::) e resumo do job."""
from __future__ import annotations

import logging
import os
import sys

IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"


class _ActionsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if IN_ACTIONS and record.levelno >= logging.ERROR:
            return f"::error::{msg}"
        if IN_ACTIONS and record.levelno == logging.WARNING:
            return f"::warning::{msg}"
        return msg


def get_logger(name: str = "pinbot") -> logging.Logger:
    try:  # console do Windows (cp1252) não exibe acentos sem isso
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_ActionsFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return logger


def write_step_summary(markdown: str) -> None:
    """Escreve no painel 'Summary' da execução do workflow."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown + "\n")
