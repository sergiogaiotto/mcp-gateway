"""Retenção: artefatos que expiram, retratos e chaves vencidas apagados, eventos antigos.

Revisão: 0008
Anterior: 0005 (durante o desenvolvimento; na integração, a 0007)

- ``runs.artifacts_expired_at``: quando os arquivos dos artefatos da execução foram
  todos apagados pela retenção (prazo das evidências). ``runs.captures_expired_at``:
  quando as telas e o vídeo de uma execução que deu certo (ou foi cancelada) foram
  apagados (prazo curto); os arquivos baixados por ela ficam até a outra marca. As
  marcas são as únicas colunas que a retenção muda: marcar não muda mais nada na
  execução, uma marca nunca é desfeita e, depois da primeira, a lista de artefatos
  (nomes, tamanhos e hashes) não muda (trava ``runs_artifacts_guard``). Só uma
  execução concluída pode ter marca. Dois índices parciais acham as execuções
  vencidas de cada prazo sem ler as já marcadas.
- O despachante não ganha ``DELETE`` em nenhuma tabela. As exclusões são funções
  ``SECURITY DEFINER`` (donas: o dono das tabelas; ``EXECUTE`` só para o papel do
  despachante; ``search_path`` fixo) que recebem só a idade e o tamanho do lote,
  nunca uma lista de identificadores, como ``rapa_ensure_event_partitions``:

  - ``rapa_purge_run_snapshots(dias, lote)`` apaga os retratos das falhas gravados
    há mais de ``dias`` (no mínimo 1) e devolve a empresa, a execução e a
    tentativa de cada um (para a auditoria);
  - ``rapa_purge_idempotency_keys(lote)`` apaga chaves de idempotência vencidas;
  - ``rapa_drop_event_partitions(dias)`` remove as partições mensais de
    ``run_events`` que terminaram há mais de ``dias`` (no mínimo 1).

- As funções rodam como o dono, que também está sujeito ao isolamento por linha
  (``FORCE``). Cada uma marca ``rapa.retention`` (``SET LOCAL`` logo antes de apagar,
  tirada logo depois, também numa falha) e só enxerga as linhas de todas as empresas
  por políticas ``retention_sweep_*`` restritas ao papel dono e a essa marca, só para
  ler e apagar (nunca para incluir ou mudar). Marcar ``rapa.retention`` noutro papel
  não abre nada: as políticas e a trava são só do dono.
- ``run_snapshots`` continua só de inclusões, mas a trava aceita a exclusão feita
  pela função (o dono, com a marca ``run_snapshots``) de um retrato com mais de um
  dia. A trava fica com o mesmo nome (``run_snapshots_append_only``) e passa a usar a
  função própria ``rapa_run_snapshots_append_only``.

As execuções que já existem perdem o caminho do disco: ``runs.artifacts`` fica sem
``path`` e a saída de um download (a referência inteira do artefato) vira o nome dele.

O ``downgrade`` volta tudo atrás: tira as políticas, as funções, a trava nova (a de
antes volta), os índices e as colunas. Os caminhos tirados não voltam (não ficam
guardados em lugar nenhum), e o código de antes não precisa deles.
"""

from collections.abc import Sequence

from alembic import op

from rapa.control.db.roles import RoleNames, retention_grants_sql

revision: str = "0008"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tabelas que as funções da retenção apagam, cada uma com a marca da sua função.
_SWEPT = ("run_snapshots", "idempotency_keys")
_FUNCTIONS = (
    "rapa_purge_run_snapshots(integer, integer)",
    "rapa_purge_idempotency_keys(integer)",
    "rapa_drop_event_partitions(integer)",
)


def _roles() -> RoleNames:
    config = op.get_context().config
    configured = config.attributes.get("rapa_roles") if config is not None else None
    if isinstance(configured, RoleNames):
        return configured
    return RoleNames.from_env()


