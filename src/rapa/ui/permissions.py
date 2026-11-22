"""Regras de exibição: quais botões aparecem para quem.

Servem só para não mostrar ações que o servidor vai recusar. A decisão de
verdade continua no plano de controle, que confere tudo de novo.
"""

from collections.abc import Collection, Iterable, Sequence
from uuid import UUID

from rapa.core.contracts import (
    CredentialInfo,
    CredentialStatus,
    Principal,
    RobotVersionInfo,
    Role,
    VersionStatus,
    can_automate,
    uses_a1,
)

__all__ = [
    "RUN_ROLES",
    "admin_areas",
    "can_automate",
    "can_automate_anywhere",
    "can_create",
    "can_create_anywhere",
    "can_manage_company",
    "can_manage_roles",
    "can_open_company",
    "can_publish",
    "can_record",
    "can_register_credential",
    "can_register_credential_anywhere",
    "can_revoke_credential",
    "can_run",
    "can_see_usage",
    "can_validate",
    "creator_areas",
    "has_any_role",
    "is_custodian",
    "managed_areas",
    "publish_block_reason",
    "sees_expiry_alerts",
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


def can_record(principal: Principal) -> bool:
    """Se a pessoa pode gravar uma demonstração em alguma área (o papel de criação)."""
    return can_create_anywhere(principal) or bool(creator_areas(principal))


def can_automate_anywhere(principal: Principal) -> bool:
    """Se a pessoa pode criar agendas, webhooks ou lotes em alguma área."""
    return can_automate(principal, None) or any(
        can_automate(principal, area) for area in principal.area_roles
    )


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


def can_publish(
    principal: Principal,
    area_id: UUID,
    version: RobotVersionInfo,
    custodians: Collection[str] = (),
    a1_problems: Sequence[str] = (),
) -> bool:
    return publish_block_reason(principal, area_id, version, custodians, a1_problems) is None


def publish_block_reason(
    principal: Principal,
    area_id: UUID,
    version: RobotVersionInfo,
    custodians: Collection[str] = (),
    a1_problems: Sequence[str] = (),
) -> str | None:
    """Por que a versão não pode ser aprovada por este principal (ou None se pode).

    Espelha :meth:`rapa.control.services.robots.PgRobotAdmin.publish`. Num robô com
    certificado A1, ``custodians`` são os custodiantes dos certificados A1 dele (que não
    aprovam, nem têm a versão que criaram aprovada) e ``a1_problems`` o que ainda falta
    no certificado (o que o servidor devolve em ``VersionApprovals.problems``): com
    alguma pendência, a aprovação não conta, e o botão não aparece.
    """
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
    if principal.subject in version.approvals:
        return "Você já aprovou esta versão; falta a aprovação de outra pessoa."
    if not uses_a1(version.manifest):
        return None
    if principal.subject in custodians:
        return (
            "Quem guarda um certificado A1 do robô (o custodiante) não pode aprovar a versão: "
            "a aprovação é de outra pessoa."
        )
    if version.created_by in custodians:
        return (
            "Esta versão foi criada pelo custodiante de um certificado A1 do robô e não pode "
            "ser aprovada: outra pessoa precisa criar a versão."
        )
    if a1_problems:
        return (
            "O certificado A1 do robô ainda não pode ser usado, e a aprovação não contaria: "
            "veja o que falta no cartão do certificado."
        )
    return None


def admin_areas(principal: Principal, known_areas: Iterable[UUID] = ()) -> list[UUID]:
    """Áreas onde o principal administra (e pode registrar credenciais)."""
    candidates = dict.fromkeys([*principal.area_roles, *known_areas])
    return [area for area in candidates if can_register_credential(principal, area)]


def can_register_credential(principal: Principal, area_id: UUID) -> bool:
    return principal.has_role(Role.AREA_ADMIN, area_id)


def can_register_credential_anywhere(principal: Principal) -> bool:
    """Se a pessoa administra todas as áreas da empresa (e registra em qualquer uma)."""
    return principal.has_role(Role.AREA_ADMIN)


def is_custodian(principal: Principal, credential: CredentialInfo) -> bool:
    """Se o principal é o custodiante da credencial (e ainda tem o papel na área)."""
    return (
        credential.custodian_subject == principal.subject
        and Role.CREDENTIAL_CUSTODIAN in principal.roles_in(credential.area_id)
    )


def can_revoke_credential(principal: Principal, credential: CredentialInfo) -> bool:
    if credential.status is CredentialStatus.REVOKED:
        return False
    return is_custodian(principal, credential) or principal.has_role(
        Role.AREA_ADMIN, credential.area_id
    )


def sees_expiry_alerts(principal: Principal) -> bool:
    """Custodiantes e administradores veem o aviso de vencimento do certificado A1."""
    roles = set(principal.company_roles)
    for area_roles in principal.area_roles.values():
        roles |= area_roles
    return bool(roles & {Role.CREDENTIAL_CUSTODIAN, Role.AREA_ADMIN, Role.COMPANY_ADMIN})


# -- a empresa ------------------------------------------------------------------------


def has_any_role(principal: Principal) -> bool:
    """Se a pessoa tem algum papel (quem acabou de entrar pela primeira vez não tem)."""
    return bool(principal.company_roles) or any(principal.area_roles.values())


def can_manage_company(principal: Principal) -> bool:
    """Quem administra a empresa: usuários, papéis em qualquer lugar e áreas."""
    return principal.has_role(Role.COMPANY_ADMIN)


def managed_areas(principal: Principal) -> frozenset[UUID] | None:
    """Áreas cujos papéis a pessoa administra; ``None`` quando são todas."""
    if principal.has_role(Role.AREA_ADMIN):
        return None
    return frozenset(
        area for area, roles in principal.area_roles.items() if Role.AREA_ADMIN in roles
    )


def can_manage_roles(principal: Principal) -> bool:
    """Se a pessoa dá e tira papéis (na empresa toda ou nas áreas que administra)."""
    managed = managed_areas(principal)
    return can_manage_company(principal) or managed is None or bool(managed)


def can_see_usage(principal: Principal) -> bool:
    """Quem administra a empresa ou audita (na empresa ou numa área) vê o consumo."""
    return principal.has_role(Role.AUDITOR) or any(
        Role.AUDITOR in roles for roles in principal.area_roles.values()
    )


def can_open_company(principal: Principal) -> bool:
    """Se a tela Empresa tem algo para a pessoa (papéis ou consumo)."""
    return can_manage_roles(principal) or can_see_usage(principal)
