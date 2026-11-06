"""Hospeda a imagem do Pin em uma URL pública (exigido por Make/Zapier/IFTTT via webhook).

Opções (IMAGE_HOST):
- github: envia para a branch `pin-assets` do próprio repositório via API (repo PÚBLICO) -> raw.githubusercontent.com
- imgbb:  upload na API gratuita do imgbb.com (funciona com repo privado; requer IMGBB_API_KEY)
- none:   não hospeda (use apenas com o canal 'api', que envia a imagem em base64)
"""
from __future__ import annotations

import base64
from pathlib import Path

import requests

from .config import settings
from .logger import get_logger

log = get_logger("image_host")
GH_API = "https://api.github.com"


class ImageHostError(RuntimeError):
    pass


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _ensure_branch(repo: str, branch: str) -> None:
    r = requests.get(f"{GH_API}/repos/{repo}/branches/{branch}", headers=_gh_headers(), timeout=30)
    if r.status_code == 200:
        return
    info = requests.get(f"{GH_API}/repos/{repo}", headers=_gh_headers(), timeout=30).json()
    default = info.get("default_branch", "main")
    sha = requests.get(f"{GH_API}/repos/{repo}/git/ref/heads/{default}", headers=_gh_headers(), timeout=30).json()["object"]["sha"]
    created = requests.post(f"{GH_API}/repos/{repo}/git/refs", headers=_gh_headers(), timeout=30,
                            json={"ref": f"refs/heads/{branch}", "sha": sha})
    if created.status_code not in (200, 201, 422):
        raise ImageHostError(f"Não foi possível criar a branch {branch}: {created.text[:200]}")
    if info.get("private"):
        log.warning("O repositório é PRIVADO: URLs raw do GitHub não serão públicas. Use IMAGE_HOST=imgbb.")


def _upload_github(path: Path) -> str:
    repo, branch = settings.github_repository, settings.github_assets_branch
    if not (settings.github_token and repo):
        raise ImageHostError("GITHUB_TOKEN/GITHUB_REPOSITORY ausentes para IMAGE_HOST=github.")
    _ensure_branch(repo, branch)
    dest = f"pins/{path.name}"
    url = f"{GH_API}/repos/{repo}/contents/{dest}"
    existing = requests.get(url, headers=_gh_headers(), params={"ref": branch}, timeout=30)
    body = {
        "message": f"pin asset {path.name} [skip ci]",
        "content": base64.b64encode(path.read_bytes()).decode(),
        "branch": branch,
    }
    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]
    r = requests.put(url, headers=_gh_headers(), json=body, timeout=60)
    if r.status_code not in (200, 201):
        raise ImageHostError(f"Upload GitHub falhou ({r.status_code}): {r.text[:200]}")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{dest}"


def _upload_imgbb(path: Path) -> str:
    if not settings.imgbb_api_key:
        raise ImageHostError("IMGBB_API_KEY ausente para IMAGE_HOST=imgbb.")
    r = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": settings.imgbb_api_key, "image": base64.b64encode(path.read_bytes()).decode(), "name": path.stem},
        timeout=60,
    )
    if r.status_code != 200:
        raise ImageHostError(f"Upload imgbb falhou ({r.status_code}): {r.text[:200]}")
    return r.json()["data"]["url"]


def host_image(path: Path) -> str | None:
    host = settings.image_host
    if host == "none":
        return None
    try:
        url = _upload_github(path) if host == "github" else _upload_imgbb(path)
        log.info("Imagem hospedada: %s", url)
        return url
    except (ImageHostError, requests.RequestException, KeyError) as exc:
        log.warning("Falha ao hospedar imagem (%s): %s", host, exc)
        return None
