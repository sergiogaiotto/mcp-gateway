"""Regras de exibição: quais botões aparecem para quem.

Servem só para não mostrar ações que o servidor vai recusar. A decisão de
verdade continua no plano de controle, que confere tudo de novo.
"""

from collections.abc import Iterable
from uuid import UUID

from rapa.core.contracts import Principal, RobotVersionInfo, Role, VersionStatus

__all__ = [
    "RUN_ROLES",
    "can_create",
    "can_create_anywhere",
    "can_publish",
    "can_run",
    "can_validate",
    "creator_areas",
    "publish_block_reason",
    "validate_block_reason",
]

#: Papéis que podem iniciar uma execução (os mesmos do plano de controle).
RUN_ROLES = frozenset({Role.OPERATOR, Role.CREATOR, Role.APPROVER, Role.AREA_ADMIN})


def can_run(principal: Principal, area_id: UUID) -> bool:
    return any(principal.has_role(role, area_id) for role in RUN_ROLES)


def can_create(principal: Principal, area_id: UUID) -> bool:
    return principal.has_role(Role.CREATOR, area_id)


def can_create_anywhere(principal: Principal) -> bool:
    """Se o papel de criação vale para a empresa toda, e não só para algumas áreas."""
    return principal.has_role(Role.CREATOR)


def creator_areas(principal: Principal, known_areas: Iterable[UUID] = ()) -> list[UUID]:
    """Áreas onde o principal pode criar versões, entre as dele e as conhecidas."""
    candidates = dict.fromkeys([*principal.area_roles, *known_areas])
    return [area for area in candidates if can_create(principal, area)]


def can_validate(principal: Principal, area_id: UUID) -> bool:
    return principal.has_role(Role.CREATOR, area_id) or principal.has_role(Role.APPROVER, area_id)


def validate_block_reason(
    principal: Principal, area_id: UUID, version: RobotVersionInfo
) -> str | None:
    """Por que a versão não pode ser marcada como validada agora (ou None se pode)."""
    if version.status is not VersionStatus.DRAFT:
        return None
    if not can_validate(principal, area_id):
        return "Só quem cria ou aprova robôs nesta área pode validar a versão."
    if version.violations:
        return "A versão tem problemas apontados pelo portão de política; corrija e crie uma nova versão."
    return None


def can_publish(principal: Principal, area_id: UUID, version: RobotVersionInfo) -> bool:
    return publish_block_reason(principal, area_id, version) is None


def publish_block_reason(
    principal: Principal, area_id: UUID, version: RobotVersionInfo
) -> str | None:
    """Por que a versão não pode ser publicada por este principal (ou None se pode)."""
    if version.status is VersionStatus.PUBLISHED:
        return "Esta versão já está publicada."
    if version.status is not VersionStatus.VALIDATED:
        return "A versão precisa estar validada antes de ser publicada."
    if not principal.has_role(Role.APPROVER, area_id):
        return "Só quem aprova robôs nesta área pode publicar."
    if version.created_by == principal.subject:
        return (
            "Quem criou a versão não pode aprová-la. "
            "Peça a outra pessoa com papel de aprovador nesta área."
        )
    return None
