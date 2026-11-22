"""Montagem do principal a partir dos vínculos guardados no banco.

Implementa, pela forma (sem importar), os protocolos de :mod:`rapa.control.auth`:

- ``PrincipalResolver``: a camada HTTP valida o token, tira dele o ``subject`` e a
  empresa, e pede aqui os papéis. A consulta roda fixada na empresa do token, então um
  usuário de outra empresa simplesmente não existe.
- ``UserProvisioner``: o primeiro login de alguém de uma organização ligada a uma
  empresa (o usuário ainda não existe) cria o usuário sem nenhum papel, com auditoria
  ``usuario.criado_no_login``. Usuário desativado continua sem entrar; empresa
  congelada não ganha usuário novo.
- ``OrganizationDirectory`` (:class:`PgOrganizationDirectory`): a empresa ligada a uma
  organização do provedor (``companies.idp_organization``), pela função
  ``rapa_company_for_organization``, sem fixar empresa nenhuma.
"""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rapa.control.db.schema import memberships, users
from rapa.control.services import audit
from rapa.control.services._common import PgService, lock_company_status
from rapa.core.contracts import CompanyStatus, Principal, Role
from rapa.core.ids import new_id

__all__ = ["MAX_DISPLAY_NAME", "PgOrganizationDirectory", "PgPrincipalResolver"]

#: Tamanho máximo do nome de um usuário criado no primeiro login.
MAX_DISPLAY_NAME = 200


class PgPrincipalResolver(PgService):
    """Papéis da empresa e das áreas de um usuário ativo."""

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        async with self.db.tenant_session(company_id) as session:
            user = (
                (
                    await session.execute(
                        select(users.c.id, users.c.active).where(
                            users.c.subject == subject, users.c.company_id == company_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if user is None or not user["active"]:
                return None
            rows = (
                await session.execute(
                    select(memberships.c.area_id, memberships.c.role).where(
                        memberships.c.user_id == user["id"],
                        memberships.c.company_id == company_id,
                    )
                )
            ).all()
        company_roles: set[Role] = set()
        area_roles: dict[UUID, set[Role]] = {}
        for area_id, role in rows:
            if area_id is None:
                company_roles.add(Role(role))
            else:
                area_roles.setdefault(area_id, set()).add(Role(role))
        return Principal(
            subject=subject,
            company_id=company_id,
            user_id=user["id"],
            company_roles=frozenset(company_roles),
            area_roles={area: frozenset(roles) for area, roles in area_roles.items()},
        )

    async def provision(
        self, subject: str, company_id: UUID, display_name: str = ""
    ) -> Principal | None:
        """Cria, no primeiro login, o usuário que ainda não existe, sem nenhum papel.

        ``None`` (sem acesso) quando a empresa não existe ou está congelada, ou quando o
        sujeito já existe desativado nesta empresa ou é de outra empresa (o ``subject`` é
        único na plataforma). Quando o sujeito já existe, o principal é procurado de novo,
        uma vez (:meth:`resolve`, fixado nesta empresa): o primeiro login que chega junto
        com outro do mesmo sujeito perde a inclusão para ele, e entra com o usuário que o
        outro acabou de criar.
        """
        name = " ".join(display_name.split())[:MAX_DISPLAY_NAME]
        async with self.db.tenant_session(company_id) as session:
            if await lock_company_status(session, company_id) is not CompanyStatus.ACTIVE:
                return None
            created = await session.scalar(
                pg_insert(users)
                .values(id=new_id(), company_id=company_id, subject=subject, display_name=name)
                .on_conflict_do_nothing(index_elements=["subject"])
                .returning(users.c.id)
            )
            if created is not None:
                await audit.append(
                    session,
                    company_id=company_id,
                    actor=subject,
                    action="usuario.criado_no_login",
                    target=f"usuario:{created}",
                    payload={"usuario": str(created)},
                )
        if created is None:
            # Já existia (outro login do mesmo sujeito ganhou a inclusão, ou o sujeito é
            # desativado ou de outra empresa): só o usuário ativo desta empresa entra.
            return await self.resolve(subject, company_id)
        return Principal(subject=subject, company_id=company_id, user_id=created)


class PgOrganizationDirectory(PgService):
    """A empresa ligada a uma organização do provedor de identidade (sem fixar empresa)."""

    async def company_for(self, organization: str) -> UUID | None:
        if not 1 <= len(organization) <= 255:
            return None
        # Transação sem empresa: a função (SECURITY DEFINER) só devolve o identificador.
        async with self.db.dispatcher_session() as session:
            found = await session.scalar(select(func.rapa_company_for_organization(organization)))
        return found if isinstance(found, UUID) else None
