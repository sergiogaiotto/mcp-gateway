"""Partes sem banco: regras de admissão, mensagens de validação, URLs e papéis."""

from decimal import Decimal
from uuid import UUID

import pytest

from rapa.control.db.roles import RoleNames, grants_sql
from rapa.control.db.session import psycopg_url
from rapa.control.services._common import INTERACTIVE_PRIORITY, PRIORITY_BY_TRIGGER
from rapa.control.services.admission import Capacity, CompanyLoad, plan
from rapa.control.services.audit import payload_digest
from rapa.control.services.dispatcher import failure_result
from rapa.control.services.inputs import validate_inputs
from rapa.control.services.robots import fold_text
from rapa.core.contracts import RunMode, RunnerJob, RunTrigger
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.robots import baixar_notas, emitir_protocolo

A = UUID(int=1)
B = UUID(int=2)
CAPACITY = Capacity(total_slots=10, interactive_reserve_ratio=Decimal("0.10"))


def test_capacity_reserve_rounds_up() -> None:
    assert CAPACITY.reserve == 1 and CAPACITY.general_pool == 9
    assert Capacity(30, Decimal("0.10")).reserve == 3
    assert Capacity(5, Decimal("0.10")).reserve == 1
    assert Capacity(5, Decimal(0)).reserve == 0


def test_cap_is_guarantee_times_borrow_factor() -> None:
    assert CompanyLoad(0, 0, 4, Decimal(2)).cap == 8
    assert CompanyLoad(0, 0, 3, Decimal("1.5")).cap == 4
    assert CompanyLoad(0, 0, 0, Decimal(2)).cap == 0


def test_within_guarantee_and_borrowing() -> None:
    loads = {
        A: CompanyLoad(running=4, queued=10, guaranteed_slots=4, max_borrow_factor=Decimal(2)),
        B: CompanyLoad(running=0, queued=10, guaranteed_slots=4, max_borrow_factor=Decimal(2)),
    }
    admission = plan(CAPACITY, loads, interactive_running=0)
    assert admission.within_guarantee == {B}
    # Livres: 9 − 4 = 5; B ainda precisa de 4; sobra 1 vaga ociosa para A.
    assert admission.borrowing == {A}
    loads[B] = CompanyLoad(running=1, queued=10, guaranteed_slots=4, max_borrow_factor=Decimal(2))
    admission = plan(CAPACITY, loads, interactive_running=0)
    # Livres: 4; B ainda precisa de 3; sobra 1.
    assert admission.borrowing == {A}
    loads[B] = CompanyLoad(running=0, queued=10, guaranteed_slots=5, max_borrow_factor=Decimal(2))
    assert plan(CAPACITY, loads, interactive_running=0).borrowing == frozenset()


def test_no_borrowing_beyond_cap_or_without_queue() -> None:
    loads = {
        A: CompanyLoad(running=8, queued=5, guaranteed_slots=4, max_borrow_factor=Decimal(2)),
        B: CompanyLoad(running=0, queued=0, guaranteed_slots=4, max_borrow_factor=Decimal(2)),
    }
    admission = plan(CAPACITY, loads, interactive_running=0)
    assert admission.within_guarantee == frozenset()
    assert admission.borrowing == frozenset()
    assert admission.interactive


def test_interactive_reserve_and_overflow() -> None:
    full = {A: CompanyLoad(running=9, queued=5, guaranteed_slots=20)}
    admission = plan(CAPACITY, full, interactive_running=0)
    assert admission.interactive and not admission.within_guarantee
    assert not plan(CAPACITY, full, interactive_running=1).interactive
    # Dois interativos: um na reserva, outro nas vagas gerais.
    some = {A: CompanyLoad(running=7, queued=5, guaranteed_slots=20)}
    assert plan(CAPACITY, some, interactive_running=2).within_guarantee == {A}
    more = {A: CompanyLoad(running=8, queued=5, guaranteed_slots=20)}
    assert plan(CAPACITY, more, interactive_running=2).within_guarantee == frozenset()
    assert not plan(CAPACITY, more, interactive_running=2).anything


def test_interactive_overflow_never_takes_another_company_guarantee() -> None:
    # Reserva cheia: um interativo além dela só usa vaga que nenhuma outra empresa
    # precise para a própria garantia.
    loads = {B: CompanyLoad(running=0, queued=9, guaranteed_slots=9)}
    admission = plan(CAPACITY, loads, interactive_running=1, interactive_waiting={A, B})
    assert not admission.interactive
    # Livres: 9, todas da garantia da B: só a própria B pode usá-las com um interativo.
    assert admission.interactive_overflow == {B}
    assert admission.within_guarantee == {B}
    loads = {B: CompanyLoad(running=0, queued=3, guaranteed_slots=4)}
    # Livres: 9; B ainda precisa de 3; sobram 6 para o interativo da A.
    admission = plan(CAPACITY, loads, interactive_running=1, interactive_waiting={A})
    assert admission.interactive_overflow == {A} and admission.anything
    # Com vaga na reserva, qualquer interativo entra.
    admission = plan(CAPACITY, loads, interactive_running=0, interactive_waiting={A})
    assert admission.interactive
    # Sem vaga nenhuma, nenhum interativo entra.
    full = {B: CompanyLoad(running=9, queued=3, guaranteed_slots=9)}
    admission = plan(CAPACITY, full, interactive_running=1, interactive_waiting={A, B})
    assert not admission.interactive and admission.interactive_overflow == frozenset()
    assert not admission.anything


