"""Auxiliares dos testes da operação por empresa."""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.services import PgPrincipalResolver, PgProvisioning
from rapa.control.services.lifecycle import FreezeReport, freeze_company
from rapa.core.contracts import Principal, Role

__all__ = [
    "audit_actions",
    "company_counts",
    "copy_row",
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


#: Qual linha copiar, quando nem toda linha da tabela pode ser incluída de novo: uma
#: versão nova sempre nasce rascunho (o gatilho das aprovações da revisão 0006 recusa
#: incluir uma versão já publicada antes de a chave primária ser conferida), e só uma
#: versão validada recebe aprovações.
_COPYABLE = {
    "robot_versions": "status = 'draft'",
    "version_approvals": (
        "EXISTS (SELECT 1 FROM robot_versions v WHERE v.company_id = version_approvals.company_id"
        " AND v.robot_id = version_approvals.robot_id"
        " AND v.version_no = version_approvals.version_no AND v.status = 'validated')"
    ),
}


async def copy_row(db: Database, company: Any, table: str) -> tuple[str | None, str]:
    """Inclui, como a aplicação e sem os serviços, a cópia de uma linha da empresa.

    Devolve o código do erro (``None`` se passou) e a mensagem. A cópia repete a chave
    primária: numa empresa ativa, quem recusa é a chave (23505); o gatilho da empresa
    congelada roda antes dela (23514).
    """
    where = f"AND {_COPYABLE[table]} " if table in _COPYABLE else ""
    async with db.tenant_session(company) as probe:
        columns = await probe.scalar(
            text(
                "SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) FROM pg_attribute "
                "WHERE attrelid = to_regclass('public.' || :t) AND attnum > 0 "
                "AND NOT attisdropped AND attgenerated = ''"
            ),
            {"t": table},
        )
    try:
        async with db.tenant_session(company) as session:
            await session.execute(
                text(
                    f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table} "  # noqa: S608
                    f"WHERE company_id = :c {where}LIMIT 1"
                ),
                {"c": company},
            )
    except DBAPIError as exc:
        return getattr(exc.orig, "sqlstate", None), str(exc.orig)
    return None, ""