def _paths_out() -> list[str]:
    """Tira o caminho do disco das execuções que já existem (artefatos e saídas)."""
    return [
        # Como na 0002: o isolamento forçado vale para o dono; ele é suspenso só dentro
        # da transação desta migração, para a limpeza enxergar todas as empresas.
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY",
        """
        UPDATE runs r
           SET artifacts = (
               SELECT coalesce(
                   jsonb_agg(
                       CASE WHEN jsonb_typeof(a.item) = 'object' THEN a.item - 'path' ELSE a.item END
                       ORDER BY a.position
                   ),
                   '[]'::jsonb
               )
                 FROM jsonb_array_elements(r.artifacts) WITH ORDINALITY AS a(item, position)
           )
         WHERE jsonb_path_exists(r.artifacts, '$[*].path')
        """,
        # A saída de um download era a referência inteira do artefato (com o caminho):
        # vira o nome dele, o identificador que a API usa.
        """
        UPDATE runs r
           SET outputs = (
               SELECT jsonb_object_agg(
                   o.key,
                   CASE
                       WHEN jsonb_typeof(o.value) = 'object'
                            AND o.value ?& ARRAY['kind', 'name', 'path', 'sha256']
                       THEN o.value -> 'name'
                       ELSE o.value
                   END
               )
                 FROM jsonb_each(r.outputs) AS o
           )
         WHERE jsonb_typeof(r.outputs) = 'object'
           AND EXISTS (
               SELECT 1 FROM jsonb_each(r.outputs) AS o
                WHERE jsonb_typeof(o.value) = 'object'
                  AND o.value ?& ARRAY['kind', 'name', 'path', 'sha256']
           )
        """,
        "ALTER TABLE runs FORCE ROW LEVEL SECURITY",
    ]


def _runs() -> list[str]:
    return [
        "ALTER TABLE runs ADD COLUMN artifacts_expired_at timestamptz",
        "ALTER TABLE runs ADD COLUMN captures_expired_at timestamptz",
        """
        ALTER TABLE runs ADD CONSTRAINT runs_artifacts_expired_after_finish
            CHECK (artifacts_expired_at IS NULL OR finished_at IS NOT NULL)
        """,
        """
        ALTER TABLE runs ADD CONSTRAINT runs_captures_expired_after_finish
            CHECK (captures_expired_at IS NULL OR finished_at IS NOT NULL)
        """,
        # O prazo curto: as telas e o vídeo das execuções que deram certo (ou foram canceladas).
        """
        CREATE INDEX runs_captures_due ON runs (finished_at, id)
            WHERE captures_expired_at IS NULL AND artifacts_expired_at IS NULL
              AND state IN ('succeeded', 'cancelled')
        """,
        # O prazo das evidências: tudo, de toda execução concluída.
        """
        CREATE INDEX runs_artifacts_due ON runs (finished_at, id)
            WHERE artifacts_expired_at IS NULL
              AND state IN ('succeeded', 'cancelled', 'failed', 'indeterminate')
        """,
        """
        CREATE FUNCTION rapa_runs_artifacts_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        DECLARE
            unchanged runs;
        BEGIN
            IF (OLD.artifacts_expired_at IS NOT NULL
                    AND NEW.artifacts_expired_at IS DISTINCT FROM OLD.artifacts_expired_at)
                OR (OLD.captures_expired_at IS NOT NULL
                    AND NEW.captures_expired_at IS DISTINCT FROM OLD.captures_expired_at)
                OR ((OLD.artifacts_expired_at IS NOT NULL OR OLD.captures_expired_at IS NOT NULL)
                    AND NEW.artifacts IS DISTINCT FROM OLD.artifacts)
            THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation',
                    MESSAGE = 'os artefatos expirados de uma execução não voltam nem mudam';
            END IF;
            IF NEW.artifacts_expired_at IS DISTINCT FROM OLD.artifacts_expired_at
                OR NEW.captures_expired_at IS DISTINCT FROM OLD.captures_expired_at
            THEN
                unchanged := NEW;
                unchanged.artifacts_expired_at := OLD.artifacts_expired_at;
                unchanged.captures_expired_at := OLD.captures_expired_at;
                IF unchanged IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'marcar os artefatos como expirados não muda mais nada na execução';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER runs_artifacts_guard
            BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION rapa_runs_artifacts_guard()
        """,
        "COMMENT ON COLUMN runs.artifacts_expired_at IS"
        " 'Quando a retenção apagou todos os arquivos dos artefatos (prazo das evidências)'",
        "COMMENT ON COLUMN runs.captures_expired_at IS"
        " 'Quando a retenção apagou as telas e o vídeo de uma execução que deu certo"
        " (os arquivos baixados ficam)'",
    ]


