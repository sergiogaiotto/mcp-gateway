"""Implementação em memória dos contratos do plano de controle.

Serve para testar a camada HTTP (REST e MCP) e a interface sem PostgreSQL, e
para rodar uma demonstração local. Segue as mesmas regras da implementação
real: tudo é filtrado pela empresa do principal, publicar exige um aprovador
diferente de quem criou a versão e a chave de idempotência devolve a mesma
execução.
"""

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rapa.core import policy
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    Principal,
    RobotInfo,
    RobotStatus,
    RobotSummary,
    RobotVersionInfo,
    Role,
    RunState,
    RunStatus,
    RunTrigger,
    VersionStatus,
)
from rapa.core.ids import new_id
from rapa.core.robot import Manifest

__all__ = ["MemoryPlatform", "make_principal"]

_RUN_ROLES = frozenset({Role.OPERATOR, Role.CREATOR, Role.APPROVER, Role.AREA_ADMIN})


def make_principal(
    company_id: UUID,
    *roles: Role,
    subject: str = "usuario@empresa",
    area_roles: dict[UUID, frozenset[Role]] | None = None,
) -> Principal:
    """Atalho para testes."""
    return Principal(
        subject=subject,
        company_id=company_id,
        company_roles=frozenset(roles),
        area_roles=area_roles or {},
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c)).casefold()


@dataclass
class _Version:
    version_id: UUID
    version_no: int
    status: VersionStatus
    manifest: Manifest
    created_by: str
    created_at: datetime
    violations: list[str]
    approved_by: str | None = None


@dataclass
class _Robot:
    robot_id: UUID
    company_id: UUID
    area_id: UUID
    slug: str
    created_at: datetime
    versions: list[_Version] = field(default_factory=list)
    status: RobotStatus = RobotStatus.DRAFT
    current_version_no: int | None = None

    def published(self) -> _Version | None:
        if self.current_version_no is None or self.status is not RobotStatus.PUBLISHED:
            return None
        return self.versions[self.current_version_no - 1]


@dataclass
class _Run:
    company_id: UUID
    status: RunStatus
    inputs: dict[str, Any]
    manifest: Manifest


