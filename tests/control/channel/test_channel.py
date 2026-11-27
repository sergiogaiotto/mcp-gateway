"""Canal do despachante: bilhete por tentativa, trabalho uma vez só, limites e confirmação."""

import asyncio
import json
import logging
import socket
import stat
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest

from rapa.control import channel as channel_module
from rapa.control.channel import Channel, ChannelAttempt, ChannelLimits, ChannelServer
from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo
from tests.control.launcher.fake_kube import all_bytes

SECRETS = {"portal": {"username": "ana", "password": "Tr0ub4dor&3"}}
OPTIONS = {"guarda_de_saida": True, "espera_ack_s": 30.0}


def _job(attempt: int = 1) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=attempt,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs={"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _open(channel: Channel, tmp_path: Path, job: RunnerJob) -> tuple[ChannelAttempt, str]:
    work = tmp_path / f"{job.run_id}-{job.attempt}"
    (work / "ack").mkdir(parents=True)
    artifacts = tmp_path / "artefatos" / str(job.run_id)
    artifacts.mkdir(parents=True)
    return channel.open(
        job, work_dir=work, artifacts_dir=artifacts, secrets=SECRETS, options=OPTIONS
    )


@asynccontextmanager
async def client(channel: Channel, ticket: str | None) -> AsyncIterator[httpx2.AsyncClient]:
    headers = {"Authorization": f"Bearer {ticket}"} if ticket is not None else {}
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=channel.app),
        base_url="http://canal",
        headers=headers,
        trust_env=False,
    ) as http:
        yield http


def _url(job: RunnerJob, tail: str) -> str:
    return f"/canal/{job.run_id}/{job.attempt}/{tail}"


def _event(job: RunEvent | RunnerJob, seq: int, **data: object) -> bytes:
    assert isinstance(job, RunnerJob)
    return (
        RunEvent(
            run_id=job.run_id,
            attempt=job.attempt,
            seq=seq,
            at=datetime.now(UTC),
            kind=RunEventKind.STEP_STARTED,
            step_id="passo",
            data=dict(data),
        )
        .model_dump_json()
        .encode()
    )


async def test_a_ticket_only_opens_its_own_attempt(tmp_path: Path) -> None:
    channel = Channel()
    job_a, job_b = _job(), _job()
    _, ticket_a = _open(channel, tmp_path, job_a)
    attempt_b, ticket_b = _open(channel, tmp_path, job_b)
    later = _job(attempt=2).model_copy(update={"run_id": job_a.run_id})
    cases = [
        (ticket_b, _url(job_a, "trabalho")),  # bilhete de outra execução
        ("x" * 43, _url(job_a, "trabalho")),  # bilhete errado
        (ticket_a, _url(later, "trabalho")),  # a mesma execução, outra tentativa
        (ticket_a, f"/canal/{new_id()}/1/trabalho"),  # execução que não existe
        (ticket_a, "/canal/nao-e-uuid/1/trabalho"),
    ]
    bodies = set()
    for ticket, url in cases:
        async with client(channel, ticket) as http:
            response = await http.get(url)
        assert response.status_code == 401, url
        bodies.add(response.text)
    async with client(channel, None) as http:
        assert (await http.get(_url(job_a, "trabalho"))).status_code == 401
    # A mesma resposta em todos os casos: nada diz se a tentativa existe.
    assert len(bodies) == 1
    # O bilhete só fica pelo hash (e fora do repr).
    assert attempt_b._ticket != ticket_b.encode() and ticket_b not in repr(attempt_b)
    async with client(channel, ticket_b) as http:
        assert (await http.get(_url(job_b, "trabalho"))).status_code == 200


