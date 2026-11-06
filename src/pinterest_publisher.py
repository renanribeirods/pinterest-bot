"""Publicação no Pinterest com cadeia de canais e fallback seguro.

Canais (PUBLISH_CHANNELS, em ordem de prioridade):
- webhook: envia o Pin para um cenário Make / Zapier / n8n / IFTTT que usa a integração OFICIAL
           já aprovada dessas plataformas com o Pinterest. É a rota "zero-approval": você só conecta
           sua conta Pinterest no Make, sem precisar de app próprio aprovado.
- api:     Pinterest API v5 oficial (POST /v5/pins). Exige app com acesso Standard para Pins públicos
           (em acesso Trial, os Pins ficam visíveis apenas para você / sandbox).
- dryrun:  não publica nada, apenas registra (útil para testes).

Erros de autenticação (401/403) ou limite (429) desativam o canal pelo resto da execução e o próximo
canal da cadeia assume. Se todos falharem, o Pin NÃO é marcado como publicado e volta para a fila.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import settings
from .logger import get_logger

log = get_logger("publisher")


@dataclass
class PinPayload:
    board_id: str
    title: str
    description: str
    link: str
    alt_text: str
    image_path: Path
    image_url: str | None = None
    platform: str = ""
    locale: str = ""
    product_id: str = ""
    variant_id: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class PublishResult:
    ok: bool
    channel: str
    pin_id: str = ""
    pin_url: str = ""
    error: str = ""


class ChannelDisabled(Exception):
    """Falha que inutiliza o canal até a próxima execução (token expirado, rate limit, config ausente)."""


class TransientError(Exception):
    """Falha momentânea: vale tentar o próximo canal, mas o canal continua ativo."""


# ---------------------------------------------------------------------------
class PinterestAPIChannel:
    name = "api"

    def __init__(self) -> None:
        self.base = settings.pinterest_api_base.rstrip("/")
        self.token = settings.pinterest_access_token
        self._refreshed = False

    def _refresh_token(self) -> bool:
        """Troca o refresh token por um novo access token (válido ~30 dias; refresh token ~1 ano)."""
        if self._refreshed or not (settings.pinterest_refresh_token and settings.pinterest_client_id
                                   and settings.pinterest_client_secret):
            return False
        self._refreshed = True
        basic = base64.b64encode(f"{settings.pinterest_client_id}:{settings.pinterest_client_secret}".encode()).decode()
        try:
            r = requests.post(
                f"{self.base}/oauth/token",
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "refresh_token", "refresh_token": settings.pinterest_refresh_token},
                timeout=30,
            )
        except requests.RequestException as exc:
            log.warning("Falha de rede ao renovar token do Pinterest: %s", exc)
            return False
        if r.status_code != 200:
            log.error("Renovação do token Pinterest falhou (%s): %s. Gere um novo refresh token e atualize "
                      "o Secret PINTEREST_REFRESH_TOKEN.", r.status_code, r.text[:300])
            return False
        data = r.json()
        self.token = data["access_token"]
        if data.get("refresh_token") and data["refresh_token"] != settings.pinterest_refresh_token:
            log.warning("O Pinterest emitiu um NOVO refresh token. Atualize o Secret PINTEREST_REFRESH_TOKEN "
                        "(expira em %s s) para evitar expiração futura.", data.get("refresh_token_expires_in"))
        log.info("Access token do Pinterest renovado (expira em %s s).", data.get("expires_in"))
        return True

    def publish(self, pin: PinPayload) -> PublishResult:
        if not pin.board_id:
            raise ChannelDisabled("Nenhum board_id configurado (PINTEREST_BOARD_ID / PINTEREST_BOARD_MAP).")
        if not self.token and not self._refresh_token():
            raise ChannelDisabled("Sem PINTEREST_ACCESS_TOKEN e sem refresh token válido.")

        # base64 direto: não depende de hospedagem externa da imagem
        media = {"source_type": "image_base64", "content_type": "image/jpeg",
                 "data": base64.b64encode(pin.image_path.read_bytes()).decode()}
        body = {
            "board_id": pin.board_id,
            "title": pin.title,
            "description": pin.description,
            "link": pin.link,
            "alt_text": pin.alt_text,
            "media_source": media,
        }

        for attempt in range(3):
            try:
                r = requests.post(f"{self.base}/pins", json=body, timeout=60,
                                  headers={"Authorization": f"Bearer {self.token}"})
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TransientError(f"Erro de rede na API do Pinterest: {exc}") from exc
                time.sleep(5 * (attempt + 1))
                continue

            if r.status_code in (200, 201):
                data = r.json()
                return PublishResult(True, self.name, data.get("id", ""),
                                     f"https://www.pinterest.com/pin/{data.get('id', '')}/")
            if r.status_code == 401:
                log.warning("Token do Pinterest expirado/inválido; tentando renovar...")
                if self._refresh_token():
                    continue
                raise ChannelDisabled("Token do Pinterest expirado e não renovável (401).")
            if r.status_code == 403:
                raise ChannelDisabled(f"Acesso negado pela API (403) — verifique escopos pins:write/boards:read "
                                      f"e o nível de acesso do app: {r.text[:200]}")
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset") or r.headers.get("retry-after")
                raise ChannelDisabled(f"Limite de requisições da API atingido (429). Reset em: {reset or 'desconhecido'}.")
            if r.status_code >= 500 and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise TransientError(f"API Pinterest respondeu {r.status_code}: {r.text[:300]}")
        raise TransientError("API Pinterest: tentativas esgotadas.")


# ---------------------------------------------------------------------------
class WebhookChannel:
    name = "webhook"

    def publish(self, pin: PinPayload) -> PublishResult:
        if not settings.webhook_url:
            raise ChannelDisabled("PIN_WEBHOOK_URL não configurado.")
        if not pin.image_url and not settings.webhook_include_base64:
            raise TransientError("Webhook exige URL pública da imagem (IMAGE_HOST) ou WEBHOOK_INCLUDE_BASE64=true.")

        payload = {
            "board_id": pin.board_id,
            "title": pin.title,
            "description": pin.description,
            "link": pin.link,
            "alt_text": pin.alt_text,
            "image_url": pin.image_url,
            "platform": pin.platform,
            "locale": pin.locale,
            "product_id": pin.product_id,
            "variant_id": pin.variant_id,
            **pin.extra,
        }
        if settings.webhook_include_base64:
            payload["image_base64"] = base64.b64encode(pin.image_path.read_bytes()).decode()
            payload["image_filename"] = pin.image_path.name

        headers = {"Content-Type": "application/json"}
        if settings.webhook_secret:
            headers["X-Webhook-Secret"] = settings.webhook_secret
        for attempt in range(3):
            try:
                r = requests.post(settings.webhook_url, json=payload, headers=headers, timeout=60)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TransientError(f"Webhook inacessível: {exc}") from exc
                time.sleep(5 * (attempt + 1))
                continue
            if 200 <= r.status_code < 300:
                pin_id, pin_url = "", ""
                try:  # se o cenário responder com o JSON do Pin criado, registramos o ID
                    data = r.json()
                    pin_id = str(data.get("id") or data.get("pin_id") or "")
                    pin_url = data.get("url") or (f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else "")
                except ValueError:
                    pass
                return PublishResult(True, self.name, pin_id, pin_url)
            if r.status_code in (401, 403, 404, 410):
                raise ChannelDisabled(f"Webhook recusou ({r.status_code}). Cenário desativado ou URL/segredo errados.")
            if r.status_code == 429:
                raise ChannelDisabled("Webhook atingiu o limite de operações (429) — verifique sua cota no Make/Zapier.")
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
        raise TransientError(f"Webhook falhou ({r.status_code}): {r.text[:200]}")


# ---------------------------------------------------------------------------
class DryRunChannel:
    name = "dryrun"

    def publish(self, pin: PinPayload) -> PublishResult:
        log.info("[DRY-RUN] '%s' -> board=%s link=%s img=%s", pin.title, pin.board_id or "-", pin.link,
                 pin.image_url or pin.image_path.name)
        return PublishResult(True, self.name, "", "")


CHANNELS = {"api": PinterestAPIChannel, "webhook": WebhookChannel, "dryrun": DryRunChannel}


class Publisher:
    def __init__(self, channel_names: list[str] | None = None, dry_run: bool = False) -> None:
        names = ["dryrun"] if dry_run else (channel_names or settings.publish_channels)
        self.channels = [CHANNELS[n]() for n in names if n in CHANNELS]
        self.disabled: dict[str, str] = {}
        if not self.channels:
            raise ValueError(f"Nenhum canal válido em PUBLISH_CHANNELS={names}. Use: {list(CHANNELS)}")

    @property
    def needs_hosted_image(self) -> bool:
        return any(c.name == "webhook" for c in self.active_channels)

    @property
    def active_channels(self):
        return [c for c in self.channels if c.name not in self.disabled]

    @property
    def exhausted(self) -> bool:
        return not self.active_channels

    def publish(self, pin: PinPayload) -> PublishResult:
        errors = []
        for channel in self.active_channels:
            try:
                result = channel.publish(pin)
                log.info("Publicado via %s: '%s' %s", channel.name, pin.title, result.pin_url)
                return result
            except ChannelDisabled as exc:
                self.disabled[channel.name] = str(exc)
                log.error("Canal '%s' desativado nesta execução: %s", channel.name, exc)
                errors.append(f"{channel.name}: {exc}")
            except TransientError as exc:
                log.warning("Canal '%s' falhou para este Pin: %s", channel.name, exc)
                errors.append(f"{channel.name}: {exc}")
        return PublishResult(False, "none", error=" | ".join(errors) or "nenhum canal ativo")
