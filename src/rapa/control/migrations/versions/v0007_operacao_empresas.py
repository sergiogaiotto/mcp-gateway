"""Operação por empresa: situação, vínculo com a organização, consumo e saída.

Revisão: 0007
Anterior: 0005 (durante o desenvolvimento; a integração encadeia 0006 → 0007 → 0008)

- ``companies`` ganha a situação (``status``: ``active`` ou ``frozen``, com o motivo
  e o momento da mudança) e o vínculo com a organização do provedor de identidade
  (``idp_organization``: o id ou o apelido da organização no Keycloak, único). Uma
  empresa congelada lê tudo, mas nada novo começa (a regra fica nos serviços, em
  ``rapa.control.services._common.ensure_company_active``); só o dono das tabelas
  muda a situação (a aplicação continua sem ``UPDATE`` em ``companies``).
- ``rapa_company_for_organization(text)``: acha a empresa de uma organização sem
  fixar empresa nenhuma (o token chega antes de se saber a empresa). É ``SECURITY
  DEFINER`` (dona: o dono das tabelas), com ``search_path`` fixo e ``EXECUTE`` só
  para o papel da aplicação. Como as tabelas têm ``FORCE ROW LEVEL SECURITY`` (que
  vale também para o dono), a política ``organization_lookup`` deixa o dono, e só
  ele, ler ``companies`` enquanto a marca ``rapa.organization_lookup`` está ligada; a
  função liga a marca só durante a consulta e a desliga antes de voltar. A aplicação
  não consegue usar a marca: a política não vale para ela.
- O despachante lê só ``companies.id`` e ``companies.status``: ele não pega execução
  de empresa congelada.
- Empresa congelada também no banco: ``rapa_company_active_guard`` (``BEFORE INSERT``)
  recusa linha nova de empresa congelada em execuções, robôs, versões, agendas,
  webhooks, lotes, sessões de gravação e consertos, mesmo de um caminho que esqueceu
  ``ensure_company_active``. Antes de ler a situação, toma a mesma trava consultiva
  (compartilhada, classe 7130) que toda escrita toma: espera um congelamento em
  andamento e vê a empresa já congelada. A linha só é lida se for visível (a da
  empresa da transação); a de outra empresa o isolamento por linha já recusa.
- O último ``company_admin`` ativo também no banco: ``rapa_company_admin_guard`` é um
  gatilho de restrição adiado (confere no fim da transação) em ``memberships`` (tirar ou
  trocar um ``company_admin`` da empresa inteira) e em ``users`` (desativar, apagar ou
  mudar de empresa um administrador ativo). Se a empresa ficaria sem nenhum
  ``company_admin`` ativo, recusa. Toma a trava das mudanças de papéis da empresa
  (classe 7131, a mesma do serviço), então duas transações tirando ao mesmo tempo os
  dois últimos administradores não passam juntas. A saída da empresa (dono com a marca
  ``rapa.purge_company``) passa.
- ``platform_events``: registros da plataforma sem empresa (hoje, ``empresa.apagada``,
  gravado antes de apagar uma empresa, com o SHA-256 do nome e as contagens por
  tabela). Sem isolamento por empresa e sem permissão para os papéis da aplicação e
  do despachante: só o dono lê e escreve. Só aceita inclusões.
- Apagar uma empresa: as travas de "só inclusões" (auditoria, gravações, ocorrências,
  entregas, lotes, retratos, consertos) e as que proíbem apagar (versões, sessões de
  gravação, agendas, webhooks) passam a aceitar a exclusão de uma linha só quando a
  transação é do dono da tabela e marcou ``SET LOCAL rapa.purge_company`` com a
  empresa da linha (``rapa_purge_allowed``). Nada muda para os papéis da aplicação e
  do despachante, que nem são donos das tabelas.
- O consumo mensal lê ``runs`` pelo índice ``runs_company_created`` e ``batches`` por
  ``batches_company_created``; as sessões de gravação ganham
  ``recording_sessions_company_created``.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, company_grants_sql, company_revoke_sql

revision: str = "0007"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURGE = "public.rapa_purge_allowed(TG_RELID, OLD.company_id)"


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _companies() -> list[str]:
    return [
        """
        ALTER TABLE companies
            ADD COLUMN status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'frozen')),
            ADD COLUMN status_reason text
                CHECK (status_reason IS NULL OR length(status_reason) BETWEEN 1 AND 500),
            ADD COLUMN status_changed_at timestamptz,
            ADD COLUMN idp_organization text UNIQUE
                CHECK (
                    idp_organization IS NULL
                    OR (length(idp_organization) BETWEEN 1 AND 255
                        AND idp_organization !~ '[[:cntrl:]]')
                ),
            ADD CONSTRAINT companies_frozen_has_reason
                CHECK ((status = 'frozen') = (status_reason IS NOT NULL))
        """,
        "CREATE INDEX recording_sessions_company_created"
        " ON recording_sessions (company_id, created_at)",
    ]


def _organization_lookup() -> list[str]:
    return [
        # Só o dono (quem roda a migração, dono da função abaixo) e só com a marca ligada.
        """
        CREATE POLICY organization_lookup ON companies FOR SELECT TO CURRENT_USER
            USING (current_setting('rapa.organization_lookup', true) = 'on')
        """,
        # A marca vale só durante a consulta: é desligada antes de voltar (e um erro
        # desfaz a transação, ou o ponto de salvamento, junto com ela).
        """
        CREATE FUNCTION rapa_company_for_organization(organization text) RETURNS uuid
            LANGUAGE plpgsql VOLATILE SECURITY DEFINER
            SET search_path = pg_catalog, public
        AS $$
        DECLARE
            found uuid;
        BEGIN
            IF organization IS NULL OR length(organization) NOT BETWEEN 1 AND 255 THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('rapa.organization_lookup', 'on', true);
            SELECT c.id INTO found FROM public.companies c WHERE c.idp_organization = organization;
            PERFORM set_config('rapa.organization_lookup', '', true);
            RETURN found;
        END
        $$
        """,
        "REVOKE ALL ON FUNCTION rapa_company_for_organization(text) FROM PUBLIC",
    ]


def _platform_events() -> list[str]:
    return [
        """
        CREATE TABLE platform_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_.]{1,80}$'),
            actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 255),
            target text NOT NULL CHECK (length(target) BETWEEN 1 AND 255),
            data jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(data) = 'object')
        )
        """,
        "REVOKE ALL ON TABLE platform_events FROM PUBLIC",
        """
        CREATE FUNCTION rapa_platform_events_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = 'os registros da plataforma só aceitam inclusões';
        END
        $$
        """,
        """
        CREATE TRIGGER platform_events_append_only
            BEFORE UPDATE OR DELETE ON platform_events
            FOR EACH ROW EXECUTE FUNCTION rapa_platform_events_guard()
        """,
        """
        CREATE TRIGGER platform_events_no_truncate
            BEFORE TRUNCATE ON platform_events
            FOR EACH STATEMENT EXECUTE FUNCTION rapa_platform_events_guard()
        """,
    ]


def _purge_allowed() -> str:
    return """
        CREATE FUNCTION rapa_purge_allowed(rel oid, company uuid) RETURNS boolean
            LANGUAGE sql STABLE
            SET search_path = pg_catalog
        AS $$
            SELECT company IS NOT NULL
               AND coalesce(current_setting('rapa.purge_company', true), '') = company::text
               AND EXISTS (
                   SELECT 1 FROM pg_class c
                    WHERE c.oid = rel
                      AND c.relowner = (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user)
               )
        $$
        """


def _append_only(*, purge: bool) -> str:
    allow = (
        f"""
            IF TG_OP = 'DELETE' AND TG_LEVEL = 'ROW' THEN
                IF {_PURGE} THEN
                    RETURN OLD;
                END IF;
            END IF;"""
        if purge
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION rapa_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN{allow}
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = format('a tabela %s só aceita inclusões', TG_TABLE_NAME);
        END
        $$
        """