class MemoryPlatform:
    """Catálogo, administração de robôs e execuções, tudo em memória."""

    def __init__(self) -> None:
        self.robots: dict[UUID, _Robot] = {}
        self.runs: dict[UUID, _Run] = {}
        self._idempotency: dict[tuple[UUID, str], tuple[str, UUID]] = {}

    # -- auxiliares ----------------------------------------------------------

    def _visible(self, principal: Principal, robot: _Robot) -> bool:
        if robot.company_id != principal.company_id:
            return False
        return bool(principal.roles_in(robot.area_id))

    def _robot(self, principal: Principal, robot_id: UUID) -> _Robot:
        robot = self.robots.get(robot_id)
        if robot is None or not self._visible(principal, robot):
            raise NotFound("robô não encontrado")
        return robot

    def _version(self, robot: _Robot, version_no: int) -> _Version:
        if not 1 <= version_no <= len(robot.versions):
            raise NotFound("versão não encontrada")
        return robot.versions[version_no - 1]

    @staticmethod
    def _version_info(robot: _Robot, version: _Version) -> RobotVersionInfo:
        return RobotVersionInfo(
            robot_id=robot.robot_id,
            version_id=version.version_id,
            version_no=version.version_no,
            status=version.status,
            manifest=version.manifest,
            fingerprint=version.manifest.fingerprint(),
            created_by=version.created_by,
            approved_by=version.approved_by,
            created_at=version.created_at,
            violations=version.violations,
        )

    @staticmethod
    def _summary(robot: _Robot, version: _Version) -> RobotSummary:
        manifest = version.manifest
        return RobotSummary(
            robot_id=robot.robot_id,
            company_id=robot.company_id,
            area_id=robot.area_id,
            slug=robot.slug,
            title=manifest.title,
            description=manifest.description,
            version_id=version.version_id,
            version_no=version.version_no,
            effect=manifest.effect(),
            input_schema=manifest.input_json_schema(),
            output_schema=manifest.output_json_schema(),
            fingerprint=manifest.fingerprint(),
        )

    # -- RobotAdmin ------------------------------------------------------------

    async def list_robots(self, principal: Principal) -> list[RobotInfo]:
        out = []
        for robot in self.robots.values():
            if not self._visible(principal, robot):
                continue
            latest = robot.versions[-1]
            out.append(
                RobotInfo(
                    robot_id=robot.robot_id,
                    company_id=robot.company_id,
                    area_id=robot.area_id,
                    slug=robot.slug,
                    title=latest.manifest.title,
                    status=robot.status,
                    current_version_no=robot.current_version_no,
                    created_at=robot.created_at,
                )
            )
        return sorted(out, key=lambda r: r.slug)

    async def create_version(
        self, principal: Principal, area_id: UUID, manifest: Manifest
    ) -> RobotVersionInfo:
        if not principal.has_role(Role.CREATOR, area_id):
            raise Forbidden("só quem cria robôs nesta área pode criar versões")
        robot = next(
            (
                r
                for r in self.robots.values()
                if r.company_id == principal.company_id and r.slug == manifest.name
            ),
            None,
        )
        if robot is None:
            robot = _Robot(
                robot_id=new_id(),
                company_id=principal.company_id,
                area_id=area_id,
                slug=manifest.name,
                created_at=_now(),
            )
            self.robots[robot.robot_id] = robot
        elif robot.area_id != area_id:
            raise Conflict("já existe um robô com este nome em outra área")
        version = _Version(
            version_id=new_id(),
            version_no=len(robot.versions) + 1,
            status=VersionStatus.DRAFT,
            manifest=manifest,
            created_by=principal.subject,
            created_at=_now(),
            violations=[v.message for v in policy.check(manifest)],
        )
        robot.versions.append(version)
        return self._version_info(robot, version)

    async def get_version(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo | None:
        try:
            robot = self._robot(principal, robot_id)
            return self._version_info(robot, self._version(robot, version_no))
        except NotFound:
            return None

    async def mark_validated(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo:
        robot = self._robot(principal, robot_id)
        if not (
            principal.has_role(Role.CREATOR, robot.area_id)
            or principal.has_role(Role.APPROVER, robot.area_id)
        ):
            raise Forbidden("sem permissão para validar")
        version = self._version(robot, version_no)
        if version.violations:
            raise InvalidInput("a versão tem violações de política", version.violations)
        if version.status is not VersionStatus.DRAFT:
            raise Conflict("só um rascunho pode ser marcado como validado")
        version.status = VersionStatus.VALIDATED
        return self._version_info(robot, version)

    async def publish(
        self, principal: Principal, robot_id: UUID, version_no: int
    ) -> RobotVersionInfo:
        robot = self._robot(principal, robot_id)
        if not principal.has_role(Role.APPROVER, robot.area_id):
            raise Forbidden("só quem aprova nesta área pode publicar")
        version = self._version(robot, version_no)
        if version.created_by == principal.subject:
            raise Forbidden("quem criou a versão não pode aprová-la")
        if version.status is not VersionStatus.VALIDATED:
            raise Conflict("a versão precisa estar validada antes de publicar")
        previous = robot.published()
        if previous is not None:
            previous.status = VersionStatus.SUPERSEDED
        version.status = VersionStatus.PUBLISHED
        version.approved_by = principal.subject
        robot.status = RobotStatus.PUBLISHED
        robot.current_version_no = version.version_no
        return self._version_info(robot, version)

    # -- RobotCatalog ----------------------------------------------------------

    async def list_published(self, principal: Principal) -> list[RobotSummary]:
        out = []
        for robot in self.robots.values():
            version = robot.published()
            if version is not None and self._visible(principal, robot):
                out.append(self._summary(robot, version))
        return sorted(out, key=lambda s: s.slug)

    async def get_published(self, principal: Principal, slug: str) -> RobotSummary | None:
        return next((s for s in await self.list_published(principal) if s.slug == slug), None)

    async def search(self, principal: Principal, query: str, limit: int = 10) -> list[RobotSummary]:
        terms = [t for t in _fold(query).split() if t]
        scored = []
        for summary in await self.list_published(principal):
            haystack = _fold(f"{summary.slug} {summary.title} {summary.description}")
            score = sum(1 for t in terms if t in haystack)
            if score or not terms:
                scored.append((score, summary))
        scored.sort(key=lambda item: (-item[0], item[1].slug))
        return [summary for _, summary in scored[:limit]]

    # -- RunService ------------------------------------------------------------

    async def start(
        self,
        principal: Principal,
        slug: str,
        inputs: dict[str, Any],
        trigger: RunTrigger,
        idempotency_key: str | None = None,
    ) -> RunStatus:
        robot = next(
            (
                r
                for r in self.robots.values()
                if r.slug == slug and r.published() is not None and self._visible(principal, r)
            ),
            None,
        )
        if robot is None:
            raise NotFound(f"robô '{slug}' não encontrado")
        if not any(principal.has_role(role, robot.area_id) for role in _RUN_ROLES):
            raise Forbidden("sem permissão para executar este robô")
        version = robot.published()
        if version is None:  # pragma: no cover - filtrado acima
            raise NotFound(f"robô '{slug}' não encontrado")
        manifest = version.manifest
        errors = self._check_inputs(manifest, inputs)
        if errors:
            raise InvalidInput("entradas inválidas", errors)
        request_hash = hashlib.sha256(
            json.dumps({"slug": slug, "inputs": inputs}, sort_keys=True, default=str).encode()
        ).hexdigest()
        if idempotency_key is not None:
            existing = self._idempotency.get((principal.company_id, idempotency_key))
            if existing is not None:
                stored_hash, run_id = existing
                if stored_hash != request_hash:
                    raise Conflict("a chave de idempotência já foi usada com outra requisição")
                return self.runs[run_id].status
        status = RunStatus(
            run_id=new_id(),
            robot_slug=slug,
            version_no=version.version_no,
            state=RunState.QUEUED,
            trigger=trigger,
            created_at=_now(),
        )
        self.runs[status.run_id] = _Run(principal.company_id, status, dict(inputs), manifest)
        if idempotency_key is not None:
            self._idempotency[(principal.company_id, idempotency_key)] = (
                request_hash,
                status.run_id,
            )
        return status

    async def get(self, principal: Principal, run_id: UUID) -> RunStatus | None:
        run = self.runs.get(run_id)
        if run is None or run.company_id != principal.company_id:
            return None
        return run.status

    @staticmethod
    def _check_inputs(manifest: Manifest, inputs: dict[str, Any]) -> list[str]:
        declared = {f.name: f for f in manifest.inputs}
        errors = [f"entrada não declarada: {name}" for name in inputs if name not in declared]
        errors += [
            f"entrada obrigatória ausente: {f.name}"
            for f in manifest.inputs
            if f.required and f.name not in inputs
        ]
        return errors

    # -- auxiliar de testes ----------------------------------------------------

    def set_run_state(
        self,
        run_id: UUID,
        state: RunState,
        *,
        outputs: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RunStatus:
        run = self.runs[run_id]
        now = _now()
        run.status = run.status.model_copy(
            update={
                "state": state,
                "outputs": outputs,
                "error_code": error_code,
                "error_message": error_message,
                "started_at": run.status.started_at or now,
                "finished_at": now if state.finished else None,
            }
        )
        return run.status
