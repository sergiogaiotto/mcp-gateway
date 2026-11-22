"""Autenticação da camada HTTP (REST e MCP).

O token de acesso vem do Keycloak. Daqui sai só a identidade verificada: quem
é (``sub``) e de qual empresa (a Organização do Keycloak). Os papéis nunca vêm
do token: o :class:`PrincipalResolver` busca o :class:`Principal` no nosso
banco. O token recebido nunca é repassado a outro serviço.

A empresa do token sai da Organização: pelo mapa estático
(``organization_aliases``), pelo vínculo guardado no banco
(``companies.idp_organization``, consultado por um :class:`OrganizationDirectory`
com cache curto, :class:`CachedOrganizationDirectory`: uma empresa nova entra sem
reiniciar o serviço) ou, como antes, pelo identificador do objeto do Keycloak quando
ele é o próprio identificador da empresa. Se o mapa e o banco discordam, recusa.

O vínculo guarda o id ou o apelido, e o formato diz qual é: um valor no formato de
UUID é sempre o id da organização (guardado na forma canônica), e um apelido nunca
tem esse formato. Por isso o id do token só é procurado como id e o apelido só como
apelido: uma organização cujo apelido é o id de outra não chega à empresa da outra.

No primeiro login de uma pessoa de uma organização ligada a uma empresa (pelo mapa
ou pelo banco), um :class:`UserProvisioner` cria o usuário sem nenhum papel; um
usuário desativado continua sem entrar.

Há um modo de desenvolvimento e testes com tokens fixos
(:class:`StaticTokenVerifier`), que só funciona quando ligado explicitamente
nas configurações da aplicação HTTP.
"""

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx2
import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rapa.core.contracts import Principal

__all__ = [
    "SUPPORTED_ALGORITHMS",
    "AccessDenied",
    "AuthError",
    "AuthUnavailable",
    "Authenticator",
    "BearerVerifier",
    "CachedOrganizationDirectory",
    "DevTokenVerifier",
    "JwksClient",
    "JwtSettings",
    "OrganizationDirectory",
    "PrincipalResolver",
    "StaticTokenVerifier",
    "TokenVerifier",
    "Unauthenticated",
    "UserProvisioner",
    "VerifiedToken",
    "bearer_token",
]

logger = logging.getLogger(__name__)

#: Só assinaturas assimétricas; nunca HS* nem "none".
SUPPORTED_ALGORITHMS = frozenset({"RS256", "ES256"})