def _sweep_policies() -> list[str]:
    statements: list[str] = []
    for table in _SWEPT:
        marker = f"current_setting('rapa.retention', true) = '{table}'"
        statements += [
            f"""
            CREATE POLICY retention_sweep_read ON {table}
                FOR SELECT TO CURRENT_USER USING ({marker})
            """,
            f"""
            CREATE POLICY retention_sweep_delete ON {table}
                FOR DELETE TO CURRENT_USER USING ({marker})
            """,
        ]
    return statements


def _snapshots() -> list[str]:
    return [
        "CREATE INDEX run_snapshots_created ON run_snapshots (created_at)",
        # A trava continua recusando tudo, menos a exclusão feita pela função da
        # retenção: o dono das tabelas, com a marca dela, num retrato com mais de um dia.
        """
        CREATE FUNCTION rapa_run_snapshots_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE'
                AND current_setting('rapa.retention', true) = 'run_snapshots'
                AND current_user = (
                    SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = TG_RELID
                )
                AND OLD.created_at < now() - interval '1 day'
            THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION USING ERRCODE = 'check_violation',
                MESSAGE = format('a tabela %s só aceita inclusões', TG_TABLE_NAME);
        END
        $$
        """,
        "DROP TRIGGER run_snapshots_append_only ON run_snapshots",
        """
        CREATE TRIGGER run_snapshots_append_only
            BEFORE UPDATE OR DELETE ON run_snapshots
            FOR EACH ROW EXECUTE FUNCTION rapa_run_snapshots_append_only()
        """,
    ]


