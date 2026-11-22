"""Cofre e chaves: as chaves das execuções e o caminho no cofre para o despachante.

Revisão: 0009
Anterior: 0008

- ``run_keys``: a chave de dados de cada execução com artefatos no store de objetos,
  embrulhada pela chave da empresa (``wrapped_key``, com a versão da chave da empresa em
  ``key_version``; :mod:`rapa.control.keys`). Uma por execução, com a empresa liderando
  a chave e isolamento por linha (ENABLE e FORCE). Só aceita inclusões (o despachante,
  ao registrar os artefatos); a aplicação só lê (para servir um artefato). Apagar a
  linha destrói a chave: o que sobrar dos objetos não abre mais.
- A trava ``run_keys_append_only`` (função ``rapa_run_keys_guard``) recusa toda
  mudança e toda exclusão, menos duas: a da retenção (o dono das tabelas, com a marca
  ``rapa.retention = 'run_keys'``, numa execução cujos artefatos já expiraram todos,
  ``runs.artifacts_expired_at``) e a da saída da empresa (o dono com
  ``rapa.purge_company`` da empresa da linha, :func:`rapa_purge_allowed` da revisão
  0007, como as outras travas).
- ``rapa_purge_run_keys(lote)`` (``SECURITY DEFINER``, dona: o dono das tabelas;
  ``EXECUTE`` só para o despachante; ``search_path`` fixo) apaga as chaves das
  execuções que já perderam todos os artefatos e devolve a empresa e a execução de cada
  uma (para a auditoria). Recebe só o tamanho do lote, como as funções da 0008, e pula a
  empresa congelada. Para enxergar todas as empresas, o dono tem as políticas
  ``retention_sweep_read`` e ``retention_sweep_delete`` em ``run_keys``,
  ``retention_sweep_read`` em ``runs`` (só para ler a marca de expirado), e a política
  de leitura de ``companies`` da 0008 passa a aceitar também a marca ``run_keys``.
- O despachante ganha a leitura de ``credentials.vault_path``: o caminho no cofre é uma
  referência, não um segredo, e o lançador precisa dele para buscar o segredo no
  OpenBao (:class:`rapa.control.vault.OpenBaoSecretSource`).

O ``downgrade`` volta tudo atrás (a tabela, a função, as políticas, a leitura de
``vault_path`` e a política de ``companies`` como na 0008). As chaves apagadas com a
tabela não voltam: os artefatos cifrados que dependiam delas não abrem mais.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, vault_grants_sql, vault_revoke_sql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "nullif(current_setting('rapa.company_id', true), '')::uuid"
_MARKER = "current_setting('rapa.retention', true) = 'run_keys'"
_FROZEN = (
    "EXISTS (SELECT 1 FROM public.companies AS c WHERE c.id = o.company_id AND c.status = 'frozen')"
)
_COMPANY_MARKERS_0008 = "'run_snapshots', 'idempotency_keys', 'run_events'"
_FUNCTION = "rapa_purge_run_keys(integer)"


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _companies_policy(markers: str) -> str:
    return f"""
        ALTER POLICY retention_sweep_read ON companies
            USING (current_setting('rapa.retention', true) IN ({markers}))
        """


def _table() -> list[str]:
    return [
        """
        CREATE TABLE run_keys (
            company_id uuid NOT NULL REFERENCES companies (id),
            run_id uuid NOT NULL,
            wrapped_key bytea NOT NULL CHECK (octet_length(wrapped_key) BETWEEN 16 AND 4096),
            key_version integer NOT NULL CHECK (key_version >= 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (company_id, run_id),
            FOREIGN KEY (company_id, run_id) REFERENCES runs (company_id, id)
        )
        """,
        "ALTER TABLE run_keys ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE run_keys FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON run_keys
            USING (company_id = {_TENANT}) WITH CHECK (company_id = {_TENANT})
        """,
        f"""
        CREATE FUNCTION rapa_run_keys_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                AND {_MARKER}
                AND current_user = (
                    SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID
                )
                AND EXISTS (
                    SELECT 1 FROM public.runs AS r
                     WHERE r.company_id = OLD.company_id AND r.id = OLD.run_id
                       AND r.artifacts_expired_at IS NOT NULL
                )
            THEN
                RETURN OLD;
            END IF;
            IF TG_OP = 'DELETE' AND public.rapa_purge_allowed(TG_RELID, OLD.company_id) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = format('a tabela %s só aceita inclusões', TG_TABLE_NAME);
        END
        $$
        """,  # noqa: S608 - só as constantes deste módulo
        """
        CREATE TRIGGER run_keys_append_only
            BEFORE UPDATE OR DELETE ON run_keys
            FOR EACH ROW EXECUTE FUNCTION rapa_run_keys_guard()
        """,
        "COMMENT ON TABLE run_keys IS"
        " 'Chave de dados de cada execução, embrulhada pela chave da empresa; só inclusões"
        " (sai pela retenção e pela saída da empresa)'",
    ]