_BEARER = re.compile(r"^[Bb][Ee][Aa][Rr][Ee][Rr] +([A-Za-z0-9\-._~+/]+=*) *$")
_MAX_JWKS_BYTES = 256 * 1024
_MAX_JWKS_KEYS = 50
#: Tamanho máximo do nome tirado do token para um usuário criado no primeiro login.
_MAX_DISPLAY_NAME = 200
_UNKNOWN_COMPANY = "empresa do token não reconhecida"


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Falha de autenticação, com código estável e mensagem em pt-BR."""

    status_code = 401
    code = "nao_autenticado"

    def __init__(self, message: str, *, bearer_error: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: Valor de ``error`` no cabeçalho WWW-Authenticate (RFC 6750), se houver.
        self.bearer_error = bearer_error


class Unauthenticated(AuthError):
    """Sem token, ou token inválido, expirado ou emitido para outro destino (401)."""


class AccessDenied(AuthError):
    """Token válido, mas sem acesso: empresa ausente ou ambígua, usuário desconhecido (403)."""

    status_code = 403
    code = "acesso_negado"


class AuthUnavailable(AuthError):
    """Não foi possível verificar o token agora (chaves do provedor indisponíveis) (503)."""

    status_code = 503
    code = "autenticacao_indisponivel"


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------


class PrincipalResolver(Protocol):
    """Busca o principal (com papéis) no banco a partir da identidade verificada."""

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None: ...


class UserProvisioner(Protocol):
    """Cria, no primeiro login, o usuário que ainda não existe (sem nenhum papel).

    Devolve ``None`` quando não pode criar (empresa inexistente ou congelada, sujeito
    já existente e desativado, ou de outra empresa): o acesso é negado.
    """

    async def provision(
        self, subject: str, company_id: UUID, display_name: str = ""
    ) -> Principal | None: ...


class OrganizationDirectory(Protocol):
    """A empresa ligada a uma organização do provedor (id ou apelido), ou ``None``."""

    async def company_for(self, organization: str) -> UUID | None: ...


class CachedOrganizationDirectory:
    """Cache curto (60 s por padrão) na memória sobre outro :class:`OrganizationDirectory`.

    Guarda também a resposta "não ligada": uma organização desconhecida não vai ao banco
    a cada pedido. Uma empresa nova (ou um vínculo trocado) passa a valer em até
    ``ttl_s`` segundos, sem reiniciar. Um erro do banco nunca fica no cache.
    """

    def __init__(
        self,
        inner: OrganizationDirectory,
        *,
        ttl_s: float = 60.0,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s < 0 or max_entries < 1:
            raise ValueError("o cache das organizações precisa de prazo e tamanho positivos")
        self.inner = inner
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, UUID | None]] = {}

    async def company_for(self, organization: str) -> UUID | None:
        now = self._clock()
        cached = self._entries.get(organization)
        if cached is not None and now - cached[0] < self.ttl_s:
            return cached[1]
        found = await self.inner.company_for(organization)
        if len(self._entries) >= self.max_entries:
            self._entries = {
                key: entry for key, entry in self._entries.items() if now - entry[0] < self.ttl_s
            }
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
        self._entries[organization] = (now, found)
        return found


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """Identidade extraída de um token verificado."""

    subject: str
    company_id: UUID
    client_id: str | None = None
    expires_at: int | None = None
    #: Só no modo de tokens fixos: o principal já configurado no servidor.
    principal: Principal | None = None
    #: Nome da pessoa no token (``name`` ou ``preferred_username``), para o primeiro login.
    display_name: str | None = None
    #: A empresa veio de um vínculo explícito (o mapa estático ou o banco): só assim o
    #: primeiro login cria o usuário.
    organization_linked: bool = False


class BearerVerifier(Protocol):
    """Verifica um token de acesso e devolve a identidade, ou levanta :class:`AuthError`."""

    async def verify(self, token: str) -> VerifiedToken: ...


def bearer_token(authorization: str | None) -> str:
    """Extrai o token do cabeçalho ``Authorization: Bearer <token>``."""
    if not authorization:
        raise Unauthenticated("é preciso enviar um token de acesso (Authorization: Bearer)")
    match = _BEARER.fullmatch(authorization)
    if match is None:
        raise Unauthenticated(
            "o cabeçalho Authorization precisa ser 'Bearer <token>'", bearer_error="invalid_request"
        )
    return match.group(1)


class Authenticator:
    """Junta a verificação do token com a busca do principal no banco.

    Com ``provisioner``, o primeiro login de quem ainda não existe numa empresa ligada
    explicitamente à organização do token cria o usuário sem papéis (ele só vê o pedido
    para um administrador dar um papel).
    """

    def __init__(
        self,
        verifier: BearerVerifier,
        resolver: PrincipalResolver,
        *,
        provisioner: UserProvisioner | None = None,
    ) -> None:
        self.verifier = verifier
        self.resolver = resolver
        self.provisioner = provisioner

    async def authenticate(self, authorization: str | None) -> Principal:
        token = bearer_token(authorization)
        verified = await self.verifier.verify(token)
        principal = verified.principal
        if principal is None:
            principal = await self.resolver.resolve(verified.subject, verified.company_id)
            if principal is None and self.provisioner is not None and verified.organization_linked:
                principal = await self.provisioner.provision(
                    verified.subject, verified.company_id, verified.display_name or ""
                )
            if principal is None:
                raise AccessDenied("usuário sem acesso ao RaPA nesta empresa")
        if principal.company_id != verified.company_id or principal.subject != verified.subject:
            logger.warning("principal resolvido não confere com o token; acesso negado")
            raise AccessDenied("usuário sem acesso ao RaPA nesta empresa")
        if principal.client_id is None and verified.client_id is not None:
            principal = principal.model_copy(update={"client_id": verified.client_id})
        return principal


# ---------------------------------------------------------------------------
# Tokens fixos (desenvolvimento e testes)
# ---------------------------------------------------------------------------


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class StaticTokenVerifier:
    """Mapeia tokens fixos para principais configurados no servidor.

    Só para desenvolvimento e testes: a aplicação HTTP recusa este verificador
    a menos que ``allow_static_tokens`` esteja ligado. Os tokens ficam guardados
    só como SHA-256.
    """

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        if not tokens:
            raise ValueError("informe pelo menos um token")
        self._by_digest: dict[bytes, Principal] = {}
        for token, principal in tokens.items():
            if not token or _BEARER.fullmatch(f"Bearer {token}") is None:
                raise ValueError("token fixo com caracteres inválidos")
            self._by_digest[_digest(token)] = principal

    async def verify(self, token: str) -> VerifiedToken:
        principal = self._by_digest.get(_digest(token))
        if principal is None:
            raise Unauthenticated("token inválido", bearer_error="invalid_token")
        return VerifiedToken(
            subject=principal.subject,
            company_id=principal.company_id,
            client_id=principal.client_id,
            principal=principal,
        )


class DevTokenVerifier(StaticTokenVerifier):
    """Tokens fixos que só dizem quem é a pessoa; os papéis vêm do banco.

    Para desenvolvimento e demonstração sobre o PostgreSQL: cada token aponta
    para (sujeito, empresa), e o :class:`PrincipalResolver` busca os papéis,
    como faria com um token do Keycloak. Também exige ``allow_static_tokens``.
    """

    def __init__(self, tokens: Mapping[str, tuple[str, UUID]]) -> None:
        if not tokens:
            raise ValueError("informe pelo menos um token")
        self._identities: dict[bytes, tuple[str, UUID]] = {}
        for token, identity in tokens.items():
            if not token or _BEARER.fullmatch(f"Bearer {token}") is None:
                raise ValueError("token fixo com caracteres inválidos")
            self._identities[_digest(token)] = identity

    async def verify(self, token: str) -> VerifiedToken:
        identity = self._identities.get(_digest(token))
        if identity is None:
            raise Unauthenticated("token inválido", bearer_error="invalid_token")
        subject, company_id = identity
        return VerifiedToken(subject=subject, company_id=company_id)


# ---------------------------------------------------------------------------
# Tokens do Keycloak
# ---------------------------------------------------------------------------


def _check_url(value: str, what: str) -> str:
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".test")
    if not host or not (parts.scheme == "https" or (parts.scheme == "http" and local)):
        raise ValueError(f"{what} precisa usar https")
    return value


class JwtSettings(BaseModel):
    """Configuração da verificação dos tokens emitidos pelo Keycloak."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: URL exata do emissor (``iss``), por exemplo ``https://sso/realms/rapa``.
    issuer: str = Field(min_length=1)
    #: Público exigido (``aud``): a URL do recurso do gateway.
    audience: str = Field(min_length=1)
    jwks_url: str = Field(min_length=1)
    algorithms: frozenset[str] = SUPPORTED_ALGORITHMS
    leeway_s: float = Field(default=30.0, ge=0, le=300)
    jwks_cache_ttl_s: float = Field(default=300.0, gt=0)
    #: Intervalo mínimo entre buscas das chaves quando aparece um ``kid`` novo.
    jwks_min_refresh_interval_s: float = Field(default=30.0, ge=0)
    jwks_timeout_s: float = Field(default=5.0, gt=0, le=60)
    organization_claim: str = "organization"
    #: Alias da Organização → empresa, para tokens com a lista de aliases.
    organization_aliases: dict[str, UUID] = Field(default_factory=dict)
    max_token_bytes: int = Field(default=16 * 1024, ge=512)

    @field_validator("jwks_url")
    @classmethod
    def _check_jwks_url(cls, value: str) -> str:
        return _check_url(value, "a URL das chaves (JWKS)")

    @field_validator("algorithms")
    @classmethod
    def _check_algorithms(cls, value: frozenset[str]) -> frozenset[str]:
        if not value or not value <= SUPPORTED_ALGORITHMS:
            raise ValueError("só RS256 e ES256 são aceitos")
        return value


