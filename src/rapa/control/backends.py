"""A escolha do cofre, do armazenamento dos artefatos e das chaves, pela configuração.

Os nomes (``docs/CONTRIBUINDO.md``, regras do cofre e do armazenamento):

- ``RAPA_COFRE`` = ``nenhum`` (padrão fora do desenvolvimento: robôs com credenciais
  falham sem digitar nada), ``dev`` (o arquivo ``RAPA_SEGREDOS_DEV``, só com
  ``RAPA_DEV=1``; padrão quando ele está definido) ou ``openbao``;
- ``RAPA_BAO_ENDERECO`` (``https://...``; ``http://`` só em desenvolvimento ou no
  próprio computador), ``RAPA_BAO_CA``, ``RAPA_BAO_KV`` (padrão ``rapa``),
  ``RAPA_BAO_TRANSIT`` (padrão ``rapa-transit``), ``RAPA_BAO_PAPEL``, ``RAPA_BAO_AUTH``
  (padrão ``kubernetes``), ``RAPA_BAO_TOKEN_SA`` (padrão
  ``/var/run/secrets/rapa/bao/token``) e ``RAPA_BAO_TOKEN_ARQUIVO`` (só desenvolvimento);
- ``RAPA_ARMAZENAMENTO`` = ``local`` (padrão: a pasta ``RAPA_PASTA_ARTEFATOS``) ou
  ``s3``, com ``RAPA_S3_ENDERECO``, ``RAPA_S3_REGIAO`` (padrão ``us-east-1``),
  ``RAPA_S3_BUCKET``, ``RAPA_S3_CA``, ``RAPA_S3_ESTILO_CAMINHO`` (padrão 1) e as
  credenciais em ``AWS_ACCESS_KEY_ID`` e ``AWS_SECRET_ACCESS_KEY`` (de um Secret do
  Kubernetes; passadas direto ao cliente, que nunca procura outras: sem elas, a partida
  falha);
- ``RAPA_CHAVES`` = ``openbao`` (padrão com ``RAPA_COFRE=openbao``: a chave transit
  ``empresa-<uuid>``) ou ``local`` (a chave mestra ``RAPA_CHAVE_ARTEFATOS``, 32
  caracteres ou mais, só para desenvolvimento e testes).

Tudo é conferido na partida (:class:`BackendSettings`): ``s3`` exige endereço, bucket e
as credenciais; ``openbao`` (no cofre ou nas chaves) exige o endereço e o papel (ou, só
em desenvolvimento, o arquivo do token); as chaves ``local`` com o S3 exigem
``RAPA_DEV=1`` ou a chave mestra. Em desenvolvimento sem a chave mestra, a chave local é
sorteada a cada processo (com um aviso): os artefatos de antes de reiniciar não abrem.

:class:`Backends` monta, uma vez por processo, o que a configuração pede: o cliente do
OpenBao, a chave das empresas, o cliente S3, o store dos artefatos (por banco: o da
aplicação lê, o do despachante registra), a fonte de segredos do lançador, o destruidor
de chaves da saída da empresa e as checagens de prontidão (:class:`ReadinessCheck`:
``openbao`` e ``s3``, usadas por ``/saude/pronto``).
"""

import logging
import os
import secrets
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from rapa.control.artifacts import ArtifactStore, LocalArtifactStore, S3ArtifactStore, S3Readiness
from rapa.control.db.session import Database
from rapa.control.health import ReadinessCheck
from rapa.control.keys import (
    MIN_MASTER_KEY,
    CompanyKeys,
    LocalCompanyKeys,
    PgRunKeys,
    TransitCompanyKeys,
)
from rapa.control.launcher import NoSecrets, SecretSource, StaticSecretSource
from rapa.control.services.lifecycle import KeyDestroyer, LocalKeyDestroyer
from rapa.control.vault import (
    BaoReadiness,
    BaoSettings,
    OpenBaoClient,
    OpenBaoKeyDestroyer,
    OpenBaoSecretSource,
)

__all__ = [
    "BackendSettings",
    "Backends",
    "KeysKind",
    "ReadinessCheck",
    "S3Settings",
    "StorageKind",
    "VaultKind",
    "safe_errors",
]

