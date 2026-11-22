"""Administração da empresa por ela mesma (:class:`PgCompanyAdmin`) e consumo mensal.

Regras (as mesmas pelo REST ``/v1/empresa``, pela tela Empresa e, o consumo, pela linha
de comando):

- **Quem administra a empresa** (``company_admin`` na empresa inteira) lista os usuários
  e os papéis, dá e tira papéis numa área ou na empresa inteira, desativa e reativa
  usuários e cria e renomeia áreas.
- **Quem administra uma área** (``area_admin``) lista os usuários e dá e tira só os
  papéis das áreas que administra; nunca dá ``company_admin`` nem papel na empresa
  inteira, e não desativa ninguém.
- ``platform_admin`` nunca é dado nem tirado por aqui (é da plataforma).
- Ninguém tira o último ``company_admin`` ativo da empresa (nem tirando o papel, nem
  desativando a pessoa) nem desativa a si mesmo. As mudanças de papéis de uma empresa
  passam uma de cada vez (trava da empresa), então duas pessoas tirando ao mesmo tempo
  os dois últimos administradores não deixam a empresa sem nenhum. O banco confere o
  mesmo no fim de cada transação (``rapa_company_admin_guard``, sob a mesma trava), para
  qualquer caminho que mexa em ``memberships`` ou ``users``.
- Empresa congelada: dá para tirar papéis e desativar pessoas (só diminui o acesso),
  mas não dar papéis, reativar nem criar ou renomear áreas.
- **Consumo** (:func:`company_usage`): por área e por origem, num mês do fuso
  ``America/Sao_Paulo``, pela data em que a execução foi pedida. Vê quem administra a
  empresa e quem tem o papel ``auditor`` (na empresa inteira: todas as áreas; numa área:
  só ela). Só leitura.

Tudo com auditoria (``usuario.papeis_alterados``, ``usuario.desativado``,
``usuario.reativado``, ``area.criada``, ``area.renomeada``). Para outra empresa, o
usuário e a área não existem (``NotFound``).
"""

import re
from collections import defaultdict
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import (
    RowMapping,
    and_,
    case,
    delete,
    extract,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    areas,
    batches,
    companies,
    memberships,
    recording_sessions,
    robots,
    runs,
    users,
)
from rapa.control.services import audit
from rapa.control.services._common import (
    PgService,
    ensure_company_active,
    lock_company_status,
    now_utc,
    visible_areas,
)
from rapa.core.contracts import (
    AreaInfo,
    AreaUsage,
    CompanyFrozen,
    CompanyInfo,
    CompanyStatus,
    CompanyUser,
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    Role,
    RoleGrant,
    RunState,
    RunTrigger,
    TriggerUsage,
    UsageReport,
)
from rapa.core.ids import new_id

__all__ = [
    "MAX_AREA_NAME",
    "USAGE_TIMEZONE",
    "PgCompanyAdmin",
    "clean_area_name",
    "company_usage",
    "current_month",
    "month_period",
]

#: Fuso do mês do consumo.
USAGE_TIMEZONE = "America/Sao_Paulo"
MAX_AREA_NAME = 200
_MONTH = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
#: Classe da trava consultiva das mudanças de papéis e de usuários de uma empresa.
_ADMIN_LOCK_CLASS = 7131
#: Papéis que só quem administra a empresa dá (e ``platform_admin``, que ninguém dá aqui).
_COMPANY_ONLY = frozenset({Role.COMPANY_ADMIN, Role.PLATFORM_ADMIN})
_LAST_ADMIN = (
    "a empresa precisa de pelo menos um administrador ativo; dê o papel company_admin a outra "
    "pessoa antes"
)


# ---------------------------------------------------------------------------
# Consumo mensal
# ---------------------------------------------------------------------------


def month_period(month: str) -> tuple[datetime, datetime]:
    """Início e fim (exclusivo), em UTC, do mês ``AAAA-MM`` no fuso ``America/Sao_Paulo``."""
    match = _MONTH.fullmatch(month)
    if match is None or not 2000 <= int(match.group(1)) <= 2999:
        raise InvalidInput("mês inválido", ["use o formato AAAA-MM, por exemplo 2026-11"])
    year, number = int(match.group(1)), int(match.group(2))
    zone = ZoneInfo(USAGE_TIMEZONE)
    start = datetime(year, number, 1, tzinfo=zone)
    end = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def current_month(now: datetime | None = None) -> str:
    """O mês de agora (``AAAA-MM``) no fuso do consumo."""
    local = (now or now_utc()).astimezone(ZoneInfo(USAGE_TIMEZONE))
    return f"{local.year:04d}-{local.month:02d}"


