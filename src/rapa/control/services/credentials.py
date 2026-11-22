"""Credenciais registradas e as regras do certificado A1 (:class:`PgCredentials`).

Regras:

- **Só a referência.** O valor de uma credencial nunca passa pelo RaPA: registra-se o
  caminho no cofre (``vault_path``) e os metadados (nome, tipo, origem, custodiante e,
  no certificado A1, a validade e a impressão SHA-256). O caminho no cofre só aparece
  para quem administra a área e para o custodiante.
- **Quem registra.** Só quem administra a área (``area_admin``, ou ``company_admin``)
  registra. O custodiante é obrigatório no certificado A1: um usuário ativo da empresa
  com o papel ``credential_custodian`` na área (ou na empresa inteira), que não é quem
  registra. A origem passa pela mesma normalização do manifesto
  (:func:`rapa.core.robot.normalize_origin`), e um certificado A1 nasce com a validade
  no futuro.
- **Custodiante.** Só ele renova a validade (o certificado novo no cofre) e autoriza
  um robô a usar o certificado (``a1_authorizations``, com a impressão do momento):
  o robô precisa declarar o certificado na versão mais nova, com a mesma origem, e o
  custodiante nunca autoriza um robô cuja versão mais nova (ou a publicada) ele criou;
  como a autorização é do robô, uma versão que o custodiante criar depois também não
  é aprovada (:meth:`rapa.control.services.robots.PgRobotAdmin.publish`). Renovar com
  outra impressão revoga as autorizações que dependiam da anterior. Sem a impressão,
  a renovação só vale para um certificado registrado sem ela (nada muda nas
  autorizações); se havia uma impressão, a nova é obrigatória, porque um certificado
  renovado é outro certificado. Revogar a credencial (custodiante ou administrador
  da área) revoga junto as autorizações dela.
- **Aprovações.** :meth:`PgCredentials.approvals` mostra quem aprovou uma versão,
  quantas faltam e o que falta no certificado; quem aprova é
  :meth:`rapa.control.services.robots.PgRobotAdmin.publish`.
- **Vencimento.** :meth:`PgCredentials.expiring` lista os certificados que vencem
  dentro do prazo (ou já venceram) nas áreas que a pessoa vê; :class:`CredentialAlerts`
  é a etapa do despachante que registra os avisos (``credential_alerts``), no máximo um
  por credencial e dia.

Outra empresa ou outra área: ``NotFound``. Toda mudança entra na auditoria, sem o
caminho no cofre. Empresa congelada
(:func:`rapa.control.services._common.ensure_company_active`): registrar, renovar e
autorizar são recusados (``CompanyFrozen``); revogar a credencial ou uma autorização
continua valendo (só tira acesso); ler também.
"""

import builtins
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import RowMapping, Select, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from rapa.control.db.schema import (
    a1_authorizations,
    areas,
    credentials,
    memberships,
    robot_versions,
    robots,
    users,
)
from rapa.control.db.session import Database
from rapa.control.services import audit
from rapa.control.services._common import (
    PgService,
    ensure_company_active,
    lock_company_status,
    now_utc,
    visible_areas,
)
from rapa.control.services.a1 import (
    ALERT_BATCH,
    DEFAULT_WARN_DAYS,
    a1_problems,
    is_expired,
    record_expiry_alerts,
)
from rapa.control.services.robots import approvers_of
from rapa.core.contracts import (
    A1AuthorizationInfo,
    ApprovalInfo,
    Conflict,
    CredentialInfo,
    CredentialStatus,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    Role,
    VersionApprovals,
    VersionStatus,
    required_approvals,
    uses_a1,
)
from rapa.core.ids import new_id
from rapa.core.robot import NAME_PATTERN, CredentialKind, Manifest, normalize_origin

__all__ = [
    "MAX_ALERT_BATCHES",
    "MAX_WARN_DAYS",
    "CredentialAlerts",
    "PgCredentials",
    "normalize_fingerprint",
]