class JwksClient:
    """Chaves públicas do provedor, com cache e rotação.

    As chaves ficam em memória por ``cache_ttl_s``. Um ``kid`` desconhecido
    força uma nova busca, no máximo uma vez a cada ``min_refresh_interval_s``
    (rotação de chaves sem deixar um token forjado derrubar o provedor). Se a
    busca falhar, as chaves já conhecidas continuam valendo.
    """

    def __init__(
        self,
        url: str,
        *,
        algorithms: frozenset[str] = SUPPORTED_ALGORITHMS,
        cache_ttl_s: float = 300.0,
        min_refresh_interval_s: float = 30.0,
        timeout_s: float = 5.0,
        http_client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.algorithms = algorithms
        self.cache_ttl_s = cache_ttl_s
        self.min_refresh_interval_s = min_refresh_interval_s
        self._timeout_s = timeout_s
        self._client = http_client
        self._owns_client = http_client is None
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self.cache_ttl_s

    def _may_refresh(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self.min_refresh_interval_s

    async def get_key(self, kid: str) -> jwt.PyJWK:
        now = self._clock()
        key = self._keys.get(kid)
        if key is not None and self._fresh(now):
            return key
        async with self._lock:
            now = self._clock()
            key = self._keys.get(kid)
            if (key is None or not self._fresh(now)) and self._may_refresh(now):
                await self._refresh(now)
                key = self._keys.get(kid)
            if not self._keys:
                raise AuthUnavailable(
                    "não foi possível verificar o token agora; tente de novo em instantes"
                )
        if key is None:
            raise Unauthenticated(
                "token assinado com chave desconhecida", bearer_error="invalid_token"
            )
        return key

    async def _refresh(self, now: float) -> None:
        """Busca as chaves; numa falha, mantém as já conhecidas."""
        self._last_attempt = now
        try:
            document = await self._fetch()
            keys = self._parse(document)
        except (httpx2.HTTPError, ValueError) as exc:
            logger.warning(
                "falha ao buscar as chaves do provedor de identidade: %s", type(exc).__name__
            )
            return
        self._keys = keys
        self._fetched_at = now

    async def _fetch(self) -> Any:
        if self._client is None:
            self._client = httpx2.AsyncClient(timeout=self._timeout_s, follow_redirects=False)
        response = await self._client.get(self.url, headers={"Accept": "application/json"})
        response.raise_for_status()
        if len(response.content) > _MAX_JWKS_BYTES:
            raise ValueError("documento de chaves grande demais")
        return response.json()

    def _parse(self, document: Any) -> dict[str, jwt.PyJWK]:
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError("documento de chaves inválido")
        keys: dict[str, jwt.PyJWK] = {}
        for item in document["keys"][:_MAX_JWKS_KEYS]:
            if not isinstance(item, dict):
                continue
            kid = item.get("kid")
            if not isinstance(kid, str) or not kid or item.get("use", "sig") != "sig":
                continue
            if item.get("kty") not in {"RSA", "EC"} or "d" in item:
                continue
            try:
                key = jwt.PyJWK(item)
            except jwt.PyJWTError:
                continue
            if key.algorithm_name in self.algorithms:
                keys[kid] = key
        if not keys:
            raise ValueError("nenhuma chave de assinatura utilizável")
        return keys


class TokenVerifier:
    """Verifica tokens de acesso do Keycloak (assinatura, emissor, público e validade).

    A empresa sai do claim de Organizações do Keycloak (``organization``), na
    forma de lista de aliases (``["acme"]``, mapeados por
    ``organization_aliases``) ou de objeto com o identificador
    (``{"acme": {"id": "<uuid>"}}``). Exige exatamente uma organização.
    """

    def __init__(
        self,
        settings: JwtSettings,
        *,
        jwks: JwksClient | None = None,
        organizations: OrganizationDirectory | None = None,
    ) -> None:
        self.settings = settings
        #: Vínculos organização → empresa guardados no banco (sem ele, só o mapa estático).
        self.organizations = organizations
        self.jwks = jwks or JwksClient(
            settings.jwks_url,
            algorithms=settings.algorithms,
            cache_ttl_s=settings.jwks_cache_ttl_s,
            min_refresh_interval_s=settings.jwks_min_refresh_interval_s,
            timeout_s=settings.jwks_timeout_s,
        )

    async def aclose(self) -> None:
        await self.jwks.aclose()

    async def verify(self, token: str) -> VerifiedToken:
        settings = self.settings
        if len(token) > settings.max_token_bytes:
            raise Unauthenticated("token grande demais", bearer_error="invalid_token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Unauthenticated("token malformado", bearer_error="invalid_token") from None
        alg = header.get("alg")
        kid = header.get("kid")
        if not isinstance(alg, str) or alg not in settings.algorithms:
            raise Unauthenticated(
                "algoritmo de assinatura não aceito", bearer_error="invalid_token"
            )
        if not isinstance(kid, str) or not kid:
            raise Unauthenticated("token sem identificador de chave", bearer_error="invalid_token")
        key = await self.jwks.get_key(kid)
        if key.algorithm_name != alg:
            raise Unauthenticated(
                "algoritmo de assinatura não aceito", bearer_error="invalid_token"
            )
        claims = self._decode(token, key, alg)
        typ = claims.get("typ")
        if typ is not None and (not isinstance(typ, str) or typ.lower() != "bearer"):
            raise Unauthenticated("o token não é um token de acesso", bearer_error="invalid_token")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            raise Unauthenticated(
                "token sem identificação do usuário", bearer_error="invalid_token"
            )
        company_id, linked = await self._company(claims)
        azp = claims.get("azp")
        exp = claims.get("exp")
        return VerifiedToken(
            subject=subject,
            company_id=company_id,
            client_id=azp if isinstance(azp, str) and azp else None,
            expires_at=int(exp) if isinstance(exp, int | float) else None,
            display_name=_display_name(claims),
            organization_linked=linked,
        )

    def _decode(self, token: str, key: jwt.PyJWK, alg: str) -> dict[str, Any]:
        settings = self.settings
        try:
            return jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_s,
                options={
                    "require": ["exp", "iat", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise Unauthenticated("token expirado", bearer_error="invalid_token") from None
        except jwt.ImmatureSignatureError:
            raise Unauthenticated(
                "token ainda não é válido", bearer_error="invalid_token"
            ) from None
        except jwt.InvalidAudienceError:
            raise Unauthenticated(
                "token emitido para outro destino", bearer_error="invalid_token"
            ) from None
        except jwt.InvalidIssuerError:
            raise Unauthenticated(
                "emissor do token não reconhecido", bearer_error="invalid_token"
            ) from None
        except jwt.MissingRequiredClaimError:
            raise Unauthenticated(
                "token sem informações obrigatórias", bearer_error="invalid_token"
            ) from None
        except jwt.PyJWTError:
            raise Unauthenticated("token inválido", bearer_error="invalid_token") from None

    async def _linked(self, keys: list[str]) -> list[UUID]:
        """As empresas que o banco liga ao id e ao apelido da organização."""
        if self.organizations is None:
            return []
        found: list[UUID] = []
        for key in keys:
            try:
                company = await self.organizations.company_for(key)
            except Exception as exc:  # o banco fora do ar: falha fechada, sem detalhes
                logger.warning("falha ao conferir a organização do token: %s", type(exc).__name__)
                raise AuthUnavailable(
                    "não foi possível conferir a empresa agora; tente de novo em instantes"
                ) from None
            if company is not None:
                found.append(company)
        return found

    async def _company(self, claims: Mapping[str, Any]) -> tuple[UUID, bool]:
        """A empresa do token e se ela veio de um vínculo explícito (mapa ou banco)."""
        raw = claims.get(self.settings.organization_claim)
        entries: list[tuple[str, Any]]
        if isinstance(raw, str):
            entries = [(raw, None)]
        elif isinstance(raw, list):
            if not all(isinstance(alias, str) for alias in raw):
                raise AccessDenied("informação de empresa inválida no token")
            entries = [(alias, None) for alias in raw]
        elif isinstance(raw, dict):
            entries = [
                (alias, value.get("id") if isinstance(value, dict) else None)
                for alias, value in raw.items()
            ]
        elif raw is None:
            raise AccessDenied("o token não informa a empresa do usuário")
        else:
            raise AccessDenied("informação de empresa inválida no token")
        if len(entries) != 1:
            raise AccessDenied("o usuário precisa pertencer a exatamente uma empresa")
        alias, raw_id = entries[0]
        organization_id: UUID | None = None
        if raw_id is not None:
            if not isinstance(raw_id, str):
                raise AccessDenied("identificador de empresa inválido no token")
            organization_id = _as_uuid(raw_id)
            if organization_id is None:
                raise AccessDenied("identificador de empresa inválido no token")
        mapped = self.settings.organization_aliases.get(alias)
        # O id só como id (a forma canônica) e o apelido só como apelido: um apelido no
        # formato de UUID nunca é procurado no banco (lá, esse formato é sempre um id).
        keys = [str(organization_id)] if organization_id is not None else []
        if _as_uuid(alias) is None:
            keys.append(alias)
        linked = await self._linked(keys)
        candidates = set(linked) | ({mapped} if mapped is not None else set())
        if len(candidates) > 1:
            logger.warning("organização do token com empresas diferentes no mapa e no banco")
            raise AccessDenied(_UNKNOWN_COMPANY)
        if linked:
            return linked[0], True
        if organization_id is None:
            if mapped is None:
                raise AccessDenied(_UNKNOWN_COMPANY)
            return mapped, True
        if mapped is not None and mapped != organization_id:
            logger.warning("organização do token diverge do mapeamento configurado")
            raise AccessDenied(_UNKNOWN_COMPANY)
        return organization_id, mapped is not None


def _as_uuid(value: str) -> UUID | None:
    """O UUID de ``value`` (em qualquer forma que o ``UUID`` aceita), ou ``None``."""
    try:
        return UUID(value)
    except ValueError:
        return None


def _display_name(claims: Mapping[str, Any]) -> str | None:
    """O nome da pessoa no token, sem controles e cortado (só para o primeiro login)."""
    for claim in ("name", "preferred_username"):
        value = claims.get(claim)
        if isinstance(value, str):
            cleaned = " ".join("".join(c for c in value if c.isprintable()).split())
            if cleaned:
                return cleaned[:_MAX_DISPLAY_NAME]
    return None