def _minutes(seconds: Any) -> float:
    return round(float(Decimal(str(seconds or 0))) / 60, 2)


def _seconds(started: Any, finished: Any) -> Any:
    """Segundos de ``finished - started``; 0 sem início ou sem fim.

    O que ainda não terminou não conta minutos (só a quantidade): assim o relatório de um
    mês não muda enquanto ninguém mexe nele, e os minutos entram quando a coisa termina.
    """
    return case(
        (or_(started.is_(None), finished.is_(None)), literal(0)),
        else_=func.greatest(0, extract("epoch", finished - started)),
    )


async def company_usage(
    session: AsyncSession,
    company_id: UUID,
    month: str,
    *,
    area_ids: Collection[UUID] | None = None,
) -> UsageReport:
    """Consumo da empresa no mês, por área e por origem (dentro da transação fixada nela).

    - execuções por situação e minutos de navegador (``finished_at - started_at`` de cada
      execução que começou e terminou; a que ainda roda entra na contagem, mas os minutos
      dela só quando terminar), pela data em que foi pedida (``runs_company_created``);
    - sessões de gravação e os minutos delas (``stopped_at - started_at``; a ainda aberta
      entra na contagem, mas os minutos só quando ela for encerrada), pela data de
      abertura (``recording_sessions_company_created``);
    - lotes e linhas, pela data do lote (``batches_company_created``).

    ``area_ids`` limita às áreas que quem pergunta pode ver (``None``: todas).
    """
    start, end = month_period(month)
    names = (
        await session.execute(
            select(areas.c.id, areas.c.name)
            .where(areas.c.company_id == company_id)
            .order_by(areas.c.name, areas.c.id)
        )
    ).all()
    allowed = None if area_ids is None else set(area_ids)
    report_areas: dict[UUID, AreaUsage] = {}
    for area_id, name in names:
        if allowed is None or area_id in allowed:
            report_areas[area_id] = AreaUsage(area_id=area_id, area_name=name)

    def limited(query: Any, column: Any) -> Any:
        return query if allowed is None else query.where(column.in_(list(allowed)))

    run_rows = (
        await session.execute(
            limited(
                select(
                    runs.c.area_id,
                    runs.c.trigger,
                    runs.c.state,
                    func.count(),
                    func.coalesce(func.sum(_seconds(runs.c.started_at, runs.c.finished_at)), 0),
                )
                .where(
                    runs.c.company_id == company_id,
                    runs.c.created_at >= start,
                    runs.c.created_at < end,
                )
                .group_by(runs.c.area_id, runs.c.trigger, runs.c.state),
                runs.c.area_id,
            )
        )
    ).all()
    by_trigger: dict[UUID, dict[RunTrigger, dict[str, Any]]] = defaultdict(dict)
    for area_id, trigger, state, count, seconds in run_rows:
        entry = by_trigger[area_id].setdefault(
            RunTrigger(trigger), {"runs": 0, "states": {}, "seconds": Decimal(0)}
        )
        entry["runs"] += int(count)
        entry["states"][RunState(state)] = int(count)
        entry["seconds"] += Decimal(str(seconds))
    session_rows = (
        await session.execute(
            limited(
                select(
                    recording_sessions.c.area_id,
                    func.count(),
                    func.coalesce(
                        func.sum(
                            _seconds(
                                recording_sessions.c.started_at, recording_sessions.c.stopped_at
                            )
                        ),
                        0,
                    ),
                )
                .where(
                    recording_sessions.c.company_id == company_id,
                    recording_sessions.c.created_at >= start,
                    recording_sessions.c.created_at < end,
                )
                .group_by(recording_sessions.c.area_id),
                recording_sessions.c.area_id,
            )
        )
    ).all()
    batch_rows = (
        await session.execute(
            limited(
                select(
                    robots.c.area_id, func.count(), func.coalesce(func.sum(batches.c.total_rows), 0)
                )
                .select_from(
                    batches.join(
                        robots,
                        and_(
                            robots.c.company_id == batches.c.company_id,
                            robots.c.id == batches.c.robot_id,
                        ),
                    )
                )
                .where(
                    batches.c.company_id == company_id,
                    batches.c.created_at >= start,
                    batches.c.created_at < end,
                )
                .group_by(robots.c.area_id),
                robots.c.area_id,
            )
        )
    ).all()
    sessions_by_area = {row[0]: (int(row[1]), Decimal(str(row[2]))) for row in session_rows}
    batches_by_area = {row[0]: (int(row[1]), int(row[2])) for row in batch_rows}
    out: list[AreaUsage] = []
    browser_total = Decimal(0)
    session_total = Decimal(0)
    for area_id, base in report_areas.items():
        per_trigger = by_trigger.get(area_id, {})
        triggers = [
            TriggerUsage(
                trigger=trigger,
                runs=data["runs"],
                runs_by_state=dict(sorted(data["states"].items())),
                browser_minutes=_minutes(data["seconds"]),
            )
            for trigger, data in sorted(per_trigger.items())
        ]
        session_count, session_seconds = sessions_by_area.get(area_id, (0, Decimal(0)))
        batch_count, batch_lines = batches_by_area.get(area_id, (0, 0))
        browser_seconds = sum((data["seconds"] for data in per_trigger.values()), Decimal(0))
        browser_total += browser_seconds
        session_total += session_seconds
        out.append(
            base.model_copy(
                update={
                    "runs": sum(t.runs for t in triggers),
                    "browser_minutes": _minutes(browser_seconds),
                    "by_trigger": triggers,
                    "sessions": session_count,
                    "session_minutes": _minutes(session_seconds),
                    "batches": batch_count,
                    "batch_rows": batch_lines,
                }
            )
        )
    return UsageReport(
        company_id=company_id,
        month=month,
        timezone=USAGE_TIMEZONE,
        period_start=start,
        period_end=end,
        areas=out,
        runs=sum(a.runs for a in out),
        browser_minutes=_minutes(browser_total),
        sessions=sum(a.sessions for a in out),
        session_minutes=_minutes(session_total),
        batches=sum(a.batches for a in out),
        batch_rows=sum(a.batch_rows for a in out),
    )


