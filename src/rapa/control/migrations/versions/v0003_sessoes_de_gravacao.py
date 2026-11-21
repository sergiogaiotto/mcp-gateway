"""Sessões de gravação e gravações: ``recording_sessions`` e ``recordings``.

Revisão: 0003
Anterior: 0002

Uma sessão de gravação é o navegador na nuvem em que a pessoa demonstra a tarefa
(passo "Demonstrar"). A empresa lidera as chaves das duas tabelas, que têm
isolamento por linha (ENABLE e FORCE) com a política de sempre.

- ``recording_sessions``: área, dona (o ``subject`` de quem abriu), endereço
  inicial (sem a consulta nem o fragmento, que podem ter um token), situação,
  prazos e, depois da revisão, o robô e a versão do rascunho. O processo da API
  que segura o navegador fica em ``instance_id`` e renova ``seen_at`` enquanto a
  sessão está aberta: uma sessão aberta que ninguém renova há alguns minutos (a
  API caiu) é encerrada por qualquer processo. O token do canal de controle e os
  bilhetes da visão ao vivo nunca vêm para cá. Uma trava impede trocar a empresa,
  a área, a dona, o endereço, o processo e os prazos; tirar uma sessão encerrada
  (sem atividade, prazo, falha) dessa situação; voltar de parada para aberta ou de
  rascunho para parada; e mudar o robô e a versão fora da criação de um rascunho
  (a versão de um mesmo robô só avança, e precisa existir).
- ``recordings``: a gravação da demonstração (o JSON do gravador, que já sai
  sem segredos), uma por sessão, imutável.

O despachante só lê a situação das sessões (``company_id``, ``id``, ``status``,
``expires_at`` e ``seen_at``), para contar as abertas na reserva de vagas
interativas e achar as que nenhum processo segura mais.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, session_grants_sql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = ("starting", "active", "stopped", "drafted", "expired", "failed")
_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def upgrade() -> None:
    statements = [
        f"""
        CREATE TABLE recording_sessions (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            area_id uuid NOT NULL,
            owner text NOT NULL CHECK (length(owner) BETWEEN 1 AND 255),
            title text NOT NULL DEFAULT '' CHECK (length(title) <= 120),
            start_url text NOT NULL
                CHECK (length(start_url) BETWEEN 8 AND 2000 AND start_url ~ '^https?://'),
            status text NOT NULL DEFAULT 'starting' CHECK (status IN ({_in(_STATUS)})),
            end_reason text CHECK (end_reason IS NULL OR end_reason ~ '^[a-z_]{{1,40}}$'),
            robot_id uuid,
            version_no integer CHECK (version_no >= 1),
            instance_id text NOT NULL DEFAULT '' CHECK (length(instance_id) <= 100),
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            stopped_at timestamptz,
            expires_at timestamptz NOT NULL,
            seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, id),
            FOREIGN KEY (company_id, area_id) REFERENCES areas (company_id, id),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            FOREIGN KEY (robot_id, version_no) REFERENCES robot_versions (robot_id, version_no),
            CHECK (expires_at > created_at),
            CHECK (status <> 'drafted' OR (robot_id IS NOT NULL AND version_no IS NOT NULL)),
            CHECK (status = 'drafted' OR (robot_id IS NULL AND version_no IS NULL))
        )
        """,
        """
        CREATE INDEX recording_sessions_owner
            ON recording_sessions (company_id, owner, created_at DESC)
        """,
        """
        CREATE INDEX recording_sessions_live
            ON recording_sessions (company_id, expires_at) WHERE status IN ('starting', 'active')
        """,
        """
        CREATE TABLE recordings (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            session_id uuid NOT NULL,
            created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 255),
            recording jsonb NOT NULL CHECK (jsonb_typeof(recording) = 'object'),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, id),
            UNIQUE (company_id, session_id),
            FOREIGN KEY (company_id, session_id) REFERENCES recording_sessions (company_id, id)
        )
        """,
    ]
    for table in ("recording_sessions", "recordings"):
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (company_id = {_TENANT}) WITH CHECK (company_id = {_TENANT})
            """,
        ]
    statements += [
        """
        CREATE FUNCTION rapa_recording_sessions_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'sessões de gravação não podem ser apagadas';
            END IF;
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
        """,
        """
        CREATE TRIGGER recording_sessions_guard
            BEFORE UPDATE OR DELETE ON recording_sessions
            FOR EACH ROW EXECUTE FUNCTION rapa_recording_sessions_guard()
        """,
        """
        CREATE TRIGGER recordings_append_only
            BEFORE UPDATE OR DELETE ON recordings
            FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
        """,
        "COMMENT ON TABLE recording_sessions IS"
        " 'Sessões de gravação (navegador na nuvem da demonstração); sem tokens nem bilhetes'",
        "COMMENT ON TABLE recordings IS"
        " 'Gravações das demonstrações, já sem segredos; só aceitam inclusões'",
        *session_grants_sql(_roles()),
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS recordings, recording_sessions CASCADE",
        "DROP FUNCTION IF EXISTS rapa_recording_sessions_guard()",
    ):
        op.execute(statement)