MANIFEST = emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test")


def test_validate_inputs_ok_and_errors() -> None:
    assert validate_inputs(MANIFEST, {"nome": "Ana", "cpf": "52998224725", "uf": "SP"}) == []
    errors = validate_inputs(MANIFEST, {"cpf": 5})
    assert "campo obrigatório ausente: nome" in errors
    assert "campo obrigatório ausente: uf" in errors
    assert "cpf: tipo inválido; esperado texto" in errors


def test_validate_inputs_lists_and_dates() -> None:
    manifest = Manifest.model_validate(
        {
            "name": "lancar_lote",
            "title": "Lançar lote",
            "description": "Teste de listas e datas.",
            "allowed_domains": ["portal-v1.rapa.test"],
            "inputs": [
                {"name": "dia", "title": "Dia", "type": "date"},
                {
                    "name": "itens",
                    "title": "Itens",
                    "type": "list",
                    "item_fields": [{"name": "numero", "title": "Número", "type": "integer"}],
                },
            ],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir",
                    "title": "Abrir",
                    "url": "http://portal-v1.rapa.test:8080/",
                }
            ],
        }
    )
    assert validate_inputs(manifest, {"dia": "2026-11-18", "itens": [{"numero": 1}]}) == []
    errors = validate_inputs(manifest, {"dia": "2026-13-40", "itens": [{"numero": "x", "y": 1}]})
    assert "dia: formato inválido; esperado data no formato AAAA-MM-DD" in errors
    assert "campo não declarado: itens[0].y" in errors
    assert "itens[0].numero: tipo inválido; esperado número inteiro" in errors


def test_psycopg_url() -> None:
    assert psycopg_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert psycopg_url("postgres://u@h/db") == "postgresql+psycopg://u@h/db"
    assert psycopg_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    with pytest.raises(ValueError, match="não suportada"):
        psycopg_url("mysql://u@h/db")


def test_role_names_are_validated() -> None:
    assert RoleNames().app == "rapa_app"
    with pytest.raises(ValueError, match="inválido"):
        RoleNames(app='x"; DROP TABLE runs; --')
    with pytest.raises(ValueError, match="diferentes"):
        RoleNames(app="mesmo", dispatcher="mesmo")
    blocks = grants_sql(RoleNames(app="app_x", dispatcher="disp_x"))
    assert "'app_x'" in blocks[0] and '"app_x"' in blocks[0]
    assert "rapa_ensure_event_partitions" in blocks[1]
    assert "UPDATE (status, approved_by) ON robot_versions" in blocks[0]


def test_role_names_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPA_DB_APP_ROLE", "app_y")
    monkeypatch.setenv("RAPA_DB_DISPATCHER_ROLE", "disp_y")
    assert RoleNames.from_env() == RoleNames(app="app_y", dispatcher="disp_y")


def test_fold_and_digest() -> None:
    assert fold_text("EMISSÃO de Protocolo_Ágil") == "emissao de protocolo agil"
    assert payload_digest({"b": 1, "a": "é"}) == payload_digest({"a": "é", "b": 1})


def test_failure_result_is_conservative_for_irreversible_robots() -> None:
    def job(manifest: Manifest, mode: RunMode) -> RunnerJob:
        return RunnerJob(
            run_id=new_id(),
            company_id=new_id(),
            attempt=1,
            manifest=manifest,
            inputs={},
            mode=mode,
            deadline="2030-01-01T00:00:00Z",  # type: ignore[arg-type]
        )

    reading = baixar_notas("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test")
    assert failure_result(job(reading, RunMode.FULL), "x", "m")["state"] == "failed"
    assert failure_result(job(MANIFEST, RunMode.FULL), "x", "m")["state"] == "indeterminate"
    assert failure_result(job(MANIFEST, RunMode.UNTIL_FIRST_WRITE), "x", "m")["state"] == "failed"
    # Executor que nem chegou a ser iniciado: nada pode ter rodado.
    assert failure_result(job(MANIFEST, RunMode.FULL), "x", "m", started=False)["state"] == "failed"


def test_priority_by_trigger() -> None:
    # Só validação (e, no futuro, gravação e controle ao vivo) usa a reserva
    # interativa; o botão "Executar" da interface roda em produção, como a API.
    interactive = {t for t, p in PRIORITY_BY_TRIGGER.items() if p == INTERACTIVE_PRIORITY}
    assert interactive == {RunTrigger.VALIDATION}
    assert PRIORITY_BY_TRIGGER[RunTrigger.MANUAL] == PRIORITY_BY_TRIGGER[RunTrigger.REST]
    assert set(PRIORITY_BY_TRIGGER) == set(RunTrigger)