# ---------------------------------------------------------------------------
# Usuários, papéis e áreas
# ---------------------------------------------------------------------------


def clean_area_name(name: str) -> str:
    """O nome da área com os brancos normalizados (de 1 a 200 caracteres)."""
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > MAX_AREA_NAME:
        raise InvalidInput(
            "nome de área inválido", [f"o nome precisa ter de 1 a {MAX_AREA_NAME} caracteres"]
        )
    return cleaned


def _is_company_admin(principal: Principal) -> bool:
    return principal.has_role(Role.COMPANY_ADMIN)


def _admin_areas(principal: Principal) -> frozenset[UUID] | None:
    """Áreas cujos papéis o principal administra; ``None``: todas (papel na empresa inteira)."""
    if principal.has_role(Role.AREA_ADMIN):
        return None
    return frozenset(
        area for area, roles in principal.area_roles.items() if Role.AREA_ADMIN in roles
    )


def _administers_any(principal: Principal) -> bool:
    managed = _admin_areas(principal)
    return managed is None or bool(managed)


def _audits_any(principal: Principal) -> bool:
    return principal.has_role(Role.AUDITOR) or any(
        Role.AUDITOR in roles for roles in principal.area_roles.values()
    )


def _grant_text(grant: RoleGrant) -> str:
    return f"{grant.role.value}@{grant.area_id or 'empresa'}"


