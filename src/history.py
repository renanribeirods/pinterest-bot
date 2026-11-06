"""Estado persistente (data/history.json): o que já foi publicado, ciclos de variações e falhas."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import HISTORY_FILE

MAX_POSTS_KEPT = 5000


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class History:
    def __init__(self, path: Path = HISTORY_FILE):
        self.path = path
        self.data = {"version": 1, "posts": [], "products": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except json.JSONDecodeError:
                backup = path.with_suffix(".corrupt.json")
                path.replace(backup)

    # -- consultas -------------------------------------------------------
    @property
    def posts(self) -> list[dict]:
        return self.data["posts"]

    def product_state(self, product_id: str) -> dict:
        return self.data["products"].setdefault(
            product_id,
            {"posts": 0, "cycle": 0, "variants_used": [], "last_posted": None, "failures": 0},
        )

    def last_posted(self, product_id: str) -> datetime | None:
        raw = self.data["products"].get(product_id, {}).get("last_posted")
        return datetime.fromisoformat(raw) if raw else None

    def recent_product_ids(self, n: int) -> list[str]:
        """IDs dos últimos N produtos publicados (do mais recente para o mais antigo)."""
        seen: list[str] = []
        for post in reversed(self.posts):
            pid = post["product_id"]
            if pid not in seen:
                seen.append(pid)
            if len(seen) >= n:
                break
        return seen

    def is_published(self, variant_id: str) -> bool:
        return any(p["variant_id"] == variant_id for p in self.posts)

    # -- mutações --------------------------------------------------------
    def record_post(self, product_id: str, variant_id: str, variant_index: int, **meta) -> None:
        state = self.product_state(product_id)
        state["posts"] += 1
        state["failures"] = 0
        state["last_posted"] = now_utc().isoformat()
        if variant_index not in state["variants_used"]:
            state["variants_used"].append(variant_index)
        self.posts.append({
            "product_id": product_id,
            "variant_id": variant_id,
            "published_at": state["last_posted"],
            **meta,
        })
        del self.posts[:-MAX_POSTS_KEPT]

    def start_new_cycle(self, product_id: str) -> int:
        state = self.product_state(product_id)
        state["cycle"] += 1
        state["variants_used"] = []
        return state["cycle"]

    def record_failure(self, product_id: str, error: str) -> int:
        state = self.product_state(product_id)
        state["failures"] += 1
        state["last_error"] = error[:300]
        return state["failures"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
