"""Regras do certificado A1: validade, autorização do custodiante e dois aprovadores.

Revisão: 0006
Anterior: 0005

- ``credentials`` ganha a validade do certificado (``not_after``), a impressão
  SHA-256 (``fingerprint_sha256``, 64 hexadecimais minúsculos), a revogação
  (``revoked_at`` e ``revoked_by``, uma vez só) e ``updated_at``. Um certificado A1
  novo precisa da validade (``credentials_a1_not_after``, ``NOT VALID``: as linhas
  antigas sem validade continuam no banco, e o serviço as trata como vencidas). Só a
  validade, a impressão e a revogação mudam (``rapa_credentials_guard``); a
  aplicação perde ``DELETE`` e o ``UPDATE`` das outras colunas.
- ``a1_authorizations``: a autorização do custodiante para um robô usar um
  certificado A1 numa origem, com a impressão do certificado no momento. Uma ativa
  por empresa, robô e credencial. Só inclusões, mais a revogação: só ``revoked_at`` e
  ``revoked_by`` mudam, uma vez (``rapa_a1_authorizations_guard``).
- ``version_approvals``: cada aprovação de uma versão (empresa, robô, versão e quem
  aprovou). Só inclusões; quem criou a versão não aprova, só uma versão validada
  recebe aprovações, e, num robô com certificado A1, nem o custodiante de um
  certificado dele aprova nem a versão que o custodiante criou é aprovada
  (``rapa_version_approvals_check``). As versões publicadas antes desta revisão
  ganham a aprovação de quem as publicou (``approved_by``).
- ``robot_versions`` ganha o gatilho ``rapa_robot_versions_approvals``: uma versão
  nasce rascunho e sem aprovador (incluir a versão já publicada pularia as
  aprovações) e só vira ``published`` com o número de aprovações que o manifesto
  exige (duas se ``manifest->'credentials'`` tem algum ``pfx_a1``, senão uma), e quem
  publica está entre os aprovadores.
- ``credential_alerts``: os avisos de vencimento dos certificados A1, no máximo um
  por credencial, dia e tipo (``vencendo`` ou ``vencida``). Só inclusões, pelo
  despachante.

Todas com a empresa liderando as chaves e o isolamento por linha de sempre (ENABLE
e FORCE). O despachante ganha só a leitura das colunas que a conferência antes de
lançar precisa: de ``credentials`` (nunca ``vault_path``) e de ``a1_authorizations``, e,
para contar as aprovações da versão que vai rodar sem as do custodiante, as de
``version_approvals`` (sem a data) e a empresa, o identificador e o sujeito de
``users``; e a inclusão dos avisos.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, a1_grants_sql, a1_restore_sql, a1_revokes_sql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"
_TABLES = ("a1_authorizations", "version_approvals", "credential_alerts")
_SHA256 = "'^[0-9a-f]{64}$'"
_ORIGIN = "'^https?://[^/?#@]+$'"


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _credentials() -> list[str]:
    return [
        "ALTER TABLE credentials ADD COLUMN not_after timestamptz",
        f"""
        ALTER TABLE credentials ADD COLUMN fingerprint_sha256 text
            CONSTRAINT credentials_fingerprint_format
            CHECK (fingerprint_sha256 IS NULL OR fingerprint_sha256 ~ {_SHA256})
        """,
        "ALTER TABLE credentials ADD COLUMN revoked_at timestamptz",
        """
        ALTER TABLE credentials ADD COLUMN revoked_by text
            CONSTRAINT credentials_revoked_by_length
            CHECK (revoked_by IS NULL OR length(revoked_by) BETWEEN 1 AND 255)
        """,
        "ALTER TABLE credentials ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()",
        """
        ALTER TABLE credentials ADD CONSTRAINT credentials_revoked_pair
            CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
        """,
        # As linhas antigas sem validade ficam (NOT VALID); o serviço as trata como vencidas.
        """
        ALTER TABLE credentials ADD CONSTRAINT credentials_a1_not_after
            CHECK (kind <> 'pfx_a1' OR not_after IS NOT NULL OR revoked_at IS NOT NULL)
            NOT VALID
        """,
        "ALTER TABLE credentials ADD CONSTRAINT credentials_company_id_key UNIQUE (company_id, id)",
        """
        ALTER TABLE credentials ADD CONSTRAINT credentials_company_area_id_key
            UNIQUE (company_id, area_id, id)
        """,
        """
        CREATE INDEX credentials_a1_not_after_idx ON credentials (not_after)
            WHERE kind = 'pfx_a1' AND revoked_at IS NULL
        """,
        """
        CREATE FUNCTION rapa_credentials_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
                OR NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.area_id IS DISTINCT FROM OLD.area_id
                OR NEW.name IS DISTINCT FROM OLD.name
                OR NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.origin IS DISTINCT FROM OLD.origin
                OR NEW.vault_path IS DISTINCT FROM OLD.vault_path
                OR NEW.custodian_user_id IS DISTINCT FROM OLD.custodian_user_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'da credencial só mudam a validade, a impressão e a revogação';
            END IF;
            IF OLD.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma credencial revogada não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER credentials_guard
            BEFORE UPDATE ON credentials
            FOR EACH ROW EXECUTE FUNCTION rapa_credentials_guard()
        """,
    ]


def _tables() -> list[str]:
    return [
        f"""
        CREATE TABLE a1_authorizations (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            area_id uuid NOT NULL,
            robot_id uuid NOT NULL,
            credential_id uuid NOT NULL,
            origin text NOT NULL CHECK (origin ~ {_ORIGIN}),
            fingerprint_sha256 text
                CHECK (fingerprint_sha256 IS NULL OR fingerprint_sha256 ~ {_SHA256}),
            authorized_by text NOT NULL CHECK (length(authorized_by) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            revoked_by text CHECK (revoked_by IS NULL OR length(revoked_by) BETWEEN 1 AND 255),
            PRIMARY KEY (company_id, id),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            FOREIGN KEY (company_id, area_id, credential_id)
                REFERENCES credentials (company_id, area_id, id),
            CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
        )
        """,
        """
        CREATE UNIQUE INDEX a1_authorizations_active
            ON a1_authorizations (company_id, robot_id, credential_id) WHERE revoked_at IS NULL
        """,
        """
        CREATE INDEX a1_authorizations_credential
            ON a1_authorizations (company_id, credential_id) WHERE revoked_at IS NULL
        """,
        """
        CREATE TABLE version_approvals (
            company_id uuid NOT NULL REFERENCES companies (id),
            robot_id uuid NOT NULL,
            version_no integer NOT NULL CHECK (version_no >= 1),
            approved_by text NOT NULL CHECK (length(approved_by) BETWEEN 1 AND 255),
            approved_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, robot_id, version_no, approved_by),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            FOREIGN KEY (robot_id, version_no) REFERENCES robot_versions (robot_id, version_no)
        )
        """,
        """
        CREATE TABLE credential_alerts (
            company_id uuid NOT NULL REFERENCES companies (id),
            credential_id uuid NOT NULL,
            day date NOT NULL,
            kind text NOT NULL CHECK (kind IN ('vencendo', 'vencida')),
            not_after timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, credential_id, day, kind),
            FOREIGN KEY (company_id, credential_id) REFERENCES credentials (company_id, id)
        )
        """,
    ]


def _backfill() -> list[str]:
    # As versões já publicadas (ou substituídas) ganham a aprovação de quem as publicou.
    # FORCE ROW LEVEL SECURITY vale para o dono: fica suspenso só nesta transação.
    return [
        "ALTER TABLE robot_versions NO FORCE ROW LEVEL SECURITY",
        """
        INSERT INTO version_approvals (company_id, robot_id, version_no, approved_by, approved_at)
        SELECT company_id, robot_id, version_no, approved_by, created_at
          FROM robot_versions
         WHERE approved_by IS NOT NULL
        """,
        "ALTER TABLE robot_versions FORCE ROW LEVEL SECURITY",
    ]


def _row_level_security() -> list[str]:
    statements: list[str] = []
    for table in _TABLES:
        statements += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (company_id = {_TENANT}) WITH CHECK (company_id = {_TENANT})
            """,
        ]
    return statements