def _functions() -> list[str]:
    # Cada função confere os parâmetros, marca rapa.retention (SET LOCAL) logo antes de
    # apagar e tira a marca logo depois, também numa falha no meio. A marca só vale para o
    # dono: as políticas retention_sweep_* e a trava dos retratos conferem o papel.
    return [
        """
        CREATE FUNCTION rapa_purge_run_snapshots(older_than_days integer, batch_size integer)
            RETURNS TABLE (company_id uuid, run_id uuid, attempt integer)
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, public
        AS $$
        #variable_conflict use_column
        BEGIN
            IF older_than_days IS NULL OR older_than_days < 1 OR older_than_days > 36500 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'older_than_days deve estar entre 1 e 36500';
            END IF;
            IF batch_size IS NULL OR batch_size < 1 OR batch_size > 1000 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'batch_size deve estar entre 1 e 1000';
            END IF;
            PERFORM set_config('rapa.retention', 'run_snapshots', true);
            BEGIN
                RETURN QUERY
                WITH gone AS (
                    DELETE FROM public.run_snapshots AS s
                     WHERE (s.company_id, s.run_id, s.attempt) IN (
                           SELECT o.company_id, o.run_id, o.attempt
                             FROM public.run_snapshots AS o
                            WHERE o.created_at < now() - make_interval(days => older_than_days)
                            ORDER BY o.created_at
                            LIMIT batch_size)
                    RETURNING s.company_id, s.run_id, s.attempt
                )
                SELECT g.company_id, g.run_id, g.attempt FROM gone AS g;
            EXCEPTION WHEN OTHERS THEN
                PERFORM set_config('rapa.retention', '', true);
                RAISE;
            END;
            PERFORM set_config('rapa.retention', '', true);
        END
        $$
        """,
        """
        CREATE FUNCTION rapa_purge_idempotency_keys(batch_size integer) RETURNS integer
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, public
        AS $$
        DECLARE
            deleted integer;
        BEGIN
            IF batch_size IS NULL OR batch_size < 1 OR batch_size > 1000 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'batch_size deve estar entre 1 e 1000';
            END IF;
            PERFORM set_config('rapa.retention', 'idempotency_keys', true);
            BEGIN
                DELETE FROM public.idempotency_keys AS k
                 WHERE (k.company_id, k.scope, k.key) IN (
                       SELECT o.company_id, o.scope, o.key
                         FROM public.idempotency_keys AS o
                        WHERE o.expires_at < now()
                        ORDER BY o.expires_at
                        LIMIT batch_size);
                GET DIAGNOSTICS deleted = ROW_COUNT;
            EXCEPTION WHEN OTHERS THEN
                PERFORM set_config('rapa.retention', '', true);
                RAISE;
            END;
            PERFORM set_config('rapa.retention', '', true);
            RETURN deleted;
        END
        $$
        """,
        # Os nomes das partições são os de rapa_ensure_event_partitions
        # (run_events_AAAA_MM, de um mês cada, em UTC); a mesma trava consultiva dela.
        """
        CREATE FUNCTION rapa_drop_event_partitions(older_than_days integer) RETURNS integer
            LANGUAGE plpgsql SECURITY DEFINER
            SET search_path = pg_catalog, public
            SET timezone = 'UTC'
        AS $$
        DECLARE
            part text;
            month_end timestamptz;
            dropped integer := 0;
        BEGIN
            IF older_than_days IS NULL OR older_than_days < 1 OR older_than_days > 36500 THEN
                RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
                    MESSAGE = 'older_than_days deve estar entre 1 e 36500';
            END IF;
            PERFORM pg_advisory_xact_lock(7103, 0);
            FOR part IN
                SELECT c.relname
                  FROM pg_inherits i
                  JOIN pg_class c ON c.oid = i.inhrelid
                 WHERE i.inhparent = 'public.run_events'::regclass
                   AND c.relname ~ '^run_events_[0-9]{4}_[0-9]{2}$'
                 ORDER BY c.relname
            LOOP
                month_end := to_timestamp(substr(part, 12, 7), 'YYYY_MM') + interval '1 month';
                IF month_end <= now() - make_interval(days => older_than_days) THEN
                    EXECUTE format('DROP TABLE public.%I', part);
                    dropped := dropped + 1;
                END IF;
            END LOOP;
            RETURN dropped;
        END
        $$
        """,
        *(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC" for function in _FUNCTIONS),
    ]


def upgrade() -> None:
    for statement in (
        *_paths_out(),
        *_runs(),
        *_sweep_policies(),
        *_snapshots(),
        *_functions(),
        *retention_grants_sql(_roles()),
    ):
        op.execute(statement)


def downgrade() -> None:
    statements = [f"DROP FUNCTION IF EXISTS {function}" for function in _FUNCTIONS]
    for table in _SWEPT:
        statements += [
            f"DROP POLICY IF EXISTS retention_sweep_read ON {table}",
            f"DROP POLICY IF EXISTS retention_sweep_delete ON {table}",
        ]
    statements += [
        "DROP TRIGGER run_snapshots_append_only ON run_snapshots",
        """
        CREATE TRIGGER run_snapshots_append_only
            BEFORE UPDATE OR DELETE ON run_snapshots
            FOR EACH ROW EXECUTE FUNCTION rapa_append_only()
        """,
        "DROP FUNCTION IF EXISTS rapa_run_snapshots_append_only()",
        "DROP INDEX IF EXISTS run_snapshots_created",
        "DROP TRIGGER IF EXISTS runs_artifacts_guard ON runs",
        "DROP FUNCTION IF EXISTS rapa_runs_artifacts_guard()",
        "DROP INDEX IF EXISTS runs_captures_due, runs_artifacts_due",
        "ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_captures_expired_after_finish",
        "ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_artifacts_expired_after_finish",
        "ALTER TABLE runs DROP COLUMN IF EXISTS captures_expired_at",
        "ALTER TABLE runs DROP COLUMN IF EXISTS artifacts_expired_at",
    ]
    for statement in statements:
        op.execute(statement)
