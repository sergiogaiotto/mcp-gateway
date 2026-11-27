"""O cofre (OpenBao): os segredos das credenciais e a destruição das chaves da empresa.

:class:`OpenBaoClient` fala com a API HTTP do OpenBao (``httpx``):

- entra pelo método ``kubernetes`` (``RAPA_BAO_AUTH``, padrão ``kubernetes``) com o
  token projetado da conta de serviço (``RAPA_BAO_TOKEN_SA``, com a audiência própria do
  OpenBao, lido de novo a cada entrada: o kubelet o troca) e o papel ``RAPA_BAO_PAPEL``;
  só em desenvolvimento, um token num arquivo (``RAPA_BAO_TOKEN_ARQUIVO``);
- renova o token antes de vencer (passados dois terços da validade) e, se a renovação
  falhar ou o token já tiver vencido, entra de novo; uma resposta 403 descarta o token,
  entra de novo e repete o pedido uma vez (a segunda 403 é uma recusa de verdade);
- confere o TLS com o CA de ``RAPA_BAO_CA`` (ou o do sistema), nunca segue
  redirecionamento (um 3xx é erro: aponte para o serviço ativo) e usa prazos curtos;
- os erros (:class:`VaultError`) dizem só o que falhou e o código HTTP: nunca o corpo da
  resposta, um valor ou o token.

:class:`OpenBaoSecretSource` entrega ao lançador os segredos de uma execução. Para cada
credencial do manifesto ele acha a linha registrada em ``credentials`` na área do robô
(mesmo nome, mesmo tipo e mesma origem; não revogada; o certificado A1 não vencido), lê
o KV v2 em ``<RAPA_BAO_KV>/data/empresas/<empresa>/<vault_path>`` e devolve só os campos
permitidos (:data:`VAULT_FIELDS`). O prefixo da empresa é sempre posto aqui, a partir da
empresa da execução: um ``vault_path`` nunca alcança outra empresa, e um caminho com
``..``, ``.``, barra no começo ou no fim, barras seguidas ou caracteres fora da regra do
banco é recusado. Faltou o registro, o segredo ou um campo que os passos usam (o
certificado A1 sempre precisa de ``pfx_base64``): :class:`SecretsUnavailable`, com a
lista do que falta pelo nome da credencial, e o lançador termina a execução com
``segredo_indisponivel`` sem abrir o navegador. O despachante lê ``vault_path`` (uma
referência, não um segredo); o valor do segredo só passa pela memória do lançador.

:class:`OpenBaoKeyDestroyer` é o :class:`rapa.control.services.lifecycle.KeyDestroyer`
da saída da empresa: apaga os metadados (todas as versões) de todo o KV em
``empresas/<empresa>/``, descendo pela listagem, permite a exclusão da chave transit
``empresa-<uuid>`` e a apaga, e apaga os objetos do prefixo da empresa no store de
objetos. Devolve só as contagens (vão para o registro ``empresa.apagada``).

:class:`BaoReadiness` é a checagem de prontidão do cofre: pronto só se o OpenBao
responde, está iniciado e não está selado.
"""

import asyncio
import contextlib
import logging
import math
import re
import ssl
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from rapa.control.db.schema import credentials, runs
from rapa.control.db.session import Database
from rapa.control.launcher import SecretsUnavailable
from rapa.core.contracts import RunnerJob
from rapa.core.robot import CredentialKind, CredentialRef, Manifest

if TYPE_CHECKING:
    from rapa.control.artifacts import ArtifactStore

__all__ = [
    "VAULT_FIELDS",
    "BaoReadiness",
    "BaoSettings",
    "OpenBaoClient",
    "OpenBaoKeyDestroyer",
    "OpenBaoSecretSource",
    "VaultDenied",
    "VaultError",
    "VaultUnavailable",
    "company_secret_path",
    "required_fields",
    "transit_key_name",
]

