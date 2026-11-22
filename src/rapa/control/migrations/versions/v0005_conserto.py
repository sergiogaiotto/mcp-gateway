"""Consertar: retratos das falhas e consertos pedidos.

Revisão: 0005
Anterior: 0004

Duas tabelas, as duas com a empresa liderando as chaves, isolamento por linha
(ENABLE e FORCE) com a política de sempre, e só inclusões:

- ``run_snapshots``: o retrato da página no passo que quebrou
  (``rapa.core.snapshot.PageSnapshot``), um por tentativa de execução, gravado pelo
  despachante junto com o resultado, só quando a execução falhou porque o alvo de um
  passo não foi encontrado (ou foi encontrado mais de uma vez). O retrato nunca tem
  valores de campos nem segredos (o executor os tira) e tem no máximo 1 MiB.
- ``repair_proposals``: cada conserto pedido, no máximo um por execução: o passo, o
  candidato escolhido pela pessoa (com o alvo novo), o que o modelo sugeriu, quem
  pediu e o rascunho criado com o passo consertado. A versão nova sempre é um
  rascunho de número maior que o da execução; quem publica é outra pessoa, com o
  papel de aprovação (a regra de sempre de ``robot_versions``).

O despachante só inclui retratos; a aplicação lê os retratos e lê e inclui os
consertos. Nada é apagado pela aplicação.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, repair_grants_sql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"
_TABLES = ("run_snapshots", "repair_proposals")
_STEP_ID = "'^[a-z][a-z0-9_]{0,39}$'"
_CANDIDATE_ID = "'^c[0-9]{1,3}$'"
_SHA256 = "'^[0-9a-f]{64}$'"


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _tables() -> list[str]:
    return [
        f"""
        CREATE TABLE run_snapshots (
            company_id uuid NOT NULL REFERENCES companies (id),
            run_id uuid NOT NULL,
            attempt integer NOT NULL CHECK (attempt >= 1),
            step_id text NOT NULL CHECK (step_id ~ {_STEP_ID}),
            error_code text NOT NULL CHECK (error_code ~ '^[a-z_]{{1,100}}$'),
            snapshot jsonb NOT NULL
                CHECK (jsonb_typeof(snapshot) = 'object' AND octet_length(snapshot::text) <= 1048576),
            snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ {_SHA256}),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, run_id, attempt),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id)
        )
        """,
        f"""
        CREATE TABLE repair_proposals (
            company_id uuid NOT NULL REFERENCES companies (id),
            id uuid NOT NULL,
            run_id uuid NOT NULL,
            robot_id uuid NOT NULL,
            step_id text NOT NULL CHECK (step_id ~ {_STEP_ID}),
            base_version_id uuid NOT NULL,
            base_version_no integer NOT NULL CHECK (base_version_no >= 1),
            version_id uuid NOT NULL,
            version_no integer NOT NULL,
            candidate_id text NOT NULL CHECK (candidate_id ~ {_CANDIDATE_ID}),
            candidate jsonb NOT NULL CHECK (jsonb_typeof(candidate) = 'object'),
            model_candidate_id text
                CHECK (model_candidate_id IS NULL OR model_candidate_id ~ {_CANDIDATE_ID}),
            snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ {_SHA256}),
            requested_by text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 255),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, id),
            UNIQUE (company_id, run_id),
            UNIQUE (company_id, version_id),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id),
            FOREIGN KEY (company_id, robot_id) REFERENCES robots (company_id, id),
            FOREIGN KEY (company_id, robot_id, base_version_id)
                REFERENCES robot_versions (company_id, robot_id, id),
            FOREIGN KEY (company_id, robot_id, version_id)
                REFERENCES robot_versions (company_id, robot_id, id),
            CHECK (version_no > base_version_no),
            CHECK (version_id <> base_version_id)
        )
        """,
        """
        CREATE INDEX repair_proposals_robot
            ON repair_proposals (company_id, robot_id, created_at DESC)
        """,
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
            f"""
            CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
            """,
        ]
    return statements


def _comments() -> list[str]:
    return [
        "COMMENT ON TABLE run_snapshots IS"
        " 'Retrato da página no passo que quebrou (sem valores nem segredos); só inclusões'",
        "COMMENT ON TABLE repair_proposals IS"
        " 'Consertos pedidos, um por execução, com o rascunho criado; só inclusões'",
    ]


def upgrade() -> None:
    for statement in (
        *_tables(),
        *_row_level_security(),
        *_comments(),
        *repair_grants_sql(_roles()),
    ):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS repair_proposals, run_snapshots CASCADE")