def _triggers() -> list[str]:
    return [
        """
        CREATE FUNCTION rapa_a1_authorizations_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'autorizações do certificado A1 não podem ser apagadas; revogue-as';
            END IF;
            IF NEW.company_id IS DISTINCT FROM OLD.company_id
                OR NEW.id IS DISTINCT FROM OLD.id
                OR NEW.area_id IS DISTINCT FROM OLD.area_id
                OR NEW.robot_id IS DISTINCT FROM OLD.robot_id
                OR NEW.credential_id IS DISTINCT FROM OLD.credential_id
                OR NEW.origin IS DISTINCT FROM OLD.origin
                OR NEW.fingerprint_sha256 IS DISTINCT FROM OLD.fingerprint_sha256
                OR NEW.authorized_by IS DISTINCT FROM OLD.authorized_by
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR NEW.revoked_at IS NULL
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma autorização do certificado A1 só muda para ser revogada';
            END IF;
            IF OLD.revoked_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'uma autorização do certificado A1 revogada não muda mais';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER a1_authorizations_guard
            BEFORE UPDATE OR DELETE ON a1_authorizations
            FOR EACH ROW EXECUTE FUNCTION rapa_a1_authorizations_guard()
        """,
        """
        CREATE TRIGGER version_approvals_append_only
            BEFORE UPDATE OR DELETE ON version_approvals
            FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
        """,
        """
        CREATE TRIGGER credential_alerts_append_only
            BEFORE UPDATE OR DELETE ON credential_alerts
            FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
        """,
        # Quem criou não aprova, e só a versão validada recebe aprovações. Num robô com
        # certificado A1, o custodiante de um certificado dele não aprova, e a versão
        # que o custodiante criou não recebe aprovações (separação de funções). A versão,
        # o robô e as credenciais são lidos com o isolamento de quem inclui; sem a
        # empresa fixada, a inclusão é recusada pela política (WITH CHECK) e pela chave
        # estrangeira.
        """
        CREATE FUNCTION rapa_version_approvals_check() RETURNS trigger
            LANGUAGE plpgsql AS $$
        DECLARE
            author text;
            current_status text;
            document jsonb;
            robot_area uuid;
            custodians text[];
        BEGIN
            SELECT v.created_by, v.status, v.manifest, r.area_id
              INTO author, current_status, document, robot_area
              FROM robot_versions v
              JOIN robots r ON r.company_id = v.company_id AND r.id = v.robot_id
             WHERE v.company_id = NEW.company_id
               AND v.robot_id = NEW.robot_id
               AND v.version_no = NEW.version_no;
            IF author IS NULL THEN
                -- Versão de outra empresa (invisível) ou que não existe: o isolamento por
                -- linha e a chave estrangeira recusam a inclusão logo adiante.
                RETURN NEW;
            END IF;
            IF author = NEW.approved_by THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'quem criou a versão não pode aprová-la';
            END IF;
            IF current_status <> 'validated' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'só uma versão validada recebe aprovações';
            END IF;
            SELECT array_agg(u.subject) INTO custodians
              FROM credentials c
              JOIN users u ON u.company_id = c.company_id AND u.id = c.custodian_user_id
             WHERE c.company_id = NEW.company_id
               AND c.area_id = robot_area
               AND c.kind = 'pfx_a1'
               AND c.name IN (
                   SELECT credential ->> 'name'
                     FROM jsonb_array_elements(
                         CASE WHEN jsonb_typeof(document -> 'credentials') = 'array'
                              THEN document -> 'credentials'
                              ELSE '[]'::jsonb END
                     ) AS credential
                    WHERE credential ->> 'kind' = 'pfx_a1'
               );
            IF NEW.approved_by = ANY (custodians) THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'o custodiante de um certificado A1 do robô não aprova a versão';
            END IF;
            IF author = ANY (custodians) THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'a versão foi criada pelo custodiante de um certificado A1 do robô'
                        || ' e não recebe aprovações';
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER version_approvals_check
            BEFORE INSERT ON version_approvals
            FOR EACH ROW EXECUTE FUNCTION rapa_version_approvals_check()
        """,
        # Publicar exige as aprovações que o manifesto pede (duas com certificado A1).
        # Uma versão nova ainda não tem aprovações (a chave estrangeira de
        # version_approvals pede a versão antes): ela nasce rascunho e sem aprovador,
        # senão bastaria incluir a versão já publicada para pular as aprovações.
        """
        CREATE FUNCTION rapa_robot_versions_approvals() RETURNS trigger
            LANGUAGE plpgsql AS $$
        DECLARE
            required integer;
            given integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status IS DISTINCT FROM 'draft' OR NEW.approved_by IS NOT NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'uma versão nova nasce como rascunho, sem aprovador';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status = 'published' AND OLD.status IS DISTINCT FROM 'published' THEN
                required := CASE WHEN EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(
                          CASE WHEN jsonb_typeof(NEW.manifest -> 'credentials') = 'array'
                               THEN NEW.manifest -> 'credentials'
                               ELSE '[]'::jsonb END
                      ) AS credential
                     WHERE credential ->> 'kind' = 'pfx_a1'
                ) THEN 2 ELSE 1 END;
                SELECT count(*) INTO given
                  FROM version_approvals a
                 WHERE a.company_id = NEW.company_id
                   AND a.robot_id = NEW.robot_id
                   AND a.version_no = NEW.version_no;
                IF given < required THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = format(
                            'a versão precisa de %s aprovações para ser publicada e tem %s',
                            required, given);
                END IF;
                IF NEW.approved_by IS NULL OR NOT EXISTS (
                    SELECT 1 FROM version_approvals a
                     WHERE a.company_id = NEW.company_id
                       AND a.robot_id = NEW.robot_id
                       AND a.version_no = NEW.version_no
                       AND a.approved_by = NEW.approved_by
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'quem publica a versão precisa estar entre os aprovadores';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER robot_versions_approvals
            BEFORE INSERT OR UPDATE ON robot_versions
            FOR EACH ROW EXECUTE FUNCTION rapa_robot_versions_approvals()
        """,
    ]