async def test_the_job_and_secrets_are_handed_over_once(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    async with client(channel, ticket) as http:
        first = await http.get(_url(job, "trabalho"))
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        body = first.json()
        assert RunnerJob.model_validate(body["trabalho"]) == job
        assert body["segredos"] == SECRETS and body["opcoes"] == OPTIONS
        assert attempt.fetched and attempt._payload is None  # os segredos saem da memória
        assert (await http.get(_url(job, "trabalho"))).status_code == 410
        # Nada dos segredos foi para o disco do despachante.
        assert b"Tr0ub4dor" not in all_bytes(tmp_path)
        # Depois do fim da tentativa, o bilhete não vale mais.
        channel.close(job.run_id, job.attempt)
        assert (await http.get(_url(job, "trabalho"))).status_code == 401
        assert (await http.put(_url(job, "resultado"), content=b"{}")).status_code == 401


async def test_events_are_appended_in_order_and_checked(tmp_path: Path) -> None:
    channel = Channel(ChannelLimits(max_events_body=64 * 1024, max_event_line=4096))
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    other = _job()
    async with client(channel, ticket) as http:
        post = lambda body: http.post(_url(job, "eventos"), content=body)  # noqa: E731
        assert (
            await post(_event(job, 0) + b"\n" + _event(job, 1, grava=True) + b"\n")
        ).status_code == 204
        assert (await post(_event(job, 2))).status_code == 204  # sem quebra de linha no fim
        assert (await post(b"")).status_code == 204
        refused = [
            (b"nao e json\n", 422),
            (b'{"run_id": 1}\n', 422),
            (_event(other, 3) + b"\n", 422),  # evento de outra execução
            (_event(job, 3, texto="x" * 5000) + b"\n", 413),  # linha grande demais
            ((_event(job, 3) + b"\n") * 1000, 413),  # lote grande demais
            (_event(job, 3) + b"\n" + b"nao e json\n", 422),  # um ruim derruba o lote inteiro
        ]
        for body, status in refused:
            assert (await post(body)).status_code == status
    lines = attempt.events_path.read_bytes().splitlines()
    assert [RunEvent.model_validate_json(line).seq for line in lines] == [0, 1, 2]


async def test_the_ack_is_a_long_wait_for_the_file(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    loop = asyncio.get_running_loop()
    async with client(channel, ticket) as http:
        started = loop.time()
        response = await http.get(_url(job, "ack/7"), params={"espera": "0.3"})
        assert response.status_code == 408 and loop.time() - started >= 0.3

        async def write_ack() -> None:
            await asyncio.sleep(0.3)
            (attempt.ack_dir / "7.ack").write_text("ok")

        writer = asyncio.create_task(write_ack())
        response = await http.get(_url(job, "ack/7"), params={"espera": "5"})
        await writer
        assert response.status_code == 204
        for bad in ("-1", "abc", "nan"):
            assert (await http.get(_url(job, "ack/8"), params={"espera": bad})).status_code == 400

        async def close_soon() -> None:
            await asyncio.sleep(0.2)
            channel.close(job.run_id, job.attempt)

        closer = asyncio.create_task(close_soon())
        response = await http.get(_url(job, "ack/8"), params={"espera": "5"})
        await closer
        assert response.status_code == 410


async def test_the_ack_wait_is_capped(tmp_path: Path) -> None:
    channel = Channel(ChannelLimits(max_ack_wait_s=0.2))
    job = _job()
    _, ticket = _open(channel, tmp_path, job)
    loop = asyncio.get_running_loop()
    async with client(channel, ticket) as http:
        started = loop.time()
        response = await http.get(_url(job, "ack/1"), params={"espera": "25"})
    assert response.status_code == 408 and loop.time() - started < 2


async def test_artifacts_follow_the_executor_name_and_size_rules(tmp_path: Path) -> None:
    channel = Channel(
        ChannelLimits(max_artifact_bytes=1000, max_attempt_bytes=1500, max_artifacts=3)
    )
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    async with client(channel, ticket) as http:
        put = lambda name, data: http.put(_url(job, f"artefatos/{name}"), content=data)  # noqa: E731
        stored = await put("tela-001-login-depois.png", b"png" * 10)
        assert stored.status_code == 201
        assert stored.json()["tamanho"] == 30
        path = attempt.artifacts_dir / "tela-001-login-depois.png"
        assert path.read_bytes() == b"png" * 10
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # O_EXCL: o mesmo nome não sobrescreve.
        assert (await put("tela-001-login-depois.png", b"outro")).status_code == 409
        assert path.read_bytes() == b"png" * 10
        for name in (".oculto", "a..b", "-traco", "x" * 131, "acentuação.png", "a%2Fb"):
            assert (await put(name, b"x")).status_code in (400, 404), name
        # Grande demais: recusado, sem sobra no disco.
        assert (await put("grande.pdf", b"x" * 1001)).status_code == 413
        assert not (attempt.artifacts_dir / "grande.pdf").exists()

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(11):
                yield b"y" * 100

        # Sem Content-Length (em pedaços), o limite vale do mesmo jeito.
        assert (
            await http.put(_url(job, "artefatos/pedacos.pdf"), content=chunks())
        ).status_code == 413
        assert not (attempt.artifacts_dir / "pedacos.pdf").exists()
        assert (await put("nota-1.pdf", b"z" * 900)).status_code == 201
        # O total da tentativa também tem limite.
        assert (await put("nota-2.pdf", b"z" * 900)).status_code == 413
        assert (await put("nota-3.pdf", b"z")).status_code == 201
        assert (await put("nota-4.pdf", b"z")).status_code == 413  # artefatos demais
    assert sorted(p.name for p in attempt.artifacts_dir.iterdir()) == [
        "nota-1.pdf",
        "nota-3.pdf",
        "tela-001-login-depois.png",
    ]


async def test_the_result_is_stored_once_and_has_a_limit(tmp_path: Path) -> None:
    channel = Channel(ChannelLimits(max_result_bytes=1024))
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    async with client(channel, ticket) as http:
        assert (await http.put(_url(job, "resultado"), content=b"x" * 1025)).status_code == 413
        assert not attempt.result_received.is_set()
        body = json.dumps({"run_id": str(job.run_id), "attempt": 1}).encode()
        assert (await http.put(_url(job, "resultado"), content=body)).status_code == 204
        assert attempt.result_received.is_set()
        assert attempt.result_path.read_bytes() == body
        assert stat.S_IMODE(attempt.result_path.stat().st_mode) == 0o600
        assert (await http.put(_url(job, "resultado"), content=b"{}")).status_code == 409
        assert attempt.result_path.read_bytes() == body


async def test_nothing_redirects(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    _, ticket = _open(channel, tmp_path, job)
    async with client(channel, ticket) as http:
        for url in (_url(job, "trabalho/"), f"/canal/{job.run_id}/{job.attempt}/trabalho//"):
            response = await http.get(url)
            # A barra no fim não redireciona (o Starlette faria um 307 por padrão).
            assert not response.is_redirect and response.status_code == 404, url
        assert (await http.post(_url(job, "trabalho"))).status_code == 405


def test_an_attempt_opens_once(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    _open(channel, tmp_path, job)
    with pytest.raises(ValueError, match="já está aberta"):
        _open(channel, tmp_path / "outra", job)
    assert len(channel) == 1
    channel.close(job.run_id, job.attempt)
    channel.close(job.run_id, job.attempt)  # fechar de novo não faz nada
    assert len(channel) == 0 and channel.get(job.run_id, job.attempt) is None


def _uvicorn_loggers() -> dict[str, tuple[int, list[logging.Handler], bool]]:
    loggers = {
        name: logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    return {
        name: (logger.level, list(logger.handlers), logger.propagate)
        for name, logger in loggers.items()
    }


async def test_the_channel_server_leaves_the_service_logging_alone() -> None:
    """O canal roda dentro do despachante: os loggers do uvicorn ficam como o serviço os
    configurou (no RAPA_LOG_JSON, sem handler próprio, saindo pelo raiz em JSON), em vez de
    ganharem o formato de texto e o nível do uvicorn."""
    saved = _uvicorn_loggers()
    try:
        # Como o serviço os deixa no JSON (uvicorn_log_config devolve None).
        for name in saved:
            logger = logging.getLogger(name)
            logger.handlers, logger.propagate = [], True
            logger.setLevel(logging.NOTSET)
        before = _uvicorn_loggers()
        async with ChannelServer(Channel(), host="127.0.0.1", port=0).running():
            assert _uvicorn_loggers() == before
        assert _uvicorn_loggers() == before
    finally:
        for name, (level, handlers, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers, logger.propagate = handlers, propagate


async def test_the_channel_server_serves_on_its_own_port(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    _, ticket = _open(channel, tmp_path, job)
    server = ChannelServer(channel, host="127.0.0.1", port=0)
    async with server.running() as port:
        # Um pod não toma todas as conexões do canal.
        assert server._server is not None
        assert server._server.config.limit_concurrency == channel.limits.max_connections
        async with httpx2.AsyncClient(trust_env=False) as http:
            base = f"http://127.0.0.1:{port}/canal/{job.run_id}/{job.attempt}"
            assert (await http.get(f"{base}/trabalho")).status_code == 401
            response = await http.get(
                f"{base}/trabalho", headers={"Authorization": f"Bearer {ticket}"}
            )
            assert response.status_code == 200
            assert UUID(response.json()["trabalho"]["run_id"]) == job.run_id
            assert "server" not in response.headers
    with pytest.raises(httpx2.ConnectError):
        async with httpx2.AsyncClient(trust_env=False) as http:
            await http.get(f"http://127.0.0.1:{port}/canal/x/1/trabalho")


async def test_the_events_of_an_attempt_have_a_total_limit(tmp_path: Path) -> None:
    channel = Channel(ChannelLimits(max_attempt_events_bytes=1000))
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    async with client(channel, ticket) as http:
        statuses = [
            (await http.post(_url(job, "eventos"), content=_event(job, seq) + b"\n")).status_code
            for seq in range(10)
        ]
    assert statuses[0] == 204 and statuses[-1] == 413
    assert attempt.events_path.stat().st_size <= 1000


async def _until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    give_up = loop.time() + timeout_s
    while not condition():
        if loop.time() >= give_up:
            raise AssertionError("a condição não chegou a tempo")
        await asyncio.sleep(0.01)


def _sizes(folder: Path) -> dict[str, int]:
    return {p.name: p.stat().st_size for p in folder.iterdir()}


async def test_parallel_uploads_never_pass_the_attempt_quota(tmp_path: Path) -> None:
    # A reprodução da revisão: dez envios de 900 bytes ao mesmo tempo, com cota de 1500.
    channel = Channel(
        ChannelLimits(max_artifact_bytes=1000, max_attempt_bytes=1500, max_artifacts=50)
    )
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)

    async def body() -> AsyncIterator[bytes]:
        for _ in range(9):
            yield b"z" * 100
            await asyncio.sleep(0)  # os envios se intercalam pedaço a pedaço

    async with client(channel, ticket) as http:
        responses = await asyncio.gather(
            *(http.put(_url(job, f"artefatos/nota-{n}.pdf"), content=body()) for n in range(10))
        )
    statuses = sorted(r.status_code for r in responses)
    assert set(statuses) <= {201, 413} and 201 in statuses
    sizes = _sizes(attempt.artifacts_dir)
    # Nunca mais que a cota no disco, e o que foi recusado não deixou sobra.
    assert sum(sizes.values()) <= 1500
    assert all(size == 900 for size in sizes.values())
    assert len(sizes) == statuses.count(201)
    # A conta da tentativa é exatamente o que ficou (as reservas recusadas voltaram).
    assert attempt.artifact_bytes == sum(sizes.values()) and attempt.artifacts == len(sizes)
    assert attempt.active == 0


async def test_an_upload_in_flight_stops_when_the_attempt_closes(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    gate = asyncio.Event()

    async def body() -> AsyncIterator[bytes]:
        yield b"a" * 100
        await gate.wait()  # o pod segura o envio aberto
        yield b"b" * 100

    path = attempt.artifacts_dir / "intruso.bin"
    async with client(channel, ticket) as http:
        upload = asyncio.create_task(http.put(_url(job, "artefatos/intruso.bin"), content=body()))
        await _until(lambda: path.exists() and attempt.active == 1)
        channel.close(job.run_id, job.attempt)
        # O fechamento encerra o envio na hora, e o lançador espera por isso.
        assert await channel.drain(attempt, timeout_s=2)
        response = await asyncio.wait_for(upload, 5)
    assert response.status_code == 410
    # Nada do envio interrompido fica na pasta (o lançador decide sobre ela depois).
    assert not path.exists()
    assert attempt.artifacts == 0 and attempt.artifact_bytes == 0


async def test_a_name_still_arriving_is_not_answered_as_already_stored(tmp_path: Path) -> None:
    """409 diz ao agente que o artefato já chegou inteiro; enquanto outro pedido ainda o
    grava (e pode falhar, apagando o arquivo), a resposta é 429 e o agente tenta de novo."""
    channel = Channel(ChannelLimits(idle_timeout_s=0.3))
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    url = _url(job, "artefatos/baixado-001.pdf")
    path = attempt.artifacts_dir / "baixado-001.pdf"

    async def stalls() -> AsyncIterator[bytes]:
        yield b"a" * 100
        await asyncio.sleep(30)  # a conexão do primeiro envio morreu no meio
        yield b"b" * 100

    async with client(channel, ticket) as http:
        first = asyncio.create_task(http.put(url, content=stalls()))
        await _until(lambda: path.exists() and attempt.active == 1)
        retry = await http.put(url, content=b"x" * 200)
        assert retry.status_code == 429 and retry.json() == {"erro": "artefato_em_andamento"}
        # O primeiro envio não termina: ele não deixa arquivo, e o próximo envio entra.
        assert (await asyncio.wait_for(first, 5)).status_code == 408
        assert not path.exists()
        assert (await http.put(url, content=b"x" * 200)).status_code == 201
        # Agora sim, o nome já chegou inteiro.
        assert (await http.put(url, content=b"y" * 200)).status_code == 409
    assert path.read_bytes() == b"x" * 200
    assert attempt.artifacts == 1 and attempt.artifact_bytes == 200 and attempt.active == 0


@pytest.mark.parametrize("route", ["artefatos/lento.bin", "eventos", "resultado"])
async def test_a_body_that_stops_coming_gets_408(tmp_path: Path, route: str) -> None:
    channel = Channel(ChannelLimits(idle_timeout_s=0.3))
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)

    async def body() -> AsyncIterator[bytes]:
        yield _event(job, 0)
        await asyncio.sleep(30)  # o pod nunca termina o corpo
        yield b"\n"

    method = "POST" if route == "eventos" else "PUT"
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with client(channel, ticket) as http:
        response = await http.request(method, _url(job, route), content=body())
    assert response.status_code == 408 and loop.time() - started < 5
    assert list(attempt.artifacts_dir.iterdir()) == []
    assert not attempt.events_path.exists() and not attempt.result_received.is_set()
    assert attempt.active == 0


async def test_requests_of_an_attempt_are_capped_and_others_go_on(tmp_path: Path) -> None:
    channel = Channel(ChannelLimits(max_requests=3))
    job, other = _job(), _job()
    attempt, ticket = _open(channel, tmp_path, job)
    other_attempt, other_ticket = _open(channel, tmp_path, other)
    async with client(channel, ticket) as http:
        waits = [
            asyncio.create_task(http.get(_url(job, f"ack/{seq}"), params={"espera": "10"}))
            for seq in (1, 2, 3)
        ]
        await _until(lambda: attempt.active == 3)
        # Sem vaga: 429 na hora, sem ler o corpo (o agente repete com espera crescente).
        assert (await http.get(_url(job, "ack/4"), params={"espera": "10"})).status_code == 429
        response = await http.post(_url(job, "eventos"), content=_event(job, 0))
        assert response.status_code == 429 and not attempt.events_path.exists()
        # Outra tentativa não sente nada.
        async with client(channel, other_ticket) as other_http:
            response = await other_http.post(_url(other, "eventos"), content=_event(other, 0))
        assert response.status_code == 204 and other_attempt.active == 0
        for seq in (1, 2, 3):
            (attempt.ack_dir / f"{seq}.ack").write_text("ok")
        attempt.wake_acks()
        assert [r.status_code for r in await asyncio.gather(*waits)] == [204, 204, 204]
    assert attempt.active == 0


async def test_one_events_batch_at_a_time(tmp_path: Path) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    gate = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        yield _event(job, 0)
        await gate.wait()
        yield b"\n"

    read: list[int] = []

    async def counted() -> AsyncIterator[bytes]:
        read.append(1)
        yield _event(job, 1) + b"\n"

    async with client(channel, ticket) as http:
        first = asyncio.create_task(http.post(_url(job, "eventos"), content=slow()))
        await _until(lambda: attempt._events_lock.locked())
        # Um segundo lote ao mesmo tempo nem é lido: o corpo só entra na memória na vez dele.
        second = await http.post(_url(job, "eventos"), content=counted())
        assert second.status_code == 429 and read == []
        gate.set()
        assert (await first).status_code == 204
        assert (await http.post(_url(job, "eventos"), content=counted())).status_code == 204
    assert [
        RunEvent.model_validate_json(line).seq
        for line in attempt.events_path.read_bytes().splitlines()
    ] == [0, 1]


async def test_the_ack_wait_wakes_on_the_launcher_signal_without_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = _open(channel, tmp_path, job)
    # Sem o aviso, a espera só confere o arquivo a cada 30 s: quem acorda é o aviso.
    monkeypatch.setattr("rapa.control.channel._ACK_POLL_S", 30.0)

    def no_threads(*_: object, **__: object) -> None:
        raise AssertionError("a espera pela confirmação não usa threads")

    monkeypatch.setattr("rapa.control.channel.asyncio.to_thread", no_threads)
    monkeypatch.setattr(channel, "_disk", no_threads)
    loop = asyncio.get_running_loop()
    async with client(channel, ticket) as http:
        wait = asyncio.create_task(http.get(_url(job, "ack/9"), params={"espera": "25"}))
        await _until(lambda: attempt.active == 1)
        started = loop.time()
        (attempt.ack_dir / "9.ack").write_text("ok")
        attempt.wake_acks()
        response = await asyncio.wait_for(wait, 5)
    assert response.status_code == 204 and loop.time() - started < 5


async def test_the_channel_disk_has_its_own_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = Channel()
    job = _job()
    _, ticket = _open(channel, tmp_path, job)
    threads: list[str] = []
    original = channel_module._append

    def append(path: Path, data: bytes) -> None:
        threads.append(threading.current_thread().name)
        original(path, data)

    monkeypatch.setattr(channel_module, "_append", append)
    async with client(channel, ticket) as http:
        assert (await http.post(_url(job, "eventos"), content=_event(job, 0))).status_code == 204
        assert (await http.put(_url(job, "artefatos/a.png"), content=b"x")).status_code == 201
    # Um pod que ocupa o disco do canal não ocupa as threads do seguimento das execuções.
    assert threads and all(name.startswith("canal-disco") for name in threads)


async def test_input_files_are_served_only_when_registered(tmp_path: Path) -> None:
    channel = Channel()
    job, other = _job(), _job()
    company = tmp_path / "arquivos"
    company.mkdir()
    (company / "nota.pdf").write_bytes(b"%PDF nota" * 10_000)
    (company / "outro.pdf").write_bytes(b"de fora")
    work = tmp_path / "w"
    (work / "ack").mkdir(parents=True)
    attempt, ticket = channel.open(
        job,
        work_dir=work,
        artifacts_dir=tmp_path / "a",
        secrets={},
        options={},
        files={"nota.pdf": company / "nota.pdf"},
    )
    _, other_ticket = _open(channel, tmp_path, other)
    async with client(channel, ticket) as http:
        response = await http.get(_url(job, "arquivos/nota.pdf"))
        assert response.status_code == 200 and response.content == b"%PDF nota" * 10_000
        assert response.headers["cache-control"] == "no-store"
        # Só os arquivos registrados para a tentativa: nem outro da pasta, nem um caminho.
        for name in ("outro.pdf", "..%2Foutro.pdf", "nota.pdf%00"):
            assert (await http.get(_url(job, f"arquivos/{name}"))).status_code == 404, name
    assert attempt.active == 0
    async with client(channel, other_ticket) as http:
        assert (await http.get(_url(job, "arquivos/nota.pdf"))).status_code == 401
    channel.close(job.run_id, job.attempt)
    async with client(channel, ticket) as http:
        assert (await http.get(_url(job, "arquivos/nota.pdf"))).status_code == 401


async def test_the_server_listens_on_both_stacks_by_default(tmp_path: Path) -> None:
    if not socket.has_dualstack_ipv6():
        pytest.skip("o sistema não tem as duas pilhas (IPv4 e IPv6) no mesmo socket")
    channel = Channel()
    async with ChannelServer(channel, port=0).running() as port:
        async with httpx2.AsyncClient(trust_env=False) as http:
            for host in ("127.0.0.1", "[::1]"):
                try:
                    response = await http.get(f"http://{host}:{port}/canal/{new_id()}/1/trabalho")
                except httpx2.ConnectError:
                    if host == "[::1]":
                        pytest.skip("sem o loopback IPv6 aqui")
                    raise
                assert response.status_code == 401, host