def _delete_guard(message: str, *, purge: bool) -> str:
    """O começo dos guardas que proíbem apagar: com ``purge``, aceita a saída da empresa."""
    allow = (
        f"""
                IF {_PURGE} THEN
                    RETURN OLD;
                END IF;"""
        if purge
        else ""
    )
    return f"""
            IF TG_OP = 'DELETE' THEN{allow}
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = '{message}';
            END IF;"""


def _robot_versions_guard(*, purge: bool) -> str:
    start = _delete_guard("versões de robô não podem ser apagadas", purge=purge)
    return f"""
        CREATE OR REPLACE FUNCTION rapa_robot_versions_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN{start}
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.version_no IS DISTINCT FROM OLD.version_no
                OR NEW.manifest IS DISTINCT FROM OLD.manifest
                OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
                OR NEW.violations IS DISTINCT FROM OLD.violations
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a versão do robô é imutável: só o estado e o aprovador podem mudar';
            END IF;
            IF OLD.approved_by IS NOT NULL AND NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o aprovador de uma versão não pode ser trocado';
            END IF;
            RETURN NEW;
        END
        $$
        """


def _recording_sessions_guard(*, purge: bool) -> str:
    start = _delete_guard("sessões de gravação não podem ser apagadas", purge=purge)
    return f"""
        CREATE OR REPLACE FUNCTION rapa_recording_sessions_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN{start}
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.area_id IS DISTINCT FROM OLD.area_id
                OR NEW.owner IS DISTINCT FROM OLD.owner
                OR NEW.start_url IS DISTINCT FROM OLD.start_url
                OR NEW.instance_id IS DISTINCT FROM OLD.instance_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a sessão de gravação não pode trocar de empresa, área, dona, endereço, processo ou prazo';
            END IF;
            IF OLD.status IN ('expired', 'failed') AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma sessão de gravação encerrada não muda de situação';
            END IF;
            IF OLD.status IN ('stopped', 'drafted') AND NEW.status NOT IN ('stopped', 'drafted') THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma sessão de gravação parada não volta a gravar';
            END IF;
            IF OLD.status = 'drafted' AND NEW.status <> 'drafted' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma sessão de gravação com rascunho não volta a ser só parada';
            END IF;
            IF (NEW.robot_id, NEW.version_no) IS DISTINCT FROM (OLD.robot_id, OLD.version_no) THEN
                IF NOT (NEW.status = 'drafted' AND OLD.status IN ('stopped', 'drafted')) THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'o robô de uma sessão de gravação só muda ao criar um rascunho';
                END IF;
                IF NEW.robot_id = OLD.robot_id AND NEW.version_no <= OLD.version_no THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'a versão do rascunho de uma sessão de gravação só avança';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """


def _schedules_guard(*, purge: bool) -> str:
    start = _delete_guard("agendas não podem ser apagadas; remova-as (arquivar)", purge=purge)
    return f"""
        CREATE OR REPLACE FUNCTION rapa_schedules_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN{start}
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a agenda não pode trocar de empresa, robô ou autor';
            END IF;
            IF OLD.archived_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma agenda removida não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """


def _webhooks_guard(*, purge: bool) -> str:
    start = _delete_guard("webhooks não podem ser apagados; remova-os (arquivar)", purge=purge)
    return f"""
        CREATE OR REPLACE FUNCTION rapa_webhooks_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN{start}
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.created_by IS DISTINCT FROM OLD.created_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o webhook não pode trocar de empresa, robô ou autor';
            END IF;
            IF NEW.key_version < OLD.key_version
                OR (NEW.key_salt IS DISTINCT FROM OLD.key_salt
                    AND NEW.key_version <= OLD.key_version)
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a chave do webhook só muda para uma versão nova';
            END IF;
            IF OLD.archived_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'um webhook removido não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """


#: Tabelas em que uma linha nova é "algo novo acontecendo" (recusada com a empresa congelada).
_FROZEN_GUARDED = (
    "runs",
    "robots",
    "robot_versions",
    "schedules",
    "webhooks",
    "batches",
    "recording_sessions",
    "repair_proposals",
)