logger = logging.getLogger("rapa.control.backends")

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TRUE = {"1", "true", "sim", "yes", "on"}
_FALSE = {"0", "false", "nao", "não", "no", "off"}


class VaultKind(StrEnum):
    NONE = "nenhum"
    DEV = "dev"
    OPENBAO = "openbao"


class StorageKind(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class KeysKind(StrEnum):
    LOCAL = "local"
    OPENBAO = "openbao"


class S3Settings(BaseModel):
    """O bucket dos artefatos (``RAPA_S3_*``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str = Field(min_length=1)
    bucket: str = Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]+[a-z0-9]$")
    region: str = Field(default="us-east-1", pattern=r"^[a-z0-9-]{1,40}$")
    ca_file: Path | None = None
    path_style: bool = True
    access_key: str = Field(min_length=1, repr=False)
    secret_key: str = Field(min_length=1, repr=False)
    dev: bool = False

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("RAPA_S3_ENDERECO precisa ser um endereço http(s)://host[:porta]")
        if parts.path or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("RAPA_S3_ENDERECO é só esquema, host e porta")
        return value

    @model_validator(mode="after")
    def _check(self) -> S3Settings:
        host = urlsplit(self.endpoint).hostname or ""
        if self.endpoint.startswith("http://") and not (self.dev or host in _LOOPBACK):
            raise ValueError("o S3 precisa de https:// fora do desenvolvimento")
        return self


class BackendSettings(BaseModel):
    """O cofre, o armazenamento e as chaves (veja o início do módulo)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dev: bool = False
    #: ``None``: ``dev`` com o arquivo de segredos de desenvolvimento, senão ``nenhum``.
    vault: VaultKind | None = None
    storage: StorageKind = StorageKind.LOCAL
    #: ``None``: ``openbao`` com o cofre no OpenBao, senão ``local``.
    keys: KeysKind | None = None
    bao: BaoSettings | None = None
    s3: S3Settings | None = None
    artifact_key: str | None = Field(default=None, min_length=MIN_MASTER_KEY, repr=False)

    @property
    def keys_kind(self) -> KeysKind:
        if self.keys is not None:
            return self.keys
        return KeysKind.OPENBAO if self.vault is VaultKind.OPENBAO else KeysKind.LOCAL

    @property
    def uses_bao(self) -> bool:
        """O OpenBao entra: o cofre dos segredos ou a chave das empresas."""
        return self.vault is VaultKind.OPENBAO or self.keys_kind is KeysKind.OPENBAO

    @model_validator(mode="after")
    def _check(self) -> BackendSettings:
        if self.vault is VaultKind.DEV and not self.dev:
            raise ValueError("RAPA_COFRE=dev só vale com RAPA_DEV=1")
        if self.uses_bao and self.bao is None:
            raise ValueError(
                "o OpenBao precisa do endereço e do papel (RAPA_BAO_ENDERECO e RAPA_BAO_PAPEL)"
            )
        if self.storage is StorageKind.S3 and self.s3 is None:
            raise ValueError(
                "RAPA_ARMAZENAMENTO=s3 precisa de RAPA_S3_ENDERECO, RAPA_S3_BUCKET, "
                "AWS_ACCESS_KEY_ID e AWS_SECRET_ACCESS_KEY"
            )
        if (
            self.storage is StorageKind.S3
            and self.keys_kind is KeysKind.LOCAL
            and not (self.dev or self.artifact_key)
        ):
            raise ValueError(
                "as chaves locais dos artefatos só valem com RAPA_DEV=1 ou com a chave mestra "
                "RAPA_CHAVE_ARTEFATOS; em produção, use RAPA_CHAVES=openbao"
            )
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> BackendSettings:
        """Lê os nomes do início do módulo (e ``RAPA_DEV`` e ``RAPA_SEGREDOS_DEV``).

        Levanta ``ValueError`` com o que está errado, nunca com os valores lidos (uma
        chave curta demais, por exemplo, não aparece na mensagem).
        """
        try:
            return cls._from_env(os.environ if env is None else env)
        except ValidationError as exc:
            raise ValueError(safe_errors(exc)) from None

    @classmethod
    def _from_env(cls, env: Mapping[str, str]) -> BackendSettings:
        dev = _flag(env, "RAPA_DEV", False)
        data: dict[str, Any] = {"dev": dev}
        vault = _choice(env, "RAPA_COFRE", VaultKind)
        if vault is None and dev and env.get("RAPA_SEGREDOS_DEV", "").strip():
            vault = VaultKind.DEV
        # Sem RAPA_COFRE: o arquivo de desenvolvimento, se houver (e só com RAPA_DEV=1, o
        # que o servidor confere), senão nenhum.
        data["vault"] = vault or (
            None if env.get("RAPA_SEGREDOS_DEV", "").strip() else VaultKind.NONE
        )
        data["storage"] = _choice(env, "RAPA_ARMAZENAMENTO", StorageKind) or StorageKind.LOCAL
        data["keys"] = _choice(env, "RAPA_CHAVES", KeysKind)
        if key := env.get("RAPA_CHAVE_ARTEFATOS", ""):
            data["artifact_key"] = key
        if address := env.get("RAPA_BAO_ENDERECO", "").strip():
            bao: dict[str, Any] = {"address": address, "dev": dev}
            for field, name in (
                ("kv_mount", "RAPA_BAO_KV"),
                ("transit_mount", "RAPA_BAO_TRANSIT"),
                ("role", "RAPA_BAO_PAPEL"),
                ("auth_mount", "RAPA_BAO_AUTH"),
            ):
                if value := env.get(name, "").strip():
                    bao[field] = value
            for field, name in (
                ("ca_file", "RAPA_BAO_CA"),
                ("sa_token_file", "RAPA_BAO_TOKEN_SA"),
                ("token_file", "RAPA_BAO_TOKEN_ARQUIVO"),
            ):
                if value := env.get(name, "").strip():
                    bao[field] = Path(value)
            data["bao"] = BaoSettings.model_validate(bao)
        if data["storage"] is StorageKind.S3:
            endpoint = env.get("RAPA_S3_ENDERECO", "").strip()
            bucket = env.get("RAPA_S3_BUCKET", "").strip()
            access = env.get("AWS_ACCESS_KEY_ID", "")
            secret = env.get("AWS_SECRET_ACCESS_KEY", "")
            if endpoint and bucket and access and secret:
                s3: dict[str, Any] = {
                    "endpoint": endpoint,
                    "bucket": bucket,
                    "access_key": access,
                    "secret_key": secret,
                    "path_style": _flag(env, "RAPA_S3_ESTILO_CAMINHO", True),
                    "dev": dev,
                }
                if region := env.get("RAPA_S3_REGIAO", "").strip():
                    s3["region"] = region
                if ca := env.get("RAPA_S3_CA", "").strip():
                    s3["ca_file"] = Path(ca)
                data["s3"] = S3Settings.model_validate(s3)
        return cls.model_validate(data)


def safe_errors(exc: ValidationError) -> str:
    """O texto de um erro de validação sem os valores recebidos (que podem ser segredos)."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        where = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "valor inválido")).removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "configuração inválida: " + "; ".join(parts)


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} precisa ser 1 ou 0")


def _choice[E: StrEnum](env: Mapping[str, str], name: str, kind: type[E]) -> E | None:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return None
    try:
        return kind(raw)
    except ValueError:
        options = ", ".join(item.value for item in kind)
        raise ValueError(f"{name} precisa ser um de: {options}") from None


#: A chave local sorteada em desenvolvimento sem ``RAPA_CHAVE_ARTEFATOS`` (uma por processo).
_DEV_KEY: list[str] = []


def _dev_key() -> str:
    if not _DEV_KEY:
        logger.warning(
            "RAPA_CHAVE_ARTEFATOS não definida: em desenvolvimento, a chave dos artefatos "
            "cifrados é sorteada neste processo e os artefatos não abrem depois de reiniciar"
        )
        _DEV_KEY.append(secrets.token_urlsafe(48))
    return _DEV_KEY[0]


class Backends:
    """O que a configuração pede, montado uma vez e compartilhado (veja o início do módulo)."""

    def __init__(
        self,
        settings: BackendSettings,
        *,
        bao: OpenBaoClient | None = None,
        s3_client: Any = None,
    ) -> None:
        self.settings = settings
        self._bao = bao
        self._s3 = s3_client
        self._keys: CompanyKeys | None = None

    @property
    def bao(self) -> OpenBaoClient | None:
        if self._bao is None and self.settings.bao is not None:
            self._bao = OpenBaoClient(self.settings.bao)
        return self._bao

    def _require_bao(self) -> OpenBaoClient:
        client = self.bao
        if client is None:  # pragma: no cover - barrado pela validação
            raise ValueError("o OpenBao não está configurado")
        return client

    @property
    def company_keys(self) -> CompanyKeys:
        if self._keys is None:
            if self.settings.keys_kind is KeysKind.OPENBAO:
                self._keys = TransitCompanyKeys(self._require_bao())
            else:
                self._keys = LocalCompanyKeys(self.settings.artifact_key or _dev_key())
        return self._keys

    @property
    def s3_client(self) -> Any:
        if self._s3 is None and self.settings.s3 is not None:
            self._s3 = _s3_client(self.settings.s3)
        return self._s3

    def artifact_store(self, artifacts_dir: Path, db: Database) -> ArtifactStore:
        """O store dos artefatos: ``db`` é o banco de quem usa (as chaves das execuções)."""
        s3 = self.settings.s3
        if self.settings.storage is StorageKind.S3 and s3 is not None:
            return S3ArtifactStore(
                client=self.s3_client,
                bucket=s3.bucket,
                keys=self.company_keys,
                run_keys=PgRunKeys(db),
                staging_root=artifacts_dir,
            )
        return LocalArtifactStore(artifacts_dir)

    def secret_source(self, db: Database, dev_secrets_file: Path | None) -> SecretSource:
        """A fonte de segredos do lançador; ``db`` é o banco do despachante."""
        vault = self.settings.vault
        if vault is None:
            vault = VaultKind.DEV if dev_secrets_file is not None else VaultKind.NONE
        if vault is VaultKind.DEV:
            if dev_secrets_file is None:
                raise ValueError("RAPA_COFRE=dev precisa do arquivo RAPA_SEGREDOS_DEV")
            return StaticSecretSource.from_file(dev_secrets_file)
        if vault is VaultKind.OPENBAO:
            return OpenBaoSecretSource(db, self._require_bao())
        return NoSecrets()

    def key_destroyer(self, store: ArtifactStore | None = None) -> KeyDestroyer:
        """O destruidor de chaves da saída da empresa (só registra, sem o OpenBao)."""
        secrets_in_bao = self.settings.vault is VaultKind.OPENBAO
        transit = self.settings.keys_kind is KeysKind.OPENBAO
        if not (secrets_in_bao or transit):
            return LocalKeyDestroyer()
        s3_store = store if isinstance(store, S3ArtifactStore) else None
        return OpenBaoKeyDestroyer(
            self._require_bao(), secrets=secrets_in_bao, transit=transit, store=s3_store
        )

    def readiness_checks(self) -> tuple[ReadinessCheck, ...]:
        """As checagens de prontidão do que está configurado (``openbao`` e ``s3``)."""
        checks: list[ReadinessCheck] = []
        if self.settings.uses_bao and self.bao is not None:
            checks.append(BaoReadiness(self.bao))
        if self.settings.storage is StorageKind.S3 and self.settings.s3 is not None:
            checks.append(S3Readiness(self.s3_client, self.settings.s3.bucket))
        return tuple(checks)

    async def aclose(self) -> None:
        if self._bao is not None:
            await self._bao.aclose()
        if self._s3 is not None:
            close = getattr(self._s3, "close", None)
            if callable(close):
                close()


def _s3_client(settings: S3Settings) -> Any:
    """O cliente S3 do boto3, só com as credenciais dadas (nunca procura outras)."""
    import boto3
    from botocore.config import Config

    session = boto3.session.Session(
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
    )
    return session.client(
        "s3",
        endpoint_url=settings.endpoint,
        verify=str(settings.ca_file) if settings.ca_file is not None else True,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if settings.path_style else "virtual"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )
