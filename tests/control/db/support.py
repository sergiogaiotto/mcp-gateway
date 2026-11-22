"""Auxiliares dos testes da persistência: servidor, empresas, robôs e fila limpa."""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from uuid import UUID

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.db.roles import RoleNames
from rapa.control.services import PgPlatform, PgPrincipalResolver, PgProvisioning
from rapa.core.contracts import Principal, RobotVersionInfo, Role
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.robot import Manifest
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"
PROTOCOLO_INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}

#: As ações da auditoria do cadastro (``PgProvisioning``).
PROVISIONING_ACTIONS = frozenset(
    {"empresa.criada", "area.criada", "usuario.criado", "usuario.papeis_alterados"}
)
#: Linhas de auditoria que :func:`make_tenant` deixa: a empresa, 2 áreas, 6 pessoas e 8 papéis.
TENANT_AUDIT_ROWS = 17


def without_provisioning(actions: Sequence[str]) -> list[str]:
    """As ações da auditoria sem as do cadastro da empresa (``PgProvisioning``)."""
    return [action for action in actions if action not in PROVISIONING_ACTIONS]


@dataclass(frozen=True)
class PgServer:
    host: str
    port: int
    user: str
    password: str | None
    admin_database: str = "postgres"

    def url(self, database: str, user: str | None = None, password: str | None = None) -> str:
        user = user or self.user
        password = password if user != self.user else self.password
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        return f"postgresql://{auth}@{self.host}:{self.port}/{database}"

    def connect(self, database: str | None = None) -> psycopg.Connection:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=database or self.admin_database,
            autocommit=True,
            connect_timeout=10,
        )


@dataclass(frozen=True)
class PgDatabase:
    server: PgServer
    name: str
    owner: str
    roles: RoleNames
    passwords: dict[str, str]

    @property
    def superuser_url(self) -> str:
        return self.server.url(self.name)

    def url_for(self, role: str) -> str:
        return self.server.url(self.name, role, self.passwords[role])

    @property
    def owner_url(self) -> str:
        return self.url_for(self.owner)

    @property
    def app_url(self) -> str:
        return self.url_for(self.roles.app)

    @property
    def dispatcher_url(self) -> str:
        return self.url_for(self.roles.dispatcher)


@dataclass(frozen=True)
class Tenant:
    company_id: UUID
    area_id: UUID
    other_area_id: UUID
    creator: Principal
    approver: Principal
    operator: Principal
    viewer: Principal
    admin: Principal
    outsider: Principal  # papel só na outra área


async def make_tenant(
    db: Database,
    name: str,
    *,
    guaranteed_slots: int = 2,
    max_borrow_factor: Decimal | float = Decimal("2.0"),
    max_queued: int = 1000,
) -> Tenant:
    provisioning = PgProvisioning(db)
    resolver = PgPrincipalResolver(db)
    company = await provisioning.create_company(
        name,
        guaranteed_slots=guaranteed_slots,
        max_borrow_factor=max_borrow_factor,
        max_queued=max_queued,
    )
    area = await provisioning.create_area(company, "Fiscal")
    other = await provisioning.create_area(company, "Jurídico")
    tag = secrets.token_hex(4)

    async def person(label: str, *grants: tuple[Role, UUID | None]) -> Principal:
        subject = f"{label}-{tag}@{name.lower().replace(' ', '-')}"
        user_id = await provisioning.create_user(company, subject, label)
        for role, area_id in grants:
            await provisioning.add_membership(company, user_id, role, area_id)
        principal = await resolver.resolve(subject, company)
        assert principal is not None
        return principal

    return Tenant(
        company_id=company,
        area_id=area,
        other_area_id=other,
        creator=await person("criadora", (Role.CREATOR, area)),
        approver=await person("aprovador", (Role.APPROVER, area), (Role.OPERATOR, area)),
        operator=await person("operador", (Role.OPERATOR, area)),
        viewer=await person("leitora", (Role.VIEWER, area)),
        admin=await person("admin", (Role.COMPANY_ADMIN, None)),
        outsider=await person("vizinho", (Role.CREATOR, other), (Role.OPERATOR, other)),
    )


async def publish(platform: PgPlatform, tenant: Tenant, manifest: Manifest) -> RobotVersionInfo:
    version = await platform.create_version(tenant.creator, tenant.area_id, manifest)
    await platform.mark_validated(tenant.creator, version.robot_id, version.version_no)
    return await platform.publish(tenant.approver, version.robot_id, version.version_no)


def protocolo() -> Manifest:
    return emitir_protocolo(BASE, HOST)


def notas() -> Manifest:
    return baixar_notas(BASE, HOST)


async def reset_queue(
    superuser: AsyncEngine, *, total_slots: int, reserve_ratio: float = 0.10
) -> None:
    """Cancela o que ficou na fila de outros testes e fixa a capacidade."""
    async with superuser.begin() as conn:
        await conn.execute(
            text(
                "UPDATE runs SET state = 'cancelled', finished_at = now(), lease_expires_at = NULL "
                "WHERE state IN ('queued', 'running', 'awaiting_human')"
            )
        )
        await conn.execute(
            text(
                "UPDATE platform_settings SET total_browser_slots = :total, "
                "interactive_reserve_ratio = :ratio"
            ),
            {"total": total_slots, "ratio": Decimal(str(reserve_ratio))},
        )


def event(
    run_id: UUID,
    attempt: int,
    seq: int,
    kind: RunEventKind,
    step_id: str | None = None,
    **data: Any,
) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        attempt=attempt,
        seq=seq,
        at=datetime.now(UTC),
        kind=kind,
        step_id=step_id,
        data=data,
    )


def result(run_id: UUID, attempt: int, state: str = "succeeded", **extra: Any) -> dict[str, Any]:
    """RESULT.json mínimo da interface A."""
    body: dict[str, Any] = {
        "run_id": str(run_id),
        "attempt": attempt,
        "state": state,
        "outputs": None,
        "error_code": None,
        "error_message": None,
        "failed_step_id": None,
        "irreversible_started": False,
        "steps_done": 0,
        "artifacts": [],
    }
    body.update(extra)
    return body


async def failed_run(
    app_db: Database,
    dispatcher_db: Database,
    tenant: Tenant,
    *,
    snapshot: dict[str, Any] | None,
    error_code: str = "elemento_nao_encontrado",
    step_id: str = "entrar",
    slug: str = "emitir_protocolo",
    state: str = "failed",
) -> UUID:
    """Uma execução que o executor deu por falha no passo ``step_id``, com o retrato dado.

    O despachante pega execuções até chegar a esta (as outras ficam rodando).
    """
    from rapa.control.services import PgRunQueue
    from rapa.core.contracts import RunTrigger

    run = await PgPlatform(app_db).start(tenant.operator, slug, PROTOCOLO_INPUTS, RunTrigger.REST)
    queue = PgRunQueue(dispatcher_db)
    for _ in range(20):
        # Outras execuções na fila (de outra prioridade) ficam rodando: não importam aqui.
        job = await queue.claim_next()
        assert job is not None, "a execução não foi pega pelo despachante"
        if job.run_id == run.run_id:
            break
    else:  # pragma: no cover - auxiliar de teste
        raise AssertionError("a execução não foi pega pelo despachante")
    body = result(
        job.run_id,
        job.attempt,
        state,
        error_code=error_code,
        error_message="Não encontrei o elemento 'botão Entrar' no passo 'Clicar em Entrar'.",
        failed_step_id=step_id,
        failure_snapshot=snapshot,
    )
    assert await queue.finish(job.run_id, job.attempt, body)
    return run.run_id