def _policies() -> list[str]:
    return [
        f"""
        CREATE POLICY retention_sweep_read ON run_keys
            FOR SELECT TO CURRENT_USER USING ({_MARKER})
        """,
        f"""
        CREATE POLICY retention_sweep_delete ON run_keys
            FOR DELETE TO CURRENT_USER USING ({_MARKER})
        """,
        # A marca de expirado das execuções (só ler), para achar as chaves que já podem sair.
        f"""
        CREATE POLICY retention_sweep_read ON runs
            FOR SELECT TO CURRENT_USER USING ({_MARKER})
        """,
        _companies_policy(f"{_COMPANY_MARKERS_0008}, 'run_keys'"),
    ]


def _function() -> list[str]:
    return [
        f"""
        CREATE FUNCTION rapa_purge_run_keys(batch_size integer)
            RETURNS TABLE (company_id uuid, run_id uuid)
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, public
        AS $$
        #variable_conflict use_column
        BEGIN
            IF batch_size IS NULL OR batch_size < 1 OR batch_size > 1000 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'batch_size deve estar entre 1 e 1000';
            END IF;
            PERFORM set_config('rapa.retention', 'run_keys', true);
            BEGIN
                RETURN QUERY
                WITH gone AS (
                    DELETE FROM public.run_keys AS k
                     WHERE (k.company_id, k.run_id) IN (
                           SELECT o.company_id, o.run_id
                             FROM public.run_keys AS o
                             JOIN public.runs AS r
                               ON r.company_id = o.company_id AND r.id = o.run_id
                            WHERE r.artifacts_expired_at IS NOT NULL
                              AND NOT {_FROZEN}
                            ORDER BY r.artifacts_expired_at, o.run_id
                            LIMIT batch_size)
                    RETURNING k.company_id, k.run_id
                )
                SELECT g.company_id, g.run_id FROM gone AS g;
            EXCEPTION WHEN OTHERS THEN
                PERFORM set_config('rapa.retention', '', true);
                RAISE;
            END;
            PERFORM set_config('rapa.retention', '', true);
        END
        $$
        """,  # noqa: S608 - só as constantes deste módulo
        f"REVOKE ALL ON FUNCTION {_FUNCTION} FROM PUBLIC",
    ]


def upgrade() -> None:
    for statement in (*_table(), *_policies(), *_function(), *vault_grants_sql(_roles())):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        *vault_revoke_sql(_roles()),
        f"DROP FUNCTION IF EXISTS {_FUNCTION}",
        "DROP POLICY IF EXISTS retention_sweep_read ON runs",
        _companies_policy(_COMPANY_MARKERS_0008),
        "DROP TABLE IF EXISTS run_keys",
        "DROP FUNCTION IF EXISTS rapa_run_keys_guard()",
    ):
        op.execute(statement)