def _company_active_guard() -> list[str]:
    return [
        """
        CREATE FUNCTION rapa_company_active_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, public
        AS $$
        DECLARE
            current_status text;
        BEGIN
            -- A mesma trava, compartilhada, que toda escrita toma (e que congelar toma
            -- exclusiva): espera um congelamento em andamento e lê a situação depois dele.
            PERFORM pg_advisory_xact_lock_shared(7130, hashtext(NEW.company_id::text));
            SELECT c.status INTO current_status FROM public.companies c WHERE c.id = NEW.company_id;
            IF current_status = 'frozen' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a empresa está congelada: nada novo pode começar';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        *(
            f"CREATE TRIGGER {table}_company_active BEFORE INSERT ON {table}"
            " FOR EACH ROW EXECUTE FUNCTION rapa_company_active_guard()"
            for table in _FROZEN_GUARDED
        ),
    ]


def _company_admin_guard() -> list[str]:
    return [
        """
        CREATE FUNCTION rapa_company_admin_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, public
        AS $$
        DECLARE
            company uuid := OLD.company_id;
        BEGIN
            IF TG_TABLE_NAME = 'users' THEN
                -- Só um administrador ativo que deixa de ser (desativado, apagado ou movido).
                IF TG_OP = 'UPDATE' THEN
                    IF NEW.active AND NEW.company_id = OLD.company_id THEN
                        RETURN NULL;
                    END IF;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.memberships m
                     WHERE m.company_id = company AND m.user_id = OLD.id
                       AND m.role = 'company_admin' AND m.area_id IS NULL
                ) THEN
                    RETURN NULL;  -- sem o papel (ou o vínculo saiu junto: o gatilho dele confere)
                END IF;
            ELSIF TG_OP = 'UPDATE' THEN
                IF NEW.role = 'company_admin' AND NEW.area_id IS NULL
                    AND NEW.company_id = OLD.company_id AND NEW.user_id = OLD.user_id
                THEN
                    RETURN NULL;  -- o mesmo papel, da mesma pessoa
                END IF;
            END IF;
            -- A saída da empresa (o dono com a marca) e a empresa que já não existe passam.
            IF public.rapa_purge_allowed(TG_RELID, company) THEN
                RETURN NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.companies c WHERE c.id = company) THEN
                RETURN NULL;
            END IF;
            -- A trava das mudanças de papéis da empresa (a do serviço): uma de cada vez, e a
            -- contagem abaixo já vê o que a anterior confirmou.
            PERFORM pg_advisory_xact_lock(7131, hashtext(company::text));
            IF NOT EXISTS (
                SELECT 1 FROM public.users u
                  JOIN public.memberships m ON m.company_id = u.company_id AND m.user_id = u.id
                 WHERE u.company_id = company AND u.active
                   AND m.role = 'company_admin' AND m.area_id IS NULL
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a empresa precisa de pelo menos um administrador ativo (company_admin)';
            END IF;
            RETURN NULL;
        END
        $$
        """,
        """
        CREATE CONSTRAINT TRIGGER memberships_keep_company_admin
            AFTER UPDATE OR DELETE ON memberships
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (OLD.role = 'company_admin' AND OLD.area_id IS NULL)
            EXECUTE FUNCTION rapa_company_admin_guard()
        """,
        """
        CREATE CONSTRAINT TRIGGER users_keep_company_admin
            AFTER UPDATE OF active, company_id OR DELETE ON users
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            WHEN (OLD.active)
            EXECUTE FUNCTION rapa_company_admin_guard()
        """,
    ]


def _drop_company_guards() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS users_keep_company_admin ON users",
        "DROP TRIGGER IF EXISTS memberships_keep_company_admin ON memberships",
        "DROP FUNCTION IF EXISTS rapa_company_admin_guard()",
        *(f"DROP TRIGGER IF EXISTS {table}_company_active ON {table}" for table in _FROZEN_GUARDED),
        "DROP FUNCTION IF EXISTS rapa_company_active_guard()",
    ]


def _guards(*, purge: bool) -> list[str]:
    return [
        _append_only(purge=purge),
        _robot_versions_guard(purge=purge),
        _recording_sessions_guard(purge=purge),
        _schedules_guard(purge=purge),
        _webhooks_guard(purge=purge),
    ]


def _comments() -> list[str]:
    return [
        "COMMENT ON COLUMN companies.status IS"
        " 'Situação: active ou frozen (congelada: lê tudo, nada novo começa)'",
        "COMMENT ON COLUMN companies.idp_organization IS"
        " 'Id ou apelido da organização no provedor de identidade (Keycloak)'",
        "COMMENT ON TABLE platform_events IS"
        " 'Registros da plataforma sem empresa (empresa.apagada); só o dono; só inclusões'",
    ]


def upgrade() -> None:
    for statement in (
        *_companies(),
        *_organization_lookup(),
        *_platform_events(),
        _purge_allowed(),
        *_guards(purge=True),
        *_company_active_guard(),
        *_company_admin_guard(),
        *_comments(),
        *company_grants_sql(_roles()),
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        *_drop_company_guards(),
        *_guards(purge=False),
        "DROP FUNCTION IF EXISTS rapa_purge_allowed(oid, uuid)",
        "DROP TABLE IF EXISTS platform_events",
        "DROP FUNCTION IF EXISTS rapa_platform_events_guard()",
        "DROP FUNCTION IF EXISTS rapa_company_for_organization(text)",
        "DROP POLICY IF EXISTS organization_lookup ON companies",
        *company_revoke_sql(_roles()),
        "DROP INDEX IF EXISTS recording_sessions_company_created",
        "ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_frozen_has_reason,"
        " DROP COLUMN IF EXISTS idp_organization, DROP COLUMN IF EXISTS status_changed_at,"
        " DROP COLUMN IF EXISTS status_reason, DROP COLUMN IF EXISTS status",
    ):
        op.execute(statement)