class PgCompanyAdmin(PgService):
    """Usuários, papéis, áreas e consumo da empresa do principal (:class:`CompanyAdminService`)."""

    # -- leitura -------------------------------------------------------------------

    async def get_company(self, principal: Principal) -> CompanyInfo:
        async with self.db.tenant_session(principal.company_id) as session:
            row = (
                (
                    await session.execute(
                        select(companies).where(companies.c.id == principal.company_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise NotFound("empresa não encontrada")
        privileged = (
            _is_company_admin(principal) or _administers_any(principal) or _audits_any(principal)
        )
        return CompanyInfo(
            company_id=row["id"],
            name=row["name"],
            status=CompanyStatus(row["status"]),
            status_reason=row["status_reason"] if privileged else None,
            status_changed_at=row["status_changed_at"],
            created_at=row["created_at"],
        )

    def _visible_grant(self, principal: Principal, grant: RoleGrant) -> bool:
        """Quem administra a empresa vê todos; quem administra áreas, os das áreas dele
        e os da empresa inteira (que valem nelas)."""
        if _is_company_admin(principal):
            return True
        managed = _admin_areas(principal)
        return grant.area_id is None or managed is None or grant.area_id in managed

    def _manageable(self, principal: Principal, grant: RoleGrant) -> bool:
        """Se o principal pode dar ou tirar este papel."""
        if grant.role is Role.PLATFORM_ADMIN:
            return False
        if _is_company_admin(principal):
            return True
        if grant.area_id is None or grant.role in _COMPANY_ONLY:
            return False
        managed = _admin_areas(principal)
        return managed is None or grant.area_id in managed

    async def _grants(
        self, session: AsyncSession, company_id: UUID
    ) -> dict[UUID, list[RowMapping]]:
        rows = (
            (
                await session.execute(
                    select(
                        memberships.c.id,
                        memberships.c.user_id,
                        memberships.c.area_id,
                        memberships.c.role,
                    )
                    .where(memberships.c.company_id == company_id)
                    .order_by(memberships.c.role, memberships.c.area_id)
                )
            )
            .mappings()
            .all()
        )
        out: dict[UUID, list[RowMapping]] = defaultdict(list)
        for row in rows:
            out[row["user_id"]].append(row)
        return out

    def _user(self, principal: Principal, row: RowMapping, grants: list[RowMapping]) -> CompanyUser:
        visible = [
            grant
            for grant in (RoleGrant(role=Role(g["role"]), area_id=g["area_id"]) for g in grants)
            if self._visible_grant(principal, grant)
        ]
        visible.sort(key=lambda g: (g.area_id is not None, str(g.area_id or ""), g.role.value))
        return CompanyUser(
            user_id=row["id"],
            subject=row["subject"],
            display_name=row["display_name"],
            active=row["active"],
            created_at=row["created_at"],
            roles=visible,
        )

    async def _user_view(
        self, session: AsyncSession, principal: Principal, user_id: UUID
    ) -> CompanyUser:
        row = await self._user_row(session, principal.company_id, user_id)
        grants = (await self._grants(session, principal.company_id)).get(user_id, [])
        return self._user(principal, row, grants)

    @staticmethod
    async def _user_row(session: AsyncSession, company_id: UUID, user_id: UUID) -> RowMapping:
        row: RowMapping | None = (
            (
                await session.execute(
                    select(users).where(users.c.company_id == company_id, users.c.id == user_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("usuário não encontrado")
        return row

    async def list_users(self, principal: Principal) -> list[CompanyUser]:
        if not (_is_company_admin(principal) or _administers_any(principal)):
            raise Forbidden("só quem administra a empresa ou uma área vê os usuários")
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (
                (
                    await session.execute(
                        select(users)
                        .where(users.c.company_id == principal.company_id)
                        .order_by(users.c.subject)
                    )
                )
                .mappings()
                .all()
            )
            grants = await self._grants(session, principal.company_id)
        return [self._user(principal, row, grants.get(row["id"], [])) for row in rows]

    async def list_areas(self, principal: Principal) -> list[AreaInfo]:
        query = (
            select(areas.c.id, areas.c.name, areas.c.created_at)
            .where(areas.c.company_id == principal.company_id)
            .order_by(areas.c.name, areas.c.id)
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(areas.c.id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).all()
        return [AreaInfo(area_id=r[0], name=r[1], created_at=r[2]) for r in rows]

    async def usage(self, principal: Principal, month: str | None = None) -> UsageReport:
        if principal.has_role(Role.AUDITOR):
            allowed: list[UUID] | None = None
        else:
            allowed = sorted(
                area for area in principal.area_roles if principal.has_role(Role.AUDITOR, area)
            )
            if not allowed:
                raise Forbidden("só quem administra a empresa ou audita vê o consumo")
        month = month or current_month()
        month_period(month)  # confere o formato antes de abrir a transação
        async with self.db.tenant_session(principal.company_id) as session:
            return await company_usage(session, principal.company_id, month, area_ids=allowed)

    # -- papéis -----------------------------------------------------------------------

    @staticmethod
    async def _lock_admin(session: AsyncSession, company_id: UUID) -> CompanyStatus:
        """A trava da situação (primeiro, como toda escrita) e a das mudanças de papéis."""
        status = await lock_company_status(session, company_id)
        if status is None:
            raise NotFound("empresa não encontrada")
        await session.execute(
            select(func.pg_advisory_xact_lock(_ADMIN_LOCK_CLASS, func.hashtext(str(company_id))))
        )
        return status

    @staticmethod
    async def _check_admins_left(session: AsyncSession, company_id: UUID) -> None:
        left = await session.scalar(
            select(func.count(func.distinct(users.c.id)))
            .select_from(
                users.join(
                    memberships,
                    and_(
                        memberships.c.company_id == users.c.company_id,
                        memberships.c.user_id == users.c.id,
                    ),
                )
            )
            .where(
                users.c.company_id == company_id,
                users.c.active.is_(True),
                memberships.c.role == Role.COMPANY_ADMIN.value,
                memberships.c.area_id.is_(None),
            )
        )
        if not left:
            raise Conflict(_LAST_ADMIN)

    def _check_request(self, principal: Principal, roles: list[RoleGrant]) -> set[RoleGrant]:
        if not (_is_company_admin(principal) or _administers_any(principal)):
            raise Forbidden("só quem administra a empresa ou uma área dá e tira papéis")
        wanted = set(roles)
        for grant in sorted(wanted, key=_grant_text):
            if grant.role is Role.PLATFORM_ADMIN:
                raise Forbidden("o papel platform_admin nunca é dado por aqui")
            if grant.role is Role.COMPANY_ADMIN and not _is_company_admin(principal):
                raise Forbidden("só quem administra a empresa dá o papel company_admin")
            if grant.role is Role.COMPANY_ADMIN and grant.area_id is not None:
                raise InvalidInput(
                    "papel inválido", ["o papel company_admin vale para a empresa inteira"]
                )
            if not self._manageable(principal, grant):
                if grant.area_id is None:
                    raise Forbidden("só quem administra a empresa dá papéis na empresa inteira")
                raise Forbidden("você só dá e tira os papéis das áreas que administra")
        return wanted

    async def set_roles(
        self, principal: Principal, user_id: UUID, roles: list[RoleGrant]
    ) -> CompanyUser:
        """Troca os papéis do usuário que o principal administra pelos de ``roles``.

        Os outros (os de outras áreas, para quem administra áreas; ``platform_admin``,
        para todos) ficam como estão.
        """
        wanted = self._check_request(principal, roles)
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            status = await self._lock_admin(session, company_id)
            await self._user_row(session, company_id, user_id)
            needed = {g.area_id for g in wanted if g.area_id is not None}
            if needed:
                found = set(
                    (
                        await session.scalars(
                            select(areas.c.id).where(
                                areas.c.company_id == company_id, areas.c.id.in_(list(needed))
                            )
                        )
                    ).all()
                )
                if found != needed:
                    raise NotFound("área não encontrada")
            current = (await self._grants(session, company_id)).get(user_id, [])
            mine = {
                RoleGrant(role=Role(row["role"]), area_id=row["area_id"]): row["id"]
                for row in current
            }
            scope = {
                grant: mid for grant, mid in mine.items() if self._manageable(principal, grant)
            }
            removed = sorted(set(scope) - wanted, key=_grant_text)
            added = sorted(wanted - set(scope), key=_grant_text)
            if added and status is CompanyStatus.FROZEN:
                raise CompanyFrozen()
            if removed:
                await session.execute(
                    delete(memberships).where(
                        memberships.c.company_id == company_id,
                        memberships.c.id.in_([scope[g] for g in removed]),
                    )
                )
            if added:
                await session.execute(
                    insert(memberships),
                    [
                        {
                            "id": new_id(),
                            "company_id": company_id,
                            "user_id": user_id,
                            "area_id": grant.area_id,
                            "role": grant.role.value,
                        }
                        for grant in added
                    ],
                )
            if removed or added:
                await self._check_admins_left(session, company_id)
                await audit.append(
                    session,
                    company_id=company_id,
                    actor=principal.subject,
                    action="usuario.papeis_alterados",
                    target=f"usuario:{user_id}",
                    payload={
                        "adicionados": [_grant_text(g) for g in added],
                        "removidos": [_grant_text(g) for g in removed],
                    },
                )
            return await self._user_view(session, principal, user_id)

    # -- usuários -----------------------------------------------------------------------

    @staticmethod
    def _check_company_admin(principal: Principal, what: str) -> None:
        if not _is_company_admin(principal):
            raise Forbidden(f"só quem administra a empresa {what}")

    async def deactivate_user(self, principal: Principal, user_id: UUID) -> CompanyUser:
        self._check_company_admin(principal, "desativa usuários")
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            await self._lock_admin(session, company_id)
            row = await self._user_row(session, company_id, user_id)
            if row["id"] == principal.user_id or row["subject"] == principal.subject:
                raise Conflict("você não pode desativar a si mesmo")
            if row["active"]:
                await session.execute(
                    update(users)
                    .where(users.c.company_id == company_id, users.c.id == user_id)
                    .values(active=False)
                )
                await self._check_admins_left(session, company_id)
                await audit.append(
                    session,
                    company_id=company_id,
                    actor=principal.subject,
                    action="usuario.desativado",
                    target=f"usuario:{user_id}",
                    payload={"usuario": str(user_id)},
                )
            return await self._user_view(session, principal, user_id)

    async def reactivate_user(self, principal: Principal, user_id: UUID) -> CompanyUser:
        self._check_company_admin(principal, "reativa usuários")
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            status = await self._lock_admin(session, company_id)
            row = await self._user_row(session, company_id, user_id)
            if not row["active"]:
                if status is CompanyStatus.FROZEN:
                    raise CompanyFrozen()
                await session.execute(
                    update(users)
                    .where(users.c.company_id == company_id, users.c.id == user_id)
                    .values(active=True)
                )
                await audit.append(
                    session,
                    company_id=company_id,
                    actor=principal.subject,
                    action="usuario.reativado",
                    target=f"usuario:{user_id}",
                    payload={"usuario": str(user_id)},
                )
            return await self._user_view(session, principal, user_id)

    # -- áreas ----------------------------------------------------------------------------

    async def create_area(self, principal: Principal, name: str) -> AreaInfo:
        self._check_company_admin(principal, "cria áreas")
        cleaned = clean_area_name(name)
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            row = (
                await session.execute(
                    pg_insert(areas)
                    .values(id=new_id(), company_id=company_id, name=cleaned)
                    .on_conflict_do_nothing(index_elements=["company_id", "name"])
                    .returning(areas.c.id, areas.c.name, areas.c.created_at)
                )
            ).one_or_none()
            if row is None:
                raise Conflict("já existe uma área com este nome")
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="area.criada",
                target=f"area:{row[0]}",
                payload={"nome": cleaned},
            )
        return AreaInfo(area_id=row[0], name=row[1], created_at=row[2])

    async def rename_area(self, principal: Principal, area_id: UUID, name: str) -> AreaInfo:
        self._check_company_admin(principal, "renomeia áreas")
        cleaned = clean_area_name(name)
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            current = (
                await session.execute(
                    select(areas.c.id, areas.c.name, areas.c.created_at)
                    .where(areas.c.company_id == company_id, areas.c.id == area_id)
                    .with_for_update()
                )
            ).one_or_none()
            if current is None:
                raise NotFound("área não encontrada")
            if current[1] == cleaned:
                return AreaInfo(area_id=current[0], name=current[1], created_at=current[2])
            try:
                async with session.begin_nested():
                    await session.execute(
                        update(areas)
                        .where(areas.c.company_id == company_id, areas.c.id == area_id)
                        .values(name=cleaned)
                    )
            except IntegrityError:
                raise Conflict("já existe uma área com este nome") from None
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="area.renomeada",
                target=f"area:{area_id}",
                payload={"antes": current[1], "depois": cleaned},
            )
        return AreaInfo(area_id=current[0], name=cleaned, created_at=current[2])
