"""Início do primeiro passo que grava: ``runs.write_started_at``.

Revisão: 0002
Anterior: 0001

Nada que gravou é repetido automaticamente. O despachante marca
``write_started_at`` ao gravar o início do primeiro passo que grava (o evento
``step_started`` com ``"grava": true`` ou o aviso de passo irreversível), que o
executor espera ver confirmado antes de agir. Uma execução marcada nunca volta
para a fila: se perder o executor, falha (ou fica indeterminada, depois de um
passo irreversível), em vez de recomeçar do primeiro passo e gravar de novo.

Como para ``irreversible_started_at``, o banco também garante a regra: uma
restrição impede a marca numa execução na fila, e uma trava impede apagar ou
trocar a marca e devolver à fila uma execução marcada.

As execuções que já existem são preenchidas a partir dos eventos gravados (na
dúvida, um passo de efeito ``write`` conta como gravação, com ou sem o campo
``grava``) e de ``irreversible_started_at``; as que estão na fila não gravaram
nada. As tabelas têm ``FORCE ROW LEVEL SECURITY``, que vale também para o dono:
para o preenchimento enxergar todas as empresas, o isolamento forçado é
suspenso só dentro da transação desta migração e volta antes do fim dela.

As permissões não mudam: as dos papéis são por tabela e já valem para a coluna
nova.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in (
        "ALTER TABLE runs ADD COLUMN write_started_at timestamptz",
        # -- preenchimento das execuções que já existem ------------------------
        "ALTER TABLE runs NO FORCE ROW LEVEL SECURITY",
        "ALTER TABLE run_events NO FORCE ROW LEVEL SECURITY",
        # O início de um passo que grava, como o executor o anuncia (ver
        # rapa.runner.ports.waits_for_ack), mais o efeito "write" dos executores
        # anteriores ao campo "grava". O passo irreversível anunciado sem o aviso
        # falhou antes de agir e não conta.
        """
        UPDATE runs r
           SET write_started_at = w.first_at
          FROM (
                SELECT e.company_id, e.run_id, min(e.at) AS first_at
                  FROM run_events e
                 WHERE e.kind = 'step_started'
                   AND (
                       e.data -> 'grava' = 'true'::jsonb
                       OR e.data -> 'irreversivel' = 'true'::jsonb
                       OR e.data ->> 'efeito' = 'write'
                   )
                 GROUP BY e.company_id, e.run_id
               ) w
         WHERE w.company_id = r.company_id AND w.run_id = r.id AND r.state <> 'queued'
        """,
        # O passo irreversível também grava (least ignora o valor nulo).
        """
        UPDATE runs
           SET write_started_at = least(write_started_at, irreversible_started_at)
         WHERE irreversible_started_at IS NOT NULL AND state <> 'queued'
        """,
        "ALTER TABLE run_events FORCE ROW LEVEL SECURITY",
        "ALTER TABLE runs FORCE ROW LEVEL SECURITY",
        # -- a regra no banco ----------------------------------------------------
        """
        ALTER TABLE runs ADD CONSTRAINT runs_write_started_not_queued
            CHECK (state <> 'queued' OR write_started_at IS NULL)
        """,
        """
        CREATE FUNCTION rapa_runs_write_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.write_started_at IS NOT NULL THEN
                IF NEW.write_started_at IS DISTINCT FROM OLD.write_started_at THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'o início do primeiro passo que grava de uma execução não pode ser alterado';
                END IF;
                IF NEW.state = 'queued' THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation',
                        MESSAGE = 'uma execução que já começou um passo que grava não pode voltar para a fila';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """,
        """
        CREATE TRIGGER runs_write_guard
            BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION rapa_runs_write_guard()
        """,
        "COMMENT ON COLUMN runs.write_started_at IS"
        " 'Início do primeiro passo que grava: a execução não volta mais para a fila'",
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TRIGGER IF EXISTS runs_write_guard ON runs",
        "DROP FUNCTION IF EXISTS rapa_runs_write_guard()",
        "ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_write_started_not_queued",
        "ALTER TABLE runs DROP COLUMN IF EXISTS write_started_at",
    ):
        op.execute(statement)
