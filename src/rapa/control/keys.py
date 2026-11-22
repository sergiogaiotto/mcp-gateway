"""As chaves dos artefatos cifrados: a chave da empresa embrulha a chave de cada execução.

Cada execução com artefatos no store de objetos tem uma chave de dados aleatória
(:func:`rapa.control.cipher.new_data_key`), guardada só embrulhada, numa linha de
``run_keys`` (empresa, execução, chave embrulhada e versão; revisão 0009), pelo
:class:`RunKeyStore` (:class:`PgRunKeys`). Quem embrulha é a chave da empresa
(:class:`CompanyKeys`, ``RAPA_CHAVES``):

- ``openbao`` (:class:`TransitCompanyKeys`): a chave transit ``empresa-<uuid>`` do
  OpenBao (``RAPA_BAO_TRANSIT``), criada pelo primeiro ``encrypt`` da empresa com os
  padrões do transit (``exportable=false``, sem cópia em claro); o RaPA nunca vê a chave
  da empresa, só manda embrulhar e desembrulhar;
- ``local`` (:class:`LocalCompanyKeys`, só para desenvolvimento e testes): uma chave por
  empresa derivada (HKDF-SHA256, com o uuid da empresa) da chave mestra
  ``RAPA_CHAVE_ARTEFATOS`` (32 caracteres ou mais), e AES-256-GCM com a empresa e a
  execução como dados associados.

Apagar a linha de ``run_keys`` (a retenção, depois de apagar os objetos; a saída da
empresa) destrói a chave da execução: o que sobrar dos objetos (num backup, por
exemplo) não abre mais. Destruir a chave da empresa (a saída da empresa, no OpenBao)
fecha todas as execuções dela de uma vez.
"""

import base64
import os
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rapa.control.db.schema import run_keys
from rapa.control.db.session import Database
from rapa.control.vault import OpenBaoClient, VaultError, transit_key_name

__all__ = [
    "MIN_MASTER_KEY",
    "CompanyKeys",
    "KeyUnavailable",
    "LocalCompanyKeys",
    "PgRunKeys",
    "RunKeyStore",
    "TransitCompanyKeys",
    "WrappedKey",
]

#: Tamanho mínimo da chave mestra local (``RAPA_CHAVE_ARTEFATOS``).
MIN_MASTER_KEY = 32
_DATA_KEY = 32
_LOCAL_PREFIX = b"local1:"
_LOCAL_INFO = b"rapa-artefatos:empresa:"
_LOCAL_AAD = b"rapa-chave-de-execucao\x00"
_TRANSIT_CIPHERTEXT = re.compile(r"^vault:v([0-9]{1,9}):[A-Za-z0-9+/=]+$")


class KeyUnavailable(RuntimeError):
    """A chave da execução não pôde ser embrulhada ou desembrulhada (sem valores)."""


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """A chave de dados de uma execução, embrulhada pela chave da empresa."""

    wrapped: bytes
    version: int


