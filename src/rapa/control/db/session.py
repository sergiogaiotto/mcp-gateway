"""Conexões com o PostgreSQL do plano de controle.

Toda consulta de dados de empresa roda numa transação aberta por
:func:`tenant_session`, que fixa a empresa com
``set_config('rapa.company_id', ..., true)``. O terceiro argumento faz o valor
valer só até o fim da transação: uma conexão devolvida ao pool nunca carrega a
empresa da requisição anterior, e uma transação sem empresa não enxerga nenhuma
linha (as políticas falham fechadas).

O despachante usa :func:`dispatcher_session`, sem empresa, com um papel que
ignora as políticas (``BYPASSRLS``) mas só tem acesso às tabelas da fila.
"""

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

__all__ = [
    "TENANT_SETTING",
    "Database",
    "create_engine",
    "dispatcher_session",
    "psycopg_url",
    "tenant_session",
]

#: Parâmetro de configuração lido pelas políticas de isolamento por linha.
TENANT_SETTING = "rapa.company_id"

_SET_TENANT = text("SELECT set_config('rapa.company_id', :company_id, true)")
_CLEAR_TENANT = text("SELECT set_config('rapa.company_id', '', true)")

_ACCEPTED_SCHEMES = {"postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg_async"}


def psycopg_url(url: str) -> str:
    """Converte ``postgresql://...`` para o driver psycopg 3 do SQLAlchemy.

    O mesmo nome de driver serve ao engine síncrono (migrações) e ao assíncrono.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    parsed = make_url(url)
    if parsed.drivername not in _ACCEPTED_SCHEMES:
        raise ValueError(
            f"URL de banco não suportada: use postgresql:// (recebido {parsed.drivername}://)"
        )
    return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """Cria o engine assíncrono (psycopg 3) a partir de uma URL ``postgresql://``.

    Argumentos extras vão para :func:`sqlalchemy.ext.asyncio.create_async_engine`
    (por exemplo, ``poolclass`` ou ``pool_size``).
    """
    kwargs.setdefault("json_serializer", _json_dumps)
    kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(psycopg_url(url), **kwargs)


def _company_text(company_id: UUID) -> str:
    if not isinstance(company_id, UUID):
        raise TypeError("a empresa deve ser um UUID vindo do principal autenticado")
    return str(company_id)


@asynccontextmanager
async def tenant_session(engine: AsyncEngine, company_id: UUID) -> AsyncIterator[AsyncSession]:
    """Abre uma transação fixada na empresa ``company_id``.

    A transação é confirmada na saída normal e desfeita se houver exceção. A
    empresa vale só dentro dela.
    """
    value = _company_text(company_id)
    async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as session:
        async with session.begin():
            await session.execute(_SET_TENANT, {"company_id": value})
            yield session


@asynccontextmanager
async def dispatcher_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Abre uma transação sem empresa, para o despachante."""
    async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as session:
        async with session.begin():
            await session.execute(_CLEAR_TENANT)
            yield session


class Database:
    """Um engine e os dois tipos de transação usados pelos serviços."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> Database:
        return cls(create_engine(url, **kwargs))

    def tenant_session(self, company_id: UUID) -> AbstractAsyncContextManager[AsyncSession]:
        """Transação fixada na empresa (ver :func:`tenant_session`)."""
        return tenant_session(self.engine, company_id)

    def dispatcher_session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Transação sem empresa, para o despachante (ver :func:`dispatcher_session`)."""
        return dispatcher_session(self.engine)

    async def dispose(self) -> None:
        await self.engine.dispose()