#: Maior prazo aceito na consulta de vencimentos, em dias.
MAX_WARN_DAYS = 3650
#: Quantos lotes, no máximo, uma volta do aviso de vencimento trata.
MAX_ALERT_BATCHES = 200
_VAULT_PATH = re.compile(r"^[A-Za-z0-9_.:/-]{1,500}$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(NAME_PATTERN)
_PFX = CredentialKind.PFX_A1.value


def normalize_fingerprint(value: str | None) -> str | None:
    """A impressão SHA-256 em 64 hexadecimais minúsculos (aceita ``AA:BB:...``).

    Levanta ``ValueError`` com a mensagem em pt-BR para o que não é uma impressão.
    """
    if value is None:
        return None
    cleaned = value.strip().lower().replace(":", "").replace(" ", "")
    if not cleaned:
        return None
    if not _FINGERPRINT.fullmatch(cleaned):
        raise ValueError(
            "impressão: use a impressão SHA-256 do certificado (64 caracteres hexadecimais)"
        )
    return cleaned


def _credential_query() -> Select[Any]:
    return select(
        credentials,
        users.c.subject.label("custodian_subject"),
        users.c.display_name.label("custodian_name"),
    ).select_from(
        credentials.outerjoin(
            users,
            (users.c.company_id == credentials.c.company_id)
            & (users.c.id == credentials.c.custodian_user_id),
        )
    )


def _check_not_after(value: datetime | None, now: datetime, errors: list[str]) -> None:
    if value is None:
        return
    if value.tzinfo is None:
        errors.append("validade: informe a data com o fuso horário")
    elif value <= now:
        errors.append("validade: a data já passou")


class PgCredentials(PgService):
    """Credenciais registradas e as regras do certificado A1 (:class:`CredentialService`)."""

    def __init__(
        self,
        db: Database,
        *,
        warn_days: int = DEFAULT_WARN_DAYS,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        super().__init__(db)
        if not 1 <= warn_days <= MAX_WARN_DAYS:
            raise ValueError(f"o prazo do aviso precisa ficar entre 1 e {MAX_WARN_DAYS} dias")
        self.warn_days = warn_days
        self.clock = clock

    # -- leitura -------------------------------------------------------------------

    def _status(self, row: RowMapping, now: datetime) -> CredentialStatus:
        if row["revoked_at"] is not None:
            return CredentialStatus.REVOKED
        if row["kind"] != _PFX:
            return CredentialStatus.ACTIVE
        not_after = row["not_after"]
        if is_expired(not_after, now):
            return CredentialStatus.EXPIRED
        if (not_after - now).total_seconds() <= self.warn_days * 86_400:
            return CredentialStatus.EXPIRING
        return CredentialStatus.ACTIVE

    @staticmethod
    def _is_custodian(principal: Principal, row: RowMapping) -> bool:
        """Se o principal é o custodiante da credencial e ainda tem o papel na área."""
        return (
            row["custodian_subject"] is not None
            and row["custodian_subject"] == principal.subject
            and Role.CREDENTIAL_CUSTODIAN in principal.roles_in(row["area_id"])
        )

    def _info(self, principal: Principal, row: RowMapping, now: datetime) -> CredentialInfo:
        sees_path = principal.has_role(Role.AREA_ADMIN, row["area_id"]) or self._is_custodian(
            principal, row
        )
        return CredentialInfo(
            credential_id=row["id"],
            area_id=row["area_id"],
            name=row["name"],
            kind=CredentialKind(row["kind"]),
            origin=row["origin"],
            custodian_subject=row["custodian_subject"],
            custodian_name=row["custodian_name"] or None,
            not_after=row["not_after"],
            fingerprint_sha256=row["fingerprint_sha256"],
            status=self._status(row, now),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revoked_at=row["revoked_at"],
            revoked_by=row["revoked_by"],
            vault_path=row["vault_path"] if sees_path else None,
        )

    @staticmethod
    async def _load(
        session: AsyncSession, principal: Principal, credential_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        query = _credential_query().where(
            credentials.c.id == credential_id, credentials.c.company_id == principal.company_id
        )
        if lock:
            query = query.with_for_update(of=credentials)
        row: RowMapping | None = (await session.execute(query)).mappings().one_or_none()
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("credencial não encontrada")
        return row

    async def list(
        self, principal: Principal, area_id: UUID | None = None
    ) -> builtins.list[CredentialInfo]:
        """As credenciais das áreas que a pessoa vê (ou só de ``area_id``)."""
        query = _credential_query().where(credentials.c.company_id == principal.company_id)
        if area_id is not None:
            if not principal.roles_in(area_id):
                raise NotFound("área não encontrada")
            query = query.where(credentials.c.area_id == area_id)
        else:
            allowed = visible_areas(principal)
            if allowed is not None:
                query = query.where(credentials.c.area_id.in_(allowed))
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (
                (await session.execute(query.order_by(credentials.c.name, credentials.c.id)))
                .mappings()
                .all()
            )
        now = self.clock()
        return [self._info(principal, row, now) for row in rows]

    async def expiring(
        self, principal: Principal, within_days: int | None = None
    ) -> builtins.list[CredentialInfo]:
        """Os certificados A1 não revogados que vencem dentro do prazo, ou já venceram."""
        days = self.warn_days if within_days is None else within_days
        if not 0 <= days <= MAX_WARN_DAYS:
            raise InvalidInput("prazo inválido", [f"dias: use um número de 0 a {MAX_WARN_DAYS}"])
        now = self.clock()
        horizon = now + timedelta(days=days)
        query = _credential_query().where(
            credentials.c.company_id == principal.company_id,
            credentials.c.kind == _PFX,
            credentials.c.revoked_at.is_(None),
            or_(credentials.c.not_after.is_(None), credentials.c.not_after <= horizon),
        )
        allowed = visible_areas(principal)
        if allowed is not None:
            query = query.where(credentials.c.area_id.in_(allowed))
        query = query.order_by(credentials.c.not_after.asc().nulls_first(), credentials.c.name)
        async with self.db.tenant_session(principal.company_id) as session:
            rows = (await session.execute(query)).mappings().all()
        return [self._info(principal, row, now) for row in rows]

    # -- registro, renovação e revogação ----------------------------------------------

    @staticmethod
    async def _custodian_id(
        session: AsyncSession, company_id: UUID, area_id: UUID, subject: str
    ) -> UUID:
        user = (
            await session.execute(
                select(users.c.id, users.c.active).where(
                    users.c.company_id == company_id, users.c.subject == subject
                )
            )
        ).one_or_none()
        if user is None or not user[1]:
            raise InvalidInput(
                "custodiante inválido", ["custodiante: não é um usuário ativo da empresa"]
            )
        has_role = await session.scalar(
            select(func.count())
            .select_from(memberships)
            .where(
                memberships.c.company_id == company_id,
                memberships.c.user_id == user[0],
                memberships.c.role == Role.CREDENTIAL_CUSTODIAN.value,
                or_(memberships.c.area_id == area_id, memberships.c.area_id.is_(None)),
            )
        )
        if not has_role:
            raise InvalidInput(
                "custodiante inválido",
                [
                    "custodiante: precisa do papel de custodiante de credenciais nesta área "
                    "(ou na empresa inteira)"
                ],
            )
        user_id: UUID = user[0]
        return user_id

    async def register(
        self,
        principal: Principal,
        area_id: UUID,
        *,
        name: str,
        kind: CredentialKind,
        origin: str,
        vault_path: str,
        custodian_subject: str | None = None,
        not_after: datetime | None = None,
        fingerprint: str | None = None,
    ) -> CredentialInfo:
        """Registra a referência a uma credencial do cofre (nunca o valor)."""
        company_id = principal.company_id
        if not principal.roles_in(area_id):
            raise NotFound("área não encontrada")
        if not principal.has_role(Role.AREA_ADMIN, area_id):
            raise Forbidden("só quem administra a área registra credenciais")
        try:
            kind = CredentialKind(kind)
        except ValueError:
            raise InvalidInput(
                "credencial inválida", ["tipo: use password, totp ou pfx_a1"]
            ) from None
        now = self.clock()
        errors: list[str] = []
        if not _NAME.fullmatch(name):
            errors.append(
                "nome: use letras minúsculas, números e _ (até 64), começando por letra ou _"
            )
        try:
            origin = normalize_origin(origin)
        except ValueError as exc:
            errors.append(f"origem: {exc}")
        if not _VAULT_PATH.fullmatch(vault_path) or ".." in vault_path.split("/"):
            errors.append("caminho no cofre: use letras, números e _ . : / - (até 500), sem '..'")
        try:
            fingerprint = normalize_fingerprint(fingerprint)
        except ValueError as exc:
            errors.append(str(exc))
        custodian = (custodian_subject or "").strip() or None
        if kind is CredentialKind.PFX_A1:
            if custodian is None:
                errors.append("custodiante: obrigatório para o certificado A1")
            if not_after is None:
                errors.append("validade: obrigatória para o certificado A1")
        _check_not_after(not_after, now, errors)
        if errors:
            raise InvalidInput("credencial inválida", errors)
        if custodian is not None and custodian == principal.subject:
            raise Forbidden(
                "quem registra a credencial não pode ser o custodiante dela (separação de funções)"
            )
        credential_id = new_id()
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            area = await session.scalar(
                select(areas.c.id).where(areas.c.id == area_id, areas.c.company_id == company_id)
            )
            if area is None:
                raise NotFound("área não encontrada")
            custodian_id = (
                await self._custodian_id(session, company_id, area_id, custodian)
                if custodian is not None
                else None
            )
            taken = await session.scalar(
                select(credentials.c.id).where(
                    credentials.c.company_id == company_id,
                    credentials.c.area_id == area_id,
                    credentials.c.name == name,
                )
            )
            if taken is not None:
                raise Conflict("já existe uma credencial com este nome nesta área")
            try:
                async with session.begin_nested():
                    await session.execute(
                        insert(credentials).values(
                            id=credential_id,
                            company_id=company_id,
                            area_id=area_id,
                            name=name,
                            kind=kind.value,
                            origin=origin,
                            vault_path=vault_path,
                            custodian_user_id=custodian_id,
                            not_after=not_after,
                            fingerprint_sha256=fingerprint,
                        )
                    )
            except IntegrityError:
                raise Conflict("já existe uma credencial com este nome nesta área") from None
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="credencial.registrada",
                target=f"credencial:{credential_id}",
                payload={
                    "nome": name,
                    "tipo": kind.value,
                    "origem": origin,
                    "area": str(area_id),
                    "custodiante": custodian,
                    "vencimento": not_after.isoformat() if not_after else None,
                    "impressao": fingerprint,
                },
            )
            row = await self._load(session, principal, credential_id)
        return self._info(principal, row, now)

    @staticmethod
    async def _revoke_authorizations(
        session: AsyncSession,
        principal: Principal,
        credential_id: UUID,
        reason: str,
        *,
        only_stale: bool = False,
        current_fingerprint: str | None = None,
    ) -> builtins.list[UUID]:
        """Revoga as autorizações ativas da credencial; devolve as que revogou.

        Com ``only_stale``, só as de outra impressão que não ``current_fingerprint``.
        """
        conditions = [
            a1_authorizations.c.company_id == principal.company_id,
            a1_authorizations.c.credential_id == credential_id,
            a1_authorizations.c.revoked_at.is_(None),
        ]
        if only_stale:
            conditions.append(
                a1_authorizations.c.fingerprint_sha256.is_distinct_from(current_fingerprint)
            )
        revoked = list(
            (
                await session.scalars(
                    update(a1_authorizations)
                    .where(*conditions)
                    .values(revoked_at=func.now(), revoked_by=principal.subject)
                    .returning(a1_authorizations.c.id)
                )
            ).all()
        )
        await audit.append_many(
            session,
            company_id=principal.company_id,
            actor=principal.subject,
            action="credencial.a1_autorizacao_revogada",
            entries=[
                {"target": f"autorizacao_a1:{auth_id}", "payload": {"motivo": reason}}
                for auth_id in revoked
            ],
        )
        return revoked

    async def renew(
        self,
        principal: Principal,
        credential_id: UUID,
        *,
        not_after: datetime,
        fingerprint: str | None = None,
    ) -> CredentialInfo:
        """O custodiante registra a validade (e a impressão) do certificado renovado.

        Uma impressão diferente da registrada revoga as autorizações que dependiam da
        anterior. Sem a impressão, só renova um certificado registrado sem ela (as
        autorizações continuam: a impressão não mudou); se havia uma, a nova é
        obrigatória, em vez de apagar a anterior sem saber se o certificado mudou.
        """
        now = self.clock()
        errors: list[str] = []
        try:
            fingerprint = normalize_fingerprint(fingerprint)
        except ValueError as exc:
            errors.append(str(exc))
        _check_not_after(not_after, now, errors)
        async with self.db.tenant_session(principal.company_id) as session:
            await ensure_company_active(session, principal.company_id)
            row = await self._load(session, principal, credential_id, lock=True)
            if not self._is_custodian(principal, row):
                raise Forbidden("só o custodiante do certificado renova a validade")
            if row["kind"] != _PFX:
                raise InvalidInput("só o certificado A1 tem validade para renovar")
            if row["revoked_at"] is not None:
                raise Conflict("a credencial foi revogada; registre uma nova")
            if fingerprint is None and row["fingerprint_sha256"] is not None:
                errors.append(
                    "impressão: informe a impressão SHA-256 do certificado renovado (a "
                    "credencial tem uma registrada)"
                )
            if errors:
                raise InvalidInput("renovação inválida", errors)
            changed = fingerprint != row["fingerprint_sha256"]
            await session.execute(
                update(credentials)
                .where(credentials.c.id == credential_id)
                .values(not_after=not_after, fingerprint_sha256=fingerprint, updated_at=func.now())
            )
            revoked = (
                await self._revoke_authorizations(
                    session, principal, credential_id, "certificado_trocado"
                )
                if changed
                else []
            )
            await audit.append(
                session,
                company_id=principal.company_id,
                actor=principal.subject,
                action="credencial.renovada",
                target=f"credencial:{credential_id}",
                payload={
                    "vencimento": not_after.isoformat(),
                    "impressao": fingerprint,
                    "impressao_trocada": changed,
                    "autorizacoes_revogadas": len(revoked),
                },
            )
            row = await self._load(session, principal, credential_id)
        return self._info(principal, row, now)

    async def revoke(self, principal: Principal, credential_id: UUID) -> CredentialInfo:
        """Revoga a credencial (custodiante ou administrador da área) e as autorizações dela.

        Vale também com a empresa congelada: revogar só tira acesso.
        """
        now = self.clock()
        async with self.db.tenant_session(principal.company_id) as session:
            # Só a trava da situação (a ordem de sempre das travas), sem recusar a congelada.
            await lock_company_status(session, principal.company_id)
            row = await self._load(session, principal, credential_id, lock=True)
            if not (
                self._is_custodian(principal, row)
                or principal.has_role(Role.AREA_ADMIN, row["area_id"])
            ):
                raise Forbidden("só o custodiante ou quem administra a área revoga a credencial")
            if row["revoked_at"] is not None:
                raise Conflict("a credencial já foi revogada")
            await session.execute(
                update(credentials)
                .where(credentials.c.id == credential_id)
                .values(revoked_at=func.now(), revoked_by=principal.subject, updated_at=func.now())
            )
            revoked = await self._revoke_authorizations(
                session, principal, credential_id, "credencial_revogada"
            )
            await audit.append(
                session,
                company_id=principal.company_id,
                actor=principal.subject,
                action="credencial.revogada",
                target=f"credencial:{credential_id}",
                payload={"nome": row["name"], "autorizacoes_revogadas": len(revoked)},
            )
            row = await self._load(session, principal, credential_id)
        return self._info(principal, row, now)

    # -- autorizações do custodiante ---------------------------------------------------

    @staticmethod
    async def _robot(
        session: AsyncSession, principal: Principal, robot_id: UUID, *, lock: bool = False
    ) -> RowMapping:
        query = select(robots.c.id, robots.c.area_id).where(
            robots.c.id == robot_id, robots.c.company_id == principal.company_id
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().one_or_none()
        if row is None or not principal.roles_in(row["area_id"]):
            raise NotFound("robô não encontrado")
        return row

    def _authorization(
        self, row: RowMapping, credential: RowMapping | None, now: datetime
    ) -> A1AuthorizationInfo:
        current = (
            row["revoked_at"] is None
            and credential is not None
            and credential["revoked_at"] is None
            and not is_expired(credential["not_after"], now)
            and row["fingerprint_sha256"] == credential["fingerprint_sha256"]
        )
        return A1AuthorizationInfo(
            authorization_id=row["id"],
            robot_id=row["robot_id"],
            area_id=row["area_id"],
            credential_id=row["credential_id"],
            credential_name=credential["name"] if credential is not None else "",
            origin=row["origin"],
            fingerprint_sha256=row["fingerprint_sha256"],
            authorized_by=row["authorized_by"],
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
            revoked_by=row["revoked_by"],
            current=current,
        )

    async def authorize(
        self, principal: Principal, robot_id: UUID, credential_name: str
    ) -> A1AuthorizationInfo:
        """O custodiante autoriza o robô a usar o certificado na origem registrada."""
        company_id = principal.company_id
        now = self.clock()
        async with self.db.tenant_session(company_id) as session:
            await ensure_company_active(session, company_id)
            robot = await self._robot(session, principal, robot_id, lock=True)
            area_id: UUID = robot["area_id"]
            credential = (
                (
                    await session.execute(
                        _credential_query()
                        .where(
                            credentials.c.company_id == company_id,
                            credentials.c.area_id == area_id,
                            credentials.c.name == credential_name,
                        )
                        .with_for_update(of=credentials)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if credential is None:
                raise NotFound("credencial não encontrada na área do robô")
            if credential["kind"] != _PFX:
                raise InvalidInput(
                    "só o certificado A1 precisa da autorização do custodiante por robô"
                )
            if not self._is_custodian(principal, credential):
                raise Forbidden("só o custodiante deste certificado autoriza robôs a usá-lo")
            if credential["revoked_at"] is not None:
                raise Conflict("o certificado foi revogado")
            if is_expired(credential["not_after"], now):
                raise Conflict(
                    "o certificado venceu (ou não tem a validade registrada): renove antes"
                )
            latest = (
                (
                    await session.execute(
                        select(
                            robot_versions.c.version_no,
                            robot_versions.c.manifest,
                            robot_versions.c.created_by,
                        )
                        .where(robot_versions.c.robot_id == robot_id)
                        .order_by(robot_versions.c.version_no.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if latest is None:
                raise Conflict("o robô ainda não tem versões")
            manifest = Manifest.model_validate(latest["manifest"])
            ref = next((c for c in manifest.credentials if c.name == credential_name), None)
            if ref is None or ref.kind is not CredentialKind.PFX_A1:
                raise InvalidInput(
                    "a versão mais nova do robô não declara este certificado A1",
                    [f"versão {latest['version_no']}: sem o certificado '{credential_name}'"],
                )
            if ref.origin != credential["origin"]:
                raise InvalidInput(
                    "a versão mais nova do robô usa o certificado em outra origem",
                    [
                        f"o robô usa {ref.origin}; o certificado está registrado para "
                        f"{credential['origin']}"
                    ],
                )
            # A versão que roda é a publicada, que pode não ser a mais nova.
            published_by = await session.scalar(
                select(robot_versions.c.created_by).where(
                    robot_versions.c.robot_id == robot_id,
                    robot_versions.c.status == VersionStatus.PUBLISHED.value,
                )
            )
            if principal.subject in (latest["created_by"], published_by):
                raise Forbidden(
                    "o custodiante não autoriza um robô cuja versão mais nova (ou a publicada) "
                    "ele mesmo criou"
                )
            active = (
                (
                    await session.execute(
                        select(a1_authorizations).where(
                            a1_authorizations.c.company_id == company_id,
                            a1_authorizations.c.robot_id == robot_id,
                            a1_authorizations.c.credential_id == credential["id"],
                            a1_authorizations.c.revoked_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if active is not None:
                if active["fingerprint_sha256"] == credential["fingerprint_sha256"]:
                    raise Conflict("este robô já tem a autorização deste certificado")
                # Uma autorização de outra impressão não vale mais: dá lugar à nova.
                await self._revoke_authorizations(
                    session,
                    principal,
                    credential["id"],
                    "certificado_trocado",
                    only_stale=True,
                    current_fingerprint=credential["fingerprint_sha256"],
                )
            authorization_id = new_id()
            try:
                async with session.begin_nested():
                    await session.execute(
                        insert(a1_authorizations).values(
                            company_id=company_id,
                            id=authorization_id,
                            area_id=area_id,
                            robot_id=robot_id,
                            credential_id=credential["id"],
                            origin=credential["origin"],
                            fingerprint_sha256=credential["fingerprint_sha256"],
                            authorized_by=principal.subject,
                        )
                    )
            except IntegrityError:
                raise Conflict("este robô já tem a autorização deste certificado") from None
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="credencial.a1_autorizada",
                target=f"robo:{robot_id}/credencial:{credential['id']}",
                payload={
                    "autorizacao": str(authorization_id),
                    "nome": credential_name,
                    "origem": credential["origin"],
                    "impressao": credential["fingerprint_sha256"],
                    "versao": latest["version_no"],
                },
            )
            row = (
                (
                    await session.execute(
                        select(a1_authorizations).where(
                            a1_authorizations.c.company_id == company_id,
                            a1_authorizations.c.id == authorization_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
        return self._authorization(row, credential, now)

    async def revoke_authorization(
        self, principal: Principal, authorization_id: UUID, *, robot_id: UUID | None = None
    ) -> A1AuthorizationInfo:
        """Revoga a autorização (o custodiante do certificado ou quem administra a área).

        Com ``robot_id``, a autorização precisa ser desse robô (senão, ``NotFound``). Vale
        também com a empresa congelada: revogar só tira acesso.
        """
        company_id = principal.company_id
        now = self.clock()
        async with self.db.tenant_session(company_id) as session:
            await lock_company_status(session, company_id)
            row = (
                (
                    await session.execute(
                        select(a1_authorizations)
                        .where(
                            a1_authorizations.c.company_id == company_id,
                            a1_authorizations.c.id == authorization_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or not principal.roles_in(row["area_id"])
                or (robot_id is not None and row["robot_id"] != robot_id)
            ):
                raise NotFound("autorização não encontrada")
            credential = await self._load(session, principal, row["credential_id"])
            if not (
                self._is_custodian(principal, credential)
                or principal.has_role(Role.AREA_ADMIN, row["area_id"])
            ):
                raise Forbidden(
                    "só o custodiante do certificado ou quem administra a área revoga a autorização"
                )
            if row["revoked_at"] is not None:
                raise Conflict("a autorização já foi revogada")
            row = (
                (
                    await session.execute(
                        update(a1_authorizations)
                        .where(
                            a1_authorizations.c.company_id == company_id,
                            a1_authorizations.c.id == authorization_id,
                        )
                        .values(revoked_at=func.now(), revoked_by=principal.subject)
                        .returning(*a1_authorizations.c)
                    )
                )
                .mappings()
                .one()
            )
            await audit.append(
                session,
                company_id=company_id,
                actor=principal.subject,
                action="credencial.a1_autorizacao_revogada",
                target=f"autorizacao_a1:{authorization_id}",
                payload={"motivo": "pedido", "robo": str(row["robot_id"])},
            )
        return self._authorization(row, credential, now)

    async def list_authorizations(
        self, principal: Principal, robot_id: UUID
    ) -> builtins.list[A1AuthorizationInfo]:
        """As autorizações do robô (ativas e revogadas), da mais nova para a mais antiga."""
        company_id = principal.company_id
        now = self.clock()
        async with self.db.tenant_session(company_id) as session:
            await self._robot(session, principal, robot_id)
            rows = (
                (
                    await session.execute(
                        select(a1_authorizations)
                        .where(
                            a1_authorizations.c.company_id == company_id,
                            a1_authorizations.c.robot_id == robot_id,
                        )
                        .order_by(a1_authorizations.c.created_at.desc(), a1_authorizations.c.id)
                    )
                )
                .mappings()
                .all()
            )
            ids = sorted({row["credential_id"] for row in rows})
            by_id = {
                row["id"]: row
                for row in (
                    await session.execute(
                        _credential_query().where(
                            credentials.c.company_id == company_id, credentials.c.id.in_(ids)
                        )
                    )
                )
                .mappings()
                .all()
            }
        return [self._authorization(row, by_id.get(row["credential_id"]), now) for row in rows]

    # -- aprovações ----------------------------------------------------------------------

    async def approvals(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> VersionApprovals:
        """Quem aprovou a versão, quantas aprovações faltam e o que falta no certificado A1."""
        company_id = principal.company_id
        async with self.db.tenant_session(company_id) as session:
            robot = await self._robot(session, principal, robot_id)
            version = (
                (
                    await session.execute(
                        select(robot_versions.c.status, robot_versions.c.manifest).where(
                            robot_versions.c.robot_id == robot_id,
                            robot_versions.c.version_no == version_no,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if version is None:
                raise NotFound("versão não encontrada")
            manifest = Manifest.model_validate(version["manifest"])
            status = VersionStatus(version["status"])
            approvers = (await approvers_of(session, company_id, robot_id, version_no)).get(
                version_no, []
            )
            problems: Sequence[str] = []
            if uses_a1(manifest) and status in (VersionStatus.DRAFT, VersionStatus.VALIDATED):
                problems = await a1_problems(
                    session,
                    company_id=company_id,
                    area_id=robot["area_id"],
                    robot_id=robot_id,
                    manifest=manifest,
                    now=self.clock(),
                )
        required = required_approvals(manifest)
        return VersionApprovals(
            robot_id=robot_id,
            version_no=version_no,
            status=status,
            required=required,
            approvals=[ApprovalInfo(approved_by=who, approved_at=when) for who, when in approvers],
            missing=max(0, required - len(approvers)),
            problems=list(problems),
        )


class CredentialAlerts:
    """A etapa do despachante que registra os avisos de vencimento dos certificados A1.

    Roda pelo banco do despachante (todas as empresas), a cada 6 horas (ver
    :class:`rapa.control.services.dispatcher.Dispatcher`); cada credencial recebe no
    máximo um aviso por dia de cada tipo (``vencendo`` ou ``vencida``). Trata os
    certificados em lotes de ``batch_size``, cada um numa transação curta, até não
    sobrar nenhum sem o aviso do dia (no máximo ``max_batches`` lotes por volta).
    """

    def __init__(
        self,
        db: Database,
        *,
        warn_days: int = DEFAULT_WARN_DAYS,
        clock: Callable[[], datetime] = now_utc,
        batch_size: int = ALERT_BATCH,
        max_batches: int = MAX_ALERT_BATCHES,
    ) -> None:
        if not 1 <= warn_days <= MAX_WARN_DAYS:
            raise ValueError(f"o prazo do aviso precisa ficar entre 1 e {MAX_WARN_DAYS} dias")
        if batch_size < 1 or max_batches < 1:
            raise ValueError("o lote e o número de lotes do aviso precisam ser positivos")
        self.db = db
        self.warn_days = warn_days
        self.clock = clock
        self.batch_size = batch_size
        self.max_batches = max_batches

    async def check(self, now: datetime | None = None) -> int:
        """Registra os avisos do dia que faltam; devolve quantos registrou."""
        when = now or self.clock()
        registered = 0
        for _ in range(self.max_batches):
            async with self.db.dispatcher_session() as session:
                batch = await record_expiry_alerts(
                    session, now=when, within_days=self.warn_days, limit=self.batch_size
                )
            registered += len(batch.alerts)
            # Cada certificado do lote ganhou o aviso do dia (daqui ou de um despachante
            # ao lado), então o próximo lote pega outros; um lote incompleto foi o último.
            if batch.seen < self.batch_size:
                break
        return registered
