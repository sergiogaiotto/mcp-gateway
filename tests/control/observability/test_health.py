"""Prontidão (``/saude/pronto``): só os nomes do que falhou, prazo curto e sem enxurrada."""

import asyncio
import logging
from dataclasses import dataclass

import pytest

from rapa.control.db import Database
from rapa.control.health import DispatcherCheck, Readiness
from rapa.server import build_readiness_checks

SECRET = "senha-do-banco-na-mensagem"


class _Check:
    def __init__(
        self,
        name: str,
        *,
        ready: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.passes = ready
        self.error = error
        self.delay = delay
        self.calls = 0

    async def ready(self) -> bool:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.passes


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_ready_when_every_check_passes() -> None:
    readiness = Readiness([_Check("banco"), _Check("cofre")])
    response = await readiness.endpoint()
    assert response.status_code == 200
    assert response.body == b'{"estado":"pronto"}'
    assert response.headers["cache-control"] == "no-store"


async def test_not_ready_names_only_what_failed(caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    readiness = Readiness(
        [
            _Check("banco", error=OSError(f"conexão recusada: {SECRET}")),
            _Check("despachante", ready=False),
            _Check("armazenamento"),
            _Check("cofre", delay=10.0),
        ],
        timeout_s=0.05,
        clock=clock,
    )
    with caplog.at_level(logging.WARNING, logger="rapa.control.saude"):
        response = await readiness.endpoint()
    assert response.status_code == 503
    # Na ordem das checagens; nada do tipo nem do texto do erro na resposta.
    assert response.body == b'{"estado":"nao_pronto","falhas":["banco","despachante","cofre"]}'
    assert SECRET.encode() not in response.body
    assert sorted(record.getMessage() for record in caplog.records) == [
        "a checagem de prontidão banco falhou: OSError",
        "a checagem de prontidão cofre falhou: tempo esgotado",
        "a checagem de prontidão despachante falhou",
    ]
    assert SECRET not in caplog.text


async def test_failures_and_recoveries_are_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    clock = _Clock()
    check = _Check("banco", error=OSError("fora"))
    readiness = Readiness([check], clock=clock)
    with caplog.at_level(logging.WARNING, logger="rapa.control.saude"):
        for _ in range(3):
            clock.now += 10
            assert await readiness.failures() == ["banco"]
        check.error = None
        for _ in range(2):
            clock.now += 10
            assert await readiness.failures() == []
    assert [record.getMessage() for record in caplog.records] == [
        "a checagem de prontidão banco falhou: OSError",
        "a checagem de prontidão banco voltou a passar",
    ]
    assert check.calls == 5


async def test_results_are_cached_briefly_and_concurrent_probes_share_one_check() -> None:
    clock = _Clock()
    check = _Check("banco", delay=0.05)
    readiness = Readiness([check], cache_s=1.0, clock=clock)
    results = await asyncio.gather(*(readiness.failures() for _ in range(20)))
    assert results == [[]] * 20 and check.calls == 1
    clock.now += 0.5
    assert await readiness.failures() == [] and check.calls == 1
    clock.now += 1.0
    assert await readiness.failures() == [] and check.calls == 2


async def test_a_probe_that_gives_up_does_not_cancel_the_shared_check() -> None:
    check = _Check("banco", delay=0.05)
    readiness = Readiness([check], cache_s=0.0)
    impatient = asyncio.create_task(readiness.failures())
    await asyncio.sleep(0.01)
    patient = asyncio.create_task(readiness.failures())
    impatient.cancel()
    assert await patient == []
    assert check.calls == 1


def test_check_names_are_unique() -> None:
    with pytest.raises(ValueError, match="nome próprio"):
        Readiness([_Check("banco"), _Check("banco")])
    readiness = Readiness([_Check("banco")])
    readiness.add(_Check("cofre"))
    with pytest.raises(ValueError, match="cofre"):
        readiness.add(_Check("cofre"))


class _Supervisor:
    healthy = False


async def test_the_dispatcher_check_follows_the_supervisor() -> None:
    supervisor = _Supervisor()
    readiness = Readiness([DispatcherCheck(supervisor)], cache_s=0.0)
    assert await readiness.failures() == ["despachante"]
    supervisor.healthy = True
    assert await readiness.failures() == []


@dataclass(frozen=True, slots=True)
class _BackendCheck:
    """A forma das checagens do cofre e do S3 (``BaoReadiness``/``S3Readiness``)."""

    up: bool
    name: str = "openbao"

    async def ready(self) -> bool:
        return self.up


async def test_the_vault_and_storage_checks_plug_in_unchanged() -> None:
    db = Database.from_url("postgresql://rapa_app:x@127.0.0.1:1/rapa")
    try:
        checks = build_readiness_checks(
            db,
            None,
            None,
            extra=(_BackendCheck(up=False), _BackendCheck(up=True, name="s3")),
        )
        assert [check.name for check in checks] == ["banco", "openbao", "s3"]
        # O banco (porta fechada) e o cofre selado falham; o bucket responde.
        response = await Readiness(checks, cache_s=0.0).endpoint()
        assert response.status_code == 503
        assert response.body == b'{"estado":"nao_pronto","falhas":["banco","openbao"]}'
    finally:
        await db.dispose()