logger = logging.getLogger("rapa.control.vault")

#: Os campos que o cofre entrega ao lançador (o resto do segredo fica no cofre).
VAULT_FIELDS = frozenset({"username", "password", "totp_secret", "pfx_base64", "pfx_password"})
#: O campo do cofre de cada campo que um passo digita.
_STEP_FIELDS = {"username": "username", "password": "password", "totp": "totp_secret"}
#: A regra de ``credentials.vault_path`` no banco (revisão 0001), em segmentos.
_SEGMENT = r"[A-Za-z0-9_.:-]+"
_VAULT_PATH = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})*$")
_MAX_VAULT_PATH = 500
#: Uma montagem (KV, transit, método de entrada): segmentos simples.
_MOUNT = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")
_ROLE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
#: O token é renovado depois de passada esta fração da validade.
_RENEW_FRACTION = 2 / 3
#: Listagens do KV na destruição: profundidade e entradas, no máximo.
_MAX_DEPTH = 32
_MAX_ENTRIES = 100_000


class VaultError(RuntimeError):
    """Falha ao falar com o cofre. A mensagem nunca traz valores, tokens ou corpos."""


class VaultUnavailable(VaultError):
    """O cofre não respondeu (rede, prazo, TLS)."""


class VaultDenied(VaultError):
    """O cofre recusou o pedido mesmo depois de entrar de novo (403)."""


def _is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in _LOOPBACK


