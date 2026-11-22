"""Conferências do certificado digital A1, comuns a publicar, enfileirar e lançar.

Um robô que declara uma credencial ``pfx_a1`` só usa o certificado quando, para cada
uma delas:

1. há uma credencial registrada na área do robô com o mesmo nome, o tipo ``pfx_a1`` e
   a mesma origem (a forma de :func:`rapa.core.robot.normalize_origin`);
2. a credencial não foi revogada;
3. não venceu (sem a validade registrada, conta como vencida: falha fechada);
4. o custodiante autorizou este robô (``a1_authorizations`` ativa) com a impressão
   atual do certificado: trocar o certificado derruba a autorização.

As mesmas funções servem à aplicação (numa transação fixada na empresa) e ao
despachante (``BYPASSRLS``, que só lê as colunas que estão aqui: nunca ``vault_path``),
por isso toda consulta filtra a empresa explicitamente. As mensagens dizem o que falta
pelo nome da credencial e pela origem, nunca com segredos.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, and_, case, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    a1_authorizations,
    companies,
    credential_alerts,
    credentials,
    robot_versions,
    runs,
    users,
)
from rapa.control.services import audit
from rapa.core.contracts import CertificateBlocked, CompanyStatus, RunnerJob, uses_a1
from rapa.core.cron import DEFAULT_TIMEZONE, load_zone
from rapa.core.robot import CredentialKind, CredentialRef, Manifest

__all__ = [
    "ALERT_ACTOR",
    "ALERT_BATCH",
    "BLOCKED_MESSAGE",
    "DEFAULT_WARN_DAYS",
    "ExpiryAlert",
    "ExpiryBatch",
    "a1_custodians",
    "a1_problems",
    "a1_refs",
    "ensure_a1_ready",
    "is_expired",
    "launch_block",
    "local_date",
    "record_expiry_alerts",
]

logger = logging.getLogger("rapa.control.a1")

#: Prazo padrão do aviso de vencimento, em dias (``RAPA_A1_AVISO_DIAS``).
DEFAULT_WARN_DAYS = 30
#: Quem aparece na auditoria dos avisos de vencimento.
ALERT_ACTOR = "despachante"
#: Começo da mensagem de quando o certificado bloqueia publicar ou enfileirar.
BLOCKED_MESSAGE = "o certificado A1 do robô não pode ser usado agora"
#: Quantos certificados, no máximo, cada lote do aviso de vencimento trata.
ALERT_BATCH = 500
_ZONE = load_zone(DEFAULT_TIMEZONE)

#: As colunas de ``credentials`` que a conferência lê (as mesmas concedidas ao despachante).
_CREDENTIAL_COLUMNS = (
    credentials.c.id,
    credentials.c.company_id,
    credentials.c.area_id,
    credentials.c.name,
    credentials.c.kind,
    credentials.c.origin,
    credentials.c.not_after,
    credentials.c.fingerprint_sha256,
    credentials.c.revoked_at,
)


def a1_refs(manifest: Manifest) -> list[CredentialRef]:
    """As credenciais do manifesto que são certificados A1."""
    return [c for c in manifest.credentials if c.kind is CredentialKind.PFX_A1]


def is_expired(not_after: datetime | None, now: datetime) -> bool:
    """Vencido, ou sem a validade registrada (falha fechada)."""
    return not_after is None or not_after <= now


def local_date(value: datetime) -> str:
    """A data no fuso de Brasília (dd/mm/aaaa), para as mensagens."""
    return value.astimezone(_ZONE).strftime("%d/%m/%Y")


async def _registered(
    session: AsyncSession, company_id: UUID, area_id: UUID, names: Sequence[str]
) -> dict[str, RowMapping]:
    rows = (
        (
            await session.execute(
                select(*_CREDENTIAL_COLUMNS).where(
                    credentials.c.company_id == company_id,
                    credentials.c.area_id == area_id,
                    credentials.c.name.in_(list(names)),
                )
            )
        )
        .mappings()
        .all()
    )
    return {row["name"]: row for row in rows}


async def a1_problems(
    session: AsyncSession,
    *,
    company_id: UUID,
    area_id: UUID,
    robot_id: UUID,
    manifest: Manifest,
    now: datetime,
) -> list[str]:
    """O que falta para o robô usar os certificados A1 do manifesto (vazio: nada falta)."""
    refs = a1_refs(manifest)
    if not refs:
        return []
    registered = await _registered(session, company_id, area_id, [r.name for r in refs])
    ids = [row["id"] for row in registered.values()]
    authorized: dict[UUID, str | None] = {}
    if ids:
        for credential_id, fingerprint in (
            await session.execute(
                select(
                    a1_authorizations.c.credential_id, a1_authorizations.c.fingerprint_sha256
                ).where(
                    a1_authorizations.c.company_id == company_id,
                    a1_authorizations.c.robot_id == robot_id,
                    a1_authorizations.c.credential_id.in_(ids),
                    a1_authorizations.c.revoked_at.is_(None),
                )
            )
        ).all():
            authorized[credential_id] = fingerprint
    problems: list[str] = []
    for ref in refs:
        name = ref.name
        row = registered.get(name)
        if row is None:
            problems.append(f"o certificado '{name}' não está registrado na área do robô")
            continue
        if row["kind"] != CredentialKind.PFX_A1.value:
            problems.append(f"a credencial '{name}' está registrada, mas não como certificado A1")
            continue
        if row["origin"] != ref.origin:
            problems.append(
                f"o certificado '{name}' está registrado para {row['origin']}, "
                f"e o robô o usa em {ref.origin}"
            )
            continue
        if row["revoked_at"] is not None:
            problems.append(f"o certificado '{name}' foi revogado")
            continue
        if row["not_after"] is None:
            problems.append(
                f"o certificado '{name}' não tem a validade registrada (conta como vencido)"
            )
            continue
        if is_expired(row["not_after"], now):
            problems.append(f"o certificado '{name}' venceu em {local_date(row['not_after'])}")
            continue
        if row["id"] not in authorized:
            problems.append(
                f"falta a autorização do custodiante do certificado '{name}' para este robô"
            )
        elif authorized[row["id"]] != row["fingerprint_sha256"]:
            problems.append(
                f"a autorização do certificado '{name}' é de outro certificado (a impressão "
                "mudou): o custodiante precisa autorizar o robô de novo"
            )
    return problems


async def a1_custodians(
    session: AsyncSession, *, company_id: UUID, area_id: UUID, names: Sequence[str]
) -> set[str]:
    """Sujeitos dos custodiantes dos certificados A1 registrados com estes nomes na área."""
    if not names:
        return set()
    rows = await session.scalars(
        select(users.c.subject)
        .select_from(
            credentials.join(
                users,
                and_(
                    users.c.company_id == credentials.c.company_id,
                    users.c.id == credentials.c.custodian_user_id,
                ),
            )
        )
        .where(
            credentials.c.company_id == company_id,
            credentials.c.area_id == area_id,
            credentials.c.name.in_(list(names)),
            credentials.c.kind == CredentialKind.PFX_A1.value,
        )
    )
    return set(rows.all())


async def _manifest_of(session: AsyncSession, robot: Mapping[str, Any]) -> Manifest:
    raw = robot.get("manifest")
    if raw is None:
        raw = await session.scalar(
            select(robot_versions.c.manifest).where(robot_versions.c.id == robot["version_id"])
        )
    return raw if isinstance(raw, Manifest) else Manifest.model_validate(raw)


async def ensure_a1_ready(
    session: AsyncSession, *, company_id: UUID, robot: Mapping[str, Any], now: datetime
) -> None:
    """Levanta :class:`CertificateBlocked` se a versão que vai rodar não pode usar o A1.

    ``robot`` é a linha que :func:`rapa.control.services.runs.enqueue_run` recebe
    (``robot_id``, ``area_id``, ``version_id`` e, de preferência, ``manifest``).
    """
    manifest = await _manifest_of(session, robot)
    if not uses_a1(manifest):
        return
    problems = await a1_problems(
        session,
        company_id=company_id,
        area_id=robot["area_id"],
        robot_id=robot["robot_id"],
        manifest=manifest,
        now=now,
    )
    if problems:
        raise CertificateBlocked(f"{BLOCKED_MESSAGE}; nada foi enfileirado", problems)


async def launch_block(session: AsyncSession, job: RunnerJob, now: datetime) -> str | None:
    """Por que o despachante não pode lançar o executor desta execução (``None``: pode).

    Roda na sessão do despachante, logo antes de lançar: o certificado pode ter sido
    revogado, ter vencido ou ter perdido a autorização depois de a execução entrar na
    fila. Uma execução que sumiu também não é lançada (falha fechada).
    """
    if not uses_a1(job.manifest):
        return None
    run = (
        await session.execute(
            select(runs.c.robot_id, runs.c.area_id).where(
                runs.c.id == job.run_id, runs.c.company_id == job.company_id
            )
        )
    ).one_or_none()
    if run is None:
        return "A execução não foi encontrada; o executor não foi lançado."
    problems = await a1_problems(
        session,
        company_id=job.company_id,
        area_id=run[1],
        robot_id=run[0],
        manifest=job.manifest,
        now=now,
    )
    if not problems:
        return None
    return (
        "O certificado A1 do robô não pode ser usado agora; nada foi aberto no navegador: "
        + "; ".join(problems)
        + "."
    )


@dataclass(frozen=True, slots=True)
class ExpiryAlert:
    """Um aviso de vencimento registrado agora (o primeiro do dia para a credencial)."""

    company_id: UUID
    credential_id: UUID
    name: str
    kind: str
    not_after: datetime | None


@dataclass(frozen=True, slots=True)
class ExpiryBatch:
    """Um lote do aviso de vencimento: quantos certificados sem o aviso do dia ele pegou
    (``seen``) e os avisos que registrou (os outros, um despachante ao lado registrou)."""

    seen: int
    alerts: list[ExpiryAlert]


async def record_expiry_alerts(
    session: AsyncSession, *, now: datetime, within_days: int, limit: int = ALERT_BATCH
) -> ExpiryBatch:
    """Registra os avisos dos certificados A1 que vencem em até ``within_days`` dias ou já
    venceram: no máximo um por credencial, dia (no fuso de Brasília) e tipo.

    Um lote de até ``limit`` certificados, só entre os que ainda não têm o aviso do dia
    daquele tipo: os vencidos que ninguém revoga não tomam o lugar dos outros, e quem
    chama repete enquanto o lote vier cheio
    (:class:`rapa.control.services.credentials.CredentialAlerts`).

    Roda na sessão do despachante (todas as empresas). Cada aviso novo entra na
    auditoria da empresa (``credencial.a1_vencendo`` ou ``credencial.a1_vencida``) e no
    log, só com o nome da credencial e a data. Empresa congelada não recebe aviso (nada
    novo acontece nela, nem a renovação que o aviso pede); ao descongelar, o aviso do
    dia volta na volta seguinte.
    """
    horizon = now + timedelta(days=within_days)
    day = now.astimezone(_ZONE).date()
    # O mesmo critério de is_expired, no banco.
    expected_kind = case(
        (
            or_(credentials.c.not_after.is_(None), credentials.c.not_after <= now),
            literal("vencida"),
        ),
        else_=literal("vencendo"),
    )
    alerted = (
        select(credential_alerts.c.credential_id)
        .where(
            credential_alerts.c.company_id == credentials.c.company_id,
            credential_alerts.c.credential_id == credentials.c.id,
            credential_alerts.c.day == day,
            credential_alerts.c.kind == expected_kind,
        )
        .exists()
    )
    rows = (
        (
            await session.execute(
                select(
                    credentials.c.id,
                    credentials.c.company_id,
                    credentials.c.name,
                    credentials.c.not_after,
                )
                .where(
                    credentials.c.kind == CredentialKind.PFX_A1.value,
                    credentials.c.revoked_at.is_(None),
                    or_(credentials.c.not_after.is_(None), credentials.c.not_after <= horizon),
                    ~alerted,
                    credentials.c.company_id.not_in(
                        select(companies.c.id).where(
                            companies.c.status == CompanyStatus.FROZEN.value
                        )
                    ),
                )
                .order_by(credentials.c.not_after.asc().nulls_first(), credentials.c.id)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    alerts: list[ExpiryAlert] = []
    for row in rows:
        kind = "vencida" if is_expired(row["not_after"], now) else "vencendo"
        inserted = await session.scalar(
            pg_insert(credential_alerts)
            .values(
                company_id=row["company_id"],
                credential_id=row["id"],
                day=day,
                kind=kind,
                not_after=row["not_after"],
            )
            .on_conflict_do_nothing()
            .returning(credential_alerts.c.credential_id)
        )
        if inserted is None:
            continue
        when = local_date(row["not_after"]) if row["not_after"] is not None else "sem validade"
        await audit.append(
            session,
            company_id=row["company_id"],
            actor=ALERT_ACTOR,
            action=f"credencial.a1_{kind}",
            target=f"credencial:{row['id']}",
            payload={
                "nome": row["name"],
                "vencimento": row["not_after"].isoformat() if row["not_after"] else None,
                "dia": day.isoformat(),
            },
        )
        logger.warning(
            "certificado A1 '%s' da empresa %s %s (%s)",
            row["name"],
            row["company_id"],
            "venceu" if kind == "vencida" else "vence em breve",
            when,
        )
        alerts.append(
            ExpiryAlert(
                company_id=row["company_id"],
                credential_id=row["id"],
                name=row["name"],
                kind=kind,
                not_after=row["not_after"],
            )
        )
    return ExpiryBatch(seen=len(rows), alerts=alerts)