def _comments() -> list[str]:
    return [
        "COMMENT ON COLUMN credentials.not_after IS"
        " 'Validade do certificado A1; sem ela, o serviço trata o certificado como vencido'",
        "COMMENT ON COLUMN credentials.fingerprint_sha256 IS"
        " 'Impressão SHA-256 do certificado (64 hexadecimais minúsculos)'",
        "COMMENT ON TABLE a1_authorizations IS"
        " 'Autorizações do custodiante por robô e certificado A1; só inclusões e revogação'",
        "COMMENT ON TABLE version_approvals IS"
        " 'Aprovações de cada versão (duas com certificado A1); só inclusões'",
        "COMMENT ON TABLE credential_alerts IS"
        " 'Avisos de vencimento dos certificados A1, um por credencial, dia e tipo'",
    ]


def upgrade() -> None:
    roles = _roles()
    for statement in (
        *_credentials(),
        *_tables(),
        *_backfill(),
        *_row_level_security(),
        *_triggers(),
        *_comments(),
        *a1_revokes_sql(roles),
        *a1_grants_sql(roles),
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS credential_alerts, version_approvals, a1_authorizations CASCADE",
        "DROP TRIGGER IF EXISTS robot_versions_approvals ON robot_versions",
        "DROP TRIGGER IF EXISTS credentials_guard ON credentials",
        "DROP FUNCTION IF EXISTS rapa_robot_versions_approvals(), rapa_version_approvals_check(),"
        " rapa_a1_authorizations_guard(), rapa_credentials_guard()",
        "DROP INDEX IF EXISTS credentials_a1_not_after_idx",
        "ALTER TABLE credentials DROP CONSTRAINT IF EXISTS credentials_company_area_id_key,"
        " DROP CONSTRAINT IF EXISTS credentials_company_id_key,"
        " DROP CONSTRAINT IF EXISTS credentials_a1_not_after,"
        " DROP CONSTRAINT IF EXISTS credentials_revoked_pair",
        "ALTER TABLE credentials DROP COLUMN IF EXISTS updated_at,"
        " DROP COLUMN IF EXISTS revoked_by, DROP COLUMN IF EXISTS revoked_at,"
        " DROP COLUMN IF EXISTS fingerprint_sha256, DROP COLUMN IF EXISTS not_after",
        *a1_restore_sql(_roles()),
    ):
        op.execute(statement)