class BaoSettings(BaseModel):
    """Como falar com o OpenBao (``RAPA_BAO_*``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str = Field(min_length=1)
    ca_file: Path | None = None
    kv_mount: str = "rapa"
    transit_mount: str = "rapa-transit"
    role: str | None = None
    auth_mount: str = "kubernetes"
    sa_token_file: Path = Path("/var/run/secrets/rapa/bao/token")
    #: Só em desenvolvimento: um token do OpenBao num arquivo, no lugar da entrada.
    token_file: Path | None = None
    timeout_s: float = Field(default=5.0, gt=0, le=60)
    connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    dev: bool = False

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("RAPA_BAO_ENDERECO precisa ser um endereço http(s)://host[:porta]")
        if parts.path or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("RAPA_BAO_ENDERECO é só esquema, host e porta")
        return value

    @field_validator("kv_mount", "transit_mount", "auth_mount")
    @classmethod
    def _mount(cls, value: str) -> str:
        if not _MOUNT.fullmatch(value):
            raise ValueError("montagem do OpenBao inválida (letras, números, _ e -, em segmentos)")
        return value

    @field_validator("role")
    @classmethod
    def _role(cls, value: str | None) -> str | None:
        if value is not None and not _ROLE.fullmatch(value):
            raise ValueError("RAPA_BAO_PAPEL inválido")
        return value

    @model_validator(mode="after")
    def _check(self) -> BaoSettings:
        if self.address.startswith("http://") and not (self.dev or _is_loopback(self.address)):
            raise ValueError("o OpenBao precisa de https:// fora do desenvolvimento")
        if self.token_file is not None and not self.dev:
            raise ValueError("RAPA_BAO_TOKEN_ARQUIVO só vale com RAPA_DEV=1")
        if self.token_file is None and self.role is None:
            raise ValueError("o OpenBao precisa do papel do método kubernetes (RAPA_BAO_PAPEL)")
        return self


def _segments(path: str) -> list[str]:
    return [quote(part, safe="") for part in path.split("/")]


def company_secret_path(company_id: UUID, vault_path: str) -> str:
    """O caminho no KV de um segredo da empresa: ``empresas/<empresa>/<vault_path>``.

    O prefixo da empresa vem sempre daqui (da empresa da execução); ``vault_path`` é só
    o resto, conferido pela regra do banco, sem ``.`` nem ``..`` em nenhum segmento, sem
    barra no começo ou no fim e sem barras seguidas. Levanta ``ValueError``.
    """
    if not isinstance(company_id, UUID):
        raise TypeError("a empresa precisa ser um UUID")
    if (
        not isinstance(vault_path, str)
        or len(vault_path) > _MAX_VAULT_PATH
        or not _VAULT_PATH.fullmatch(vault_path)
        or any(part in {".", ".."} for part in vault_path.split("/"))
    ):
        raise ValueError("caminho no cofre inválido")
    return "/".join(["empresas", str(company_id), *_segments(vault_path)])


def transit_key_name(company_id: UUID) -> str:
    """O nome da chave transit da empresa: ``empresa-<uuid>``."""
    if not isinstance(company_id, UUID):
        raise TypeError("a empresa precisa ser um UUID")
    return f"empresa-{company_id}"


@dataclass(slots=True)
class _Token:
    value: str
    renew_at: float
    expires_at: float
    renewable: bool


class OpenBaoClient:
    """Cliente pequeno da API HTTP do OpenBao (veja o início do módulo)."""

    def __init__(
        self,
        settings: BaoSettings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._clock = clock
        self._client: httpx2.AsyncClient | None = None
        self._token: _Token | None = None
        self._lock = asyncio.Lock()
        #: Quantas vezes entrou (para os testes e o log).
        self.logins = 0

    def __repr__(self) -> str:
        return f"OpenBaoClient(endereco={self.settings.address!r})"

    def _verify(self) -> ssl.SSLContext | bool:
        if self.settings.ca_file is None:
            return True
        return ssl.create_default_context(cafile=str(self.settings.ca_file))

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient(
                base_url=self.settings.address,
                verify=self._verify(),
                timeout=httpx2.Timeout(
                    self.settings.timeout_s, connect=self.settings.connect_timeout_s
                ),
                follow_redirects=False,
                transport=self._transport,
                headers={"X-Vault-Request": "true"},
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._token = None

    async def _send(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        what: str,
    ) -> httpx2.Response:
        headers = {"X-Vault-Token": token} if token is not None else {}
        try:
            client = self._http()
        except (OSError, ssl.SSLError) as exc:
            raise VaultUnavailable(
                f"o CA do OpenBao (RAPA_BAO_CA) não pôde ser lido: {type(exc).__name__}"
            ) from None
        try:
            response = await client.request(
                method, f"/v1/{path}", json=json, params=params, headers=headers
            )
        except httpx2.HTTPError as exc:
            raise VaultUnavailable(
                f"o OpenBao não respondeu ({what}): {type(exc).__name__}"
            ) from None
        if 300 <= response.status_code < 400:
            raise VaultError(f"o OpenBao pediu um redirecionamento ({what}); recusado")
        return response

    @staticmethod
    def _body(response: httpx2.Response, what: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise VaultError(f"resposta inválida do OpenBao ({what})") from None
        if not isinstance(body, dict):
            raise VaultError(f"resposta inválida do OpenBao ({what})")
        return body

    def _new_token(self, auth: object, what: str) -> _Token:
        if not isinstance(auth, dict) or not isinstance(auth.get("client_token"), str):
            raise VaultError(f"resposta inválida do OpenBao ({what})")
        lease = auth.get("lease_duration")
        lease = float(lease) if isinstance(lease, int | float) and lease > 0 else math.inf
        now = self._clock()
        return _Token(
            value=auth["client_token"],
            renew_at=now + lease * _RENEW_FRACTION,
            expires_at=now + lease,
            renewable=bool(auth.get("renewable")) and lease != math.inf,
        )

    async def _login(self) -> _Token:
        settings = self.settings
        self.logins += 1
        if settings.token_file is not None:
            try:
                value = settings.token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise VaultError(
                    f"o arquivo do token do OpenBao não pôde ser lido: {type(exc).__name__}"
                ) from None
            if not value:
                raise VaultError("o arquivo do token do OpenBao está vazio")
            return _Token(value, math.inf, math.inf, renewable=False)
        try:
            jwt = settings.sa_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise VaultError(
                f"o token da conta de serviço não pôde ser lido: {type(exc).__name__}"
            ) from None
        response = await self._send(
            "POST",
            f"auth/{settings.auth_mount}/login",
            token=None,
            json={"role": settings.role, "jwt": jwt},
            what="entrada",
        )
        if response.status_code != 200:
            raise VaultDenied(f"o OpenBao recusou a entrada (HTTP {response.status_code})")
        return self._new_token(self._body(response, "entrada").get("auth"), "entrada")

    async def _renew(self, token: _Token) -> _Token | None:
        try:
            response = await self._send(
                "POST", "auth/token/renew-self", token=token.value, json={}, what="renovação"
            )
        except VaultError:
            return None
        if response.status_code != 200:
            return None
        try:
            renewed = self._new_token(self._body(response, "renovação").get("auth"), "renovação")
        except VaultError:
            return None
        return renewed

    async def _current(self) -> str:
        async with self._lock:
            token = self._token
            now = self._clock()
            if token is not None and now >= token.expires_at:
                token = None
            if token is not None and now >= token.renew_at:
                # Perto do fim: renova (ou, sem renovação, entra de novo antes de vencer).
                token = await self._renew(token) if token.renewable else None
            if token is None:
                token = await self._login()
            self._token = token
            return token.value

    async def _drop(self, value: str) -> None:
        async with self._lock:
            if self._token is not None and self._token.value == value:
                self._token = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        what: str,
        ok: Collection[int] = (200, 204),
        absent: Collection[int] = (404,),
    ) -> dict[str, Any] | None:
        """Um pedido autenticado; ``None`` quando o código está em ``absent``.

        Uma 403 descarta o token e repete uma vez com uma entrada nova.
        """
        for retry in (False, True):
            token = await self._current()
            response = await self._send(
                method, path, token=token, json=json, params=params, what=what
            )
            if response.status_code == 403 and not retry:
                await self._drop(token)
                continue
            if response.status_code in absent:
                return None
            if response.status_code not in ok:
                if response.status_code == 403:
                    raise VaultDenied(f"o OpenBao recusou o pedido ({what}, HTTP 403)")
                raise VaultError(f"o OpenBao respondeu HTTP {response.status_code} ({what})")
            if response.status_code == 204 or not response.content:
                return {}
            return self._body(response, what)
        raise AssertionError("inalcançável")  # pragma: no cover

    async def read_kv(self, mount: str, path: str) -> dict[str, Any] | None:
        """Os dados da versão atual de um segredo do KV v2, ou ``None`` se não existe."""
        body = await self.request("GET", f"{mount}/data/{path}", what="leitura do segredo")
        if body is None:
            return None
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None  # apagado (só os metadados ficaram) ou fora do formato
        return dict(data["data"])

    async def health(self) -> bool:
        """Iniciado e não selado (um standby conta como pronto)."""
        try:
            response = await self._send(
                "GET",
                "sys/health",
                token=None,
                params={"standbyok": "true", "perfstandbyok": "true"},
                what="saúde",
            )
        except VaultError:
            return False
        return response.status_code == 200


def required_fields(manifest: Manifest) -> dict[str, set[str]]:
    """Os campos do cofre de que cada credencial precisa: os que os passos digitam e, no
    certificado A1, ``pfx_base64``."""
    needed: dict[str, set[str]] = {ref.name: set() for ref in manifest.credentials}
    kinds = {ref.name: ref.kind for ref in manifest.credentials}
    for name, kind in kinds.items():
        if kind is CredentialKind.PFX_A1:
            needed[name].add("pfx_base64")

    def scan(value: object) -> Iterator[tuple[str, str]]:
        if isinstance(value, dict):
            if value.get("source") == "secret" and isinstance(value.get("credential"), str):
                yield value["credential"], str(value.get("field"))
            for item in value.values():
                yield from scan(item)
        elif isinstance(value, list):
            for item in value:
                yield from scan(item)

    for step, parents in manifest.iter_steps():
        if parents:
            continue  # os passos de dentro já vêm no do bloco
        for credential, field in scan(step.model_dump(mode="json")):
            if credential in needed and field in _STEP_FIELDS:
                needed[credential].add(_STEP_FIELDS[field])
    return needed


class OpenBaoSecretSource:
    """Os segredos de uma execução, do OpenBao (veja o início do módulo).

    ``db`` é o banco do papel do despachante (o lançador roda no despachante).
    """

    def __init__(
        self,
        db: Database,
        client: OpenBaoClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.client = client
        self.clock = clock

    async def _registered(self, job: RunnerJob) -> tuple[bool, dict[str, Any]]:
        names = [ref.name for ref in job.manifest.credentials]
        async with self.db.dispatcher_session() as session:
            area = await session.scalar(
                select(runs.c.area_id).where(
                    runs.c.id == job.run_id, runs.c.company_id == job.company_id
                )
            )
            if area is None:
                return False, {}
            rows = (
                (
                    await session.execute(
                        select(
                            credentials.c.name,
                            credentials.c.kind,
                            credentials.c.origin,
                            credentials.c.vault_path,
                            credentials.c.not_after,
                            credentials.c.revoked_at,
                        ).where(
                            credentials.c.company_id == job.company_id,
                            credentials.c.area_id == area,
                            credentials.c.name.in_(names),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return True, {row["name"]: row for row in rows}

    def _registration_problem(self, ref: CredentialRef, row: Any) -> str | None:
        name = ref.name
        if row is None:
            return f"a credencial '{name}' não está registrada na área do robô"
        if row["kind"] != ref.kind.value:
            return f"a credencial '{name}' está registrada com outro tipo"
        if row["origin"] != ref.origin:
            return f"a credencial '{name}' está registrada para outra origem"
        if row["revoked_at"] is not None:
            return f"a credencial '{name}' foi revogada"
        if ref.kind is CredentialKind.PFX_A1 and (
            row["not_after"] is None or row["not_after"] <= self.clock()
        ):
            return f"o certificado '{name}' venceu (ou não tem a validade registrada)"
        return None

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        refs = list(job.manifest.credentials)
        if not refs:
            return {}
        found, registered = await self._registered(job)
        if not found:
            raise SecretsUnavailable("a execução não foi encontrada para buscar os segredos")
        needed = required_fields(job.manifest)
        problems: list[str] = []
        out: dict[str, dict[str, str]] = {}
        for ref in refs:
            row = registered.get(ref.name)
            problem = self._registration_problem(ref, row)
            if row is None or problem is not None:
                problems.append(problem or f"a credencial '{ref.name}' não está registrada")
                continue
            try:
                path = company_secret_path(job.company_id, row["vault_path"])
            except ValueError:
                problems.append(f"o caminho no cofre da credencial '{ref.name}' é inválido")
                continue
            try:
                data = await self.client.read_kv(self.client.settings.kv_mount, path)
            except VaultError as exc:
                logger.error("cofre indisponível para a execução %s: %s", job.run_id, exc)
                raise SecretsUnavailable(
                    "o cofre não respondeu; nenhum segredo foi entregue"
                ) from None
            if data is None:
                problems.append(f"o segredo da credencial '{ref.name}' não está no cofre")
                continue
            fields: dict[str, str] = {}
            for key in sorted(VAULT_FIELDS & data.keys()):
                value = data[key]
                if not isinstance(value, str):
                    problems.append(f"o campo '{key}' da credencial '{ref.name}' não é texto")
                    continue
                fields[key] = value
            missing = sorted(needed[ref.name] - {k for k, v in fields.items() if v})
            if missing:
                problems.append(
                    f"falta {', '.join(repr(m) for m in missing)} no segredo da credencial "
                    f"'{ref.name}'"
                )
                continue
            out[ref.name] = fields
        if problems:
            raise SecretsUnavailable("; ".join(problems))
        return out


class OpenBaoKeyDestroyer:
    """Destrói o que a empresa apagada tem no cofre e no store (veja o início do módulo)."""

    def __init__(
        self,
        client: OpenBaoClient,
        *,
        secrets: bool = True,
        transit: bool = True,
        store: ArtifactStore | None = None,
    ) -> None:
        self.client = client
        self.secrets = secrets
        self.transit = transit
        self.store = store

    @property
    def destroys_secrets(self) -> bool:
        """Apaga os segredos do KV da empresa (:func:`purge_company` confere)."""
        return self.secrets

    @property
    def destroys_transit_keys(self) -> bool:
        """Apaga a chave transit da empresa (:func:`purge_company` confere)."""
        return self.transit

    async def _list(self, path: str) -> list[str]:
        body = await self.client.request(
            "GET",
            f"{self.client.settings.kv_mount}/metadata/{path}/",
            params={"list": "true"},
            what="listagem dos segredos",
        )
        if body is None:
            return []
        keys = (body.get("data") or {}).get("keys")
        if not isinstance(keys, list):
            return []
        return [
            key for key in keys if isinstance(key, str) and key and key not in {"/", "./", "../"}
        ]

    async def _delete_tree(self, path: str, depth: int, budget: list[int]) -> int:
        if depth > _MAX_DEPTH:
            raise VaultError("os segredos da empresa têm pastas demais para apagar")
        deleted = 0
        for key in await self._list(path):
            budget[0] -= 1
            if budget[0] < 0:
                raise VaultError("os segredos da empresa são muitos para apagar de uma vez")
            name = key.rstrip("/")
            if "/" in name or name in {"", ".", ".."}:
                continue
            child = f"{path}/{quote(name, safe='')}"
            if key.endswith("/"):
                deleted += await self._delete_tree(child, depth + 1, budget)
            else:
                await self.client.request(
                    "DELETE",
                    f"{self.client.settings.kv_mount}/metadata/{child}",
                    what="exclusão de um segredo",
                )
                deleted += 1
        return deleted

    async def _delete_transit(self, company_id: UUID) -> int:
        mount = self.client.settings.transit_mount
        name = transit_key_name(company_id)
        existing = await self.client.request(
            "GET", f"{mount}/keys/{name}", what="leitura da chave da empresa"
        )
        if existing is None:
            return 0  # a empresa nunca teve chave transit (ou já foi apagada)
        # O OpenBao só apaga uma chave transit com a exclusão liberada antes. Vai como
        # texto: a política da operação só deixa este campo, com o valor "true"
        # (allowed_parameters compara texto com texto), e o transit lê "true" como sim.
        await self.client.request(
            "POST",
            f"{mount}/keys/{name}/config",
            json={"deletion_allowed": "true"},
            what="liberar a exclusão da chave",
            absent=(),
        )
        await self.client.request("DELETE", f"{mount}/keys/{name}", what="exclusão da chave")
        return 1

    async def destroy(self, company_id: UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        if self.secrets:
            counts["segredos"] = await self._delete_tree(
                f"empresas/{company_id}", 0, [_MAX_ENTRIES]
            )
        if self.transit:
            counts["chaves_transit"] = await self._delete_transit(company_id)
        if self.store is not None:
            counts["objetos"] = await self.store.delete_company(company_id)
        logger.info("empresa %s: chaves e segredos destruídos %s", company_id, counts)
        return counts


@dataclass(frozen=True, slots=True)
class BaoReadiness:
    """Pronto só se o OpenBao responde, está iniciado e não está selado."""

    client: OpenBaoClient
    name: str = "openbao"

    async def ready(self) -> bool:
        with contextlib.suppress(Exception):
            return await self.client.health()
        return False