class CompanyKeys(Protocol):
    """A chave da empresa: embrulha e desembrulha a chave de dados de cada execução."""

    async def wrap(self, company_id: UUID, run_id: UUID, data_key: bytes) -> WrappedKey: ...

    async def unwrap(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> bytes: ...


class RunKeyStore(Protocol):
    """Onde ficam as chaves embrulhadas das execuções (``run_keys``)."""

    async def get(self, company_id: UUID, run_id: UUID) -> WrappedKey | None: ...

    async def add(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> WrappedKey:
        """Inclui a chave; se a execução já tem uma, devolve a que já estava (ela vale)."""
        ...


def _check_ids(company_id: UUID, run_id: UUID) -> None:
    if not isinstance(company_id, UUID) or not isinstance(run_id, UUID):
        raise TypeError("a empresa e a execução precisam ser UUIDs")


class LocalCompanyKeys:
    """Chave por empresa derivada da chave mestra local (só desenvolvimento e testes)."""

    def __init__(self, master_key: str | bytes) -> None:
        raw = master_key.encode() if isinstance(master_key, str) else bytes(master_key)
        if len(raw) < MIN_MASTER_KEY:
            raise ValueError(
                f"a chave mestra dos artefatos precisa de {MIN_MASTER_KEY} caracteres ou mais"
            )
        self._master = raw

    def __repr__(self) -> str:
        return "LocalCompanyKeys(chave=<oculta>)"

    def _company_key(self, company_id: UUID) -> AESGCM:
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"rapa-artefatos-v1",
            info=_LOCAL_INFO + company_id.bytes,
        ).derive(self._master)
        return AESGCM(derived)

    async def wrap(self, company_id: UUID, run_id: UUID, data_key: bytes) -> WrappedKey:
        _check_ids(company_id, run_id)
        if len(data_key) != _DATA_KEY:
            raise ValueError("a chave de dados precisa ter 256 bits")
        nonce = os.urandom(12)
        sealed = self._company_key(company_id).encrypt(
            nonce, data_key, _LOCAL_AAD + company_id.bytes + run_id.bytes
        )
        return WrappedKey(_LOCAL_PREFIX + nonce + sealed, 1)

    async def unwrap(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> bytes:
        _check_ids(company_id, run_id)
        raw = key.wrapped
        if not raw.startswith(_LOCAL_PREFIX) or key.version != 1:
            raise KeyUnavailable("a chave da execução não é desta chave mestra")
        body = raw[len(_LOCAL_PREFIX) :]
        try:
            data_key = self._company_key(company_id).decrypt(
                body[:12], body[12:], _LOCAL_AAD + company_id.bytes + run_id.bytes
            )
        except InvalidTag, ValueError:
            raise KeyUnavailable("a chave da execução não abre com a chave da empresa") from None
        if len(data_key) != _DATA_KEY:
            raise KeyUnavailable("a chave da execução está fora do formato")
        return data_key


class TransitCompanyKeys:
    """A chave transit ``empresa-<uuid>`` do OpenBao embrulha a chave de cada execução.

    A chave da empresa nasce no primeiro embrulho: o ``encrypt`` do transit cria a chave
    que não existe (com ``create`` na política do despachante), com os padrões do transit
    (``aes256-gcm96``, ``exportable=false``, sem cópia em claro). O despachante nunca
    escreve em ``<transit>/keys/``: aquele caminho não confere se a chave existe, todo
    POST nele pede ``update``, e ``update`` ali deixaria tornar a chave exportável.
    Dois despachantes ao mesmo tempo criam a mesma chave uma vez só (o transit trava).
    """

    def __init__(self, client: OpenBaoClient) -> None:
        self.client = client

    @property
    def _mount(self) -> str:
        return self.client.settings.transit_mount

    async def wrap(self, company_id: UUID, run_id: UUID, data_key: bytes) -> WrappedKey:
        _check_ids(company_id, run_id)
        if len(data_key) != _DATA_KEY:
            raise ValueError("a chave de dados precisa ter 256 bits")
        name = transit_key_name(company_id)
        try:
            body = await self.client.request(
                "POST",
                f"{self._mount}/encrypt/{name}",
                # "type" só vale quando o encrypt cria a chave (a primeira vez).
                json={"plaintext": base64.b64encode(data_key).decode(), "type": "aes256-gcm96"},
                what="embrulhar a chave da execução",
                absent=(),
            )
        except VaultError as exc:
            raise KeyUnavailable(str(exc)) from None
        ciphertext = ((body or {}).get("data") or {}).get("ciphertext")
        if (
            not isinstance(ciphertext, str)
            or (match := _TRANSIT_CIPHERTEXT.fullmatch(ciphertext)) is None
        ):
            raise KeyUnavailable("o OpenBao devolveu uma chave embrulhada fora do formato")
        return WrappedKey(ciphertext.encode(), int(match.group(1)))

    async def unwrap(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> bytes:
        _check_ids(company_id, run_id)
        try:
            ciphertext = key.wrapped.decode("ascii")
        except UnicodeDecodeError:
            raise KeyUnavailable("a chave da execução está fora do formato") from None
        if not _TRANSIT_CIPHERTEXT.fullmatch(ciphertext):
            raise KeyUnavailable("a chave da execução está fora do formato")
        try:
            body = await self.client.request(
                "POST",
                f"{self._mount}/decrypt/{transit_key_name(company_id)}",
                json={"ciphertext": ciphertext},
                what="desembrulhar a chave da execução",
                absent=(),
            )
        except VaultError as exc:
            raise KeyUnavailable(str(exc)) from None
        plaintext = ((body or {}).get("data") or {}).get("plaintext")
        try:
            data_key = (
                base64.b64decode(plaintext, validate=True) if isinstance(plaintext, str) else b""
            )
        except ValueError:
            data_key = b""
        if len(data_key) != _DATA_KEY:
            raise KeyUnavailable("o OpenBao devolveu uma chave de execução fora do formato")
        return data_key


class PgRunKeys:
    """``run_keys`` no PostgreSQL, sempre fixado na empresa.

    Serve aos três papéis: o despachante inclui e lê (ao registrar os artefatos), a
    aplicação só lê (ao servir um artefato) e o dono lê na exportação da empresa.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, company_id: UUID, run_id: UUID) -> WrappedKey | None:
        _check_ids(company_id, run_id)
        async with self.db.tenant_session(company_id) as session:
            row = (
                await session.execute(
                    select(run_keys.c.wrapped_key, run_keys.c.key_version).where(
                        run_keys.c.company_id == company_id, run_keys.c.run_id == run_id
                    )
                )
            ).one_or_none()
        return None if row is None else WrappedKey(bytes(row[0]), int(row[1]))

    async def add(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> WrappedKey:
        _check_ids(company_id, run_id)
        async with self.db.tenant_session(company_id) as session:
            inserted = await session.scalar(
                pg_insert(run_keys)
                .values(
                    company_id=company_id,
                    run_id=run_id,
                    wrapped_key=key.wrapped,
                    key_version=key.version,
                )
                .on_conflict_do_nothing()
                .returning(run_keys.c.run_id)
            )
        if inserted is not None:
            return key
        existing = await self.get(company_id, run_id)
        if existing is None:  # pragma: no cover - a linha não some sem a retenção
            raise KeyUnavailable("a chave da execução sumiu enquanto era guardada")
        return existing
