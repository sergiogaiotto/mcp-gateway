"""Auxiliares dos testes da operação por empresa."""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgPrincipalResolver, PgProvisioning
from rapa.control.services.lifecycle import FreezeReport, freeze_company
from rapa.core.contracts import Principal, Role

__all__ = [
    "audit_actions",
    "company_counts",
    "freeze",
    "person",
    "set_company_rows",
]

#: Toda tabela com company_id, sem as partições (o mesmo critério da exclusão).
_TENANT_TABLES = text(
    "SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
    "AND c.relkind IN ('r', 'p') AND NOT c.relispartition AND EXISTS ("
    " SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'company_id'"
    " AND NOT a.attisdropped) ORDER BY c.relname"
)


async def freeze(
    owner_engine: AsyncEngine, company_id: UUID, reason: str = "contrato encerrado"
) -> FreezeReport:
    return await freeze_company(owner_engine, company_id, reason=reason, requested_by="teste")


async def person(
    db: Database, company_id: UUID, subject: str, *grants: tuple[Role, UUID | None]
) -> Principal:
    """Um usuário novo com os papéis dados, já como principal resolvido do banco."""
    provisioning = PgProvisioning(db)
    user_id = await provisioning.create_user(company_id, subject, subject.split("@")[0])
    for role, area_id in grants:
        await provisioning.add_membership(company_id, user_id, role, area_id)
    principal = await PgPrincipalResolver(db).resolve(subject, company_id)
    assert principal is not None
    return principal


async def company_counts(superuser: AsyncEngine, company_id: UUID) -> dict[str, int]:
    """Linhas da empresa em cada tabela com company_id (e em companies)."""
    async with superuser.connect() as conn:
        tables = (await conn.scalars(_TENANT_TABLES)).all()
        counts = {
            table: int(
                await conn.scalar(
                    text(f'SELECT count(*) FROM "{table}" WHERE company_id = :c'),  # noqa: S608
                    {"c": company_id},
                )
                or 0
            )
            for table in tables
        }
        counts["companies"] = int(
            await conn.scalar(
                text("SELECT count(*) FROM companies WHERE id = :c"), {"c": company_id}
            )
            or 0
        )
    return counts


async def audit_actions(superuser: AsyncEngine, company_id: UUID) -> list[str]:
    async with superuser.connect() as conn:
        return list(
            (
                await conn.scalars(
                    text("SELECT action FROM audit_log WHERE company_id = :c ORDER BY seq"),
                    {"c": company_id},
                )
            ).all()
        )


async def set_company_rows(
    superuser: AsyncEngine, statement: str, *, bypass_guards: bool = False, **params: Any
) -> None:
    """Ajusta linhas como superusuário (cenários com horários escolhidos).

    ``bypass_guards`` desliga as travas só nesta transação (para mudar a data de uma linha
    de tabela que só aceita inclusões, por exemplo).
    """
    async with superuser.begin() as conn:
        if bypass_guards:
            await conn.execute(text("SET LOCAL session_replication_role = replica"))
        await conn.execute(text(statement), params)
