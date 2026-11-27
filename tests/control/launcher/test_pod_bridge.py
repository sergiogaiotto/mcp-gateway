"""A ponte do agente do pod com um worker de mentira: eventos, confirmação, artefatos,
resultado e SIGTERM, contra o canal de verdade e sem navegador."""

import asyncio
import json
import logging
import os
import signal
import sys
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest

from rapa.control.channel import Channel, ChannelAttempt, ChannelServer
from rapa.core.contracts import RunnerJob
from rapa.core.events import RunEvent
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo
from rapa.runner.pod import (
    ChannelClient,
    PodOptions,
    _artifacts_to_upload,
    _Bridge,
    _retrying,
    run_pod,
)

#: Um "worker" que anuncia um passo que grava, espera a confirmação, guarda um artefato e
#: escreve o resultado; num SIGTERM, escreve o resultado "cancelada" e sai.
_STUB = """#!{python}
import json, os, signal, sys, time
args = sys.argv[1:]
def arg(name):
    return args[args.index(name) + 1]
job = json.load(open(arg("--job")))
events, ack_dir, artifacts, result = arg("--events"), arg("--ack-dir"), arg("--artifacts"), arg("--result")
open({argv_file!r}, "w").write(json.dumps(args))
if "--files-dir" in args:
    folder = arg("--files-dir")
    open({argv_file!r} + ".arquivos", "w").write(json.dumps(
        {{name: open(os.path.join(folder, name), "rb").read().decode() for name in os.listdir(folder)}}
    ))
state = {{"stop": False}}
signal.signal(signal.SIGTERM, lambda *_: state.update(stop=True))
def emit(seq, kind, **data):
    with open(events, "a") as out:
        out.write(json.dumps({{
            "run_id": job["run_id"], "attempt": job["attempt"], "seq": seq,
            "at": "2026-11-22T00:00:00Z", "kind": kind, "step_id": "emitir", "data": data,
        }}) + "\\n")
first = job.get("first_seq", 0)
emit(first, "run_started")
emit(first + 1, "step_started", grava=True)
waited = time.time()
while not os.path.exists(os.path.join(ack_dir, f"{{first + 1}}.ack")) and not state["stop"]:
    if time.time() - waited > 10:
        break
    time.sleep(0.02)
name = "tela-001-emitir-depois.png"
path = os.path.join(artifacts, name)
open(path, "wb").write(b"PNG" * 100)
final = "cancelled" if state["stop"] else "succeeded"
emit(first + 2, "run_succeeded" if final == "succeeded" else "run_failed")
json.dump({{
    "run_id": job["run_id"], "attempt": job["attempt"], "state": final, "outputs": None,
    "error_code": "cancelada" if state["stop"] else None, "error_message": None,
    "failed_step_id": None, "irreversible_started": False, "steps_done": 1,
    "artifacts": [
        {{"kind": "screenshot", "name": name, "path": os.path.abspath(path), "size": 300,
          "sha256": "0" * 64}},
        {{"kind": "download", "name": "passwd", "path": "/etc/passwd", "size": 1, "sha256": "0" * 64}},
    ],
}}, open(result, "w"))
"""


def _job() -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs={"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        first_seq=5,
    )


class FlakyTransport(httpx2.AsyncBaseTransport):
    """O canal de verdade (no processo), com rotas que o teste derruba e levanta."""

    def __init__(self, channel: Channel) -> None:
        self.inner = httpx2.ASGITransport(app=channel.app)
        #: Rotas fora do ar agora (``eventos``, ``ack``, ``artefatos``, ``resultado``).
        self.failing: set[str] = set()
        self.failures: Counter[str] = Counter()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        route = request.url.path.split("/")[4]
        if route in self.failing:
            self.failures[route] += 1
            raise httpx2.ConnectError("o canal caiu", request=request)
        return await self.inner.handle_async_request(request)


@asynccontextmanager
async def _pod(
    tmp_path: Path,
    options: PodOptions | None = None,
    *,
    files: dict[str, Path] | None = None,
    flaky: bool = False,
) -> AsyncIterator[
    tuple[RunnerJob, ChannelAttempt, asyncio.Task[int], Path, FlakyTransport | None]
]:
    """Abre a tentativa num canal de verdade e roda o agente (no processo) com o worker de mentira.

    Com ``flaky``, o agente fala com o canal por um :class:`FlakyTransport`.
    """
    channel = Channel()
    job = _job()
    work = tmp_path / "despachante"
    (work / "ack").mkdir(parents=True)
    artifacts = tmp_path / "artefatos"
    artifacts.mkdir()
    attempt, ticket = channel.open(
        job,
        work_dir=work,
        artifacts_dir=artifacts,
        secrets={"portal": {"password": "Tr0ub4dor&3"}},
        options=(options or PodOptions(egress_guard=True)).wire(),
        files=files,
    )
    transport = FlakyTransport(channel) if flaky else None
    stub = tmp_path / "worker.py"
    argv = tmp_path / "argv.json"
    stub.write_text(_STUB.format(python=sys.executable, argv_file=str(argv)))
    stub.chmod(0o755)
    async with ChannelServer(channel, host="127.0.0.1", port=0).running() as port:
        canal = tmp_path / "canal"
        canal.mkdir()
        (canal / "endereco").write_text(f"http://127.0.0.1:{port}/canal/{job.run_id}/1\n")
        (canal / "bilhete").write_text(ticket)
        pod_root = tmp_path / "pod"
        (pod_root / "trabalho").mkdir(parents=True)
        (pod_root / "tmp").mkdir()
        agent = asyncio.create_task(
            run_pod(
                canal,
                pod_root / "trabalho",
                pod_root / "tmp",
                python=str(stub),
                kill_grace_s=2,
                transport=transport,
            )
        )
        try:
            yield job, attempt, agent, argv, transport
        finally:
            if not agent.done():
                agent.cancel()
            await asyncio.gather(agent, return_exceptions=True)
            # Como o lançador: a tentativa fecha antes do canal (as esperas longas acabam).
            channel.close(job.run_id, job.attempt)


async def _wait_events(attempt: ChannelAttempt, count: int) -> list[RunEvent]:
    for _ in range(500):
        if attempt.events_path.exists():
            lines = attempt.events_path.read_bytes().splitlines()
            if len(lines) >= count:
                return [RunEvent.model_validate_json(line) for line in lines]
        await asyncio.sleep(0.02)
    raise AssertionError("os eventos não chegaram ao canal")


def _listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


async def test_the_agent_bridges_events_acks_artifacts_and_the_result(tmp_path: Path) -> None:
    async with _pod(tmp_path) as (_, attempt, agent, argv, _flaky):
        events = await _wait_events(attempt, 2)
        assert [e.seq for e in events] == [5, 6]
        # Sem a confirmação do lançador, o agente não inventa uma: o worker continua esperando.
        await asyncio.sleep(0.5)
        local_ack = tmp_path / "pod" / "trabalho" / "tentativa" / "ack" / "6.ack"
        assert not local_ack.exists() and not agent.done()
        (attempt.ack_dir / "6.ack").write_text("ok")  # o lançador gravou o anúncio no banco
        assert await asyncio.wait_for(agent, 30) == 0
    result = json.loads(attempt.result_path.read_bytes())
    assert result["state"] == "succeeded"
    # O artefato da pasta da tentativa subiu; o caminho de fora dela (/etc/passwd), não.
    assert (attempt.artifacts_dir / "tela-001-emitir-depois.png").read_bytes() == b"PNG" * 100
    assert _listing(attempt.artifacts_dir) == ["tela-001-emitir-depois.png"]
    assert [e.seq for e in await _wait_events(attempt, 3)] == [5, 6, 7]
    # O worker recebeu a guarda da saída e os segredos num arquivo do tmpfs, já apagado.
    args = json.loads(argv.read_text())
    assert "--guarda-de-saida" in args
    secrets = Path(args[args.index("--secrets") + 1])
    assert secrets.parent.parent == tmp_path / "pod" / "tmp"
    assert secrets.parent.name not in _listing(tmp_path / "pod" / "tmp")
    assert _listing(tmp_path / "pod" / "tmp") == []
    assert _listing(tmp_path / "pod" / "trabalho") == []


async def test_a_sigterm_stops_the_worker_and_still_delivers_the_result(tmp_path: Path) -> None:
    async with _pod(tmp_path) as (_, attempt, agent, _argv, _flaky):
        await _wait_events(attempt, 2)
        # O lançador apagou o pod: o kubelet manda SIGTERM ao agente.
        os.kill(os.getpid(), signal.SIGTERM)
        assert await asyncio.wait_for(agent, 30) == 0
    result = json.loads(attempt.result_path.read_bytes())
    assert result["state"] == "cancelled" and result["error_code"] == "cancelada"
    assert not (tmp_path / "pod" / "trabalho" / "tentativa").exists()


async def test_the_agent_gives_up_when_the_channel_closes_the_attempt(tmp_path: Path) -> None:
    channel_closed: dict[str, Any] = {}
    async with _pod(tmp_path) as (job, attempt, agent, _argv, _flaky):
        await _wait_events(attempt, 2)
        attempt.closed = True  # a tentativa acabou no despachante (ficou antiga)
        channel_closed["done"] = True
        (attempt.ack_dir / "6.ack").write_text("ok")
        # Nada mais é aceito: o agente para o worker e desiste sem resultado entregue.
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await asyncio.wait_for(agent, 30) == 1
        assert loop.time() - started < 8
    assert not attempt.result_path.exists()
    assert not (tmp_path / "pod" / "trabalho" / "tentativa").exists()
    assert job.run_id and channel_closed["done"]


def test_only_files_in_the_attempt_folder_are_uploaded(tmp_path: Path) -> None:
    folder = tmp_path / "artefatos"
    folder.mkdir()
    (folder / "tela.png").write_bytes(b"x")
    (tmp_path / "fora.txt").write_bytes(b"y")
    (folder / "atalho.txt").symlink_to(tmp_path / "fora.txt")
    result = json.dumps(
        {
            "artifacts": [
                {"name": "tela.png", "path": str(folder / "tela.png")},
                {"name": "fora.txt", "path": str(tmp_path / "fora.txt")},
                {"name": "atalho.txt", "path": str(folder / "atalho.txt")},
                {"name": "outro.png", "path": str(folder / "tela.png")},
                {"name": "sumiu.png", "path": str(folder / "sumiu.png")},
                {"name": "tela.png", "path": str(folder / "tela.png")},
            ]
        }
    ).encode()
    assert _artifacts_to_upload(result, folder) == [("tela.png", folder / "tela.png")]
    assert _artifacts_to_upload(b"nao e json", folder) == []


@pytest.mark.parametrize(
    "address", ["ftp://canal:1/x", "http://u:s@canal:1/x", "http://canal:1/x?a=1"]
)
async def test_a_bad_channel_address_stops_the_agent(tmp_path: Path, address: str) -> None:
    canal = tmp_path / "canal"
    canal.mkdir()
    (canal / "endereco").write_text(address)
    (canal / "bilhete").write_text("b" * 43)
    assert await run_pod(canal, tmp_path, tmp_path) == 1
    assert await run_pod(tmp_path / "sem-canal", tmp_path, tmp_path) == 1


async def _until(condition: Callable[[], bool], timeout_s: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    give_up = loop.time() + timeout_s
    while not condition():
        if loop.time() >= give_up:
            raise AssertionError("a condição não chegou a tempo")
        await asyncio.sleep(0.02)


async def test_when_the_channel_drops_mid_run_everything_is_retried(tmp_path: Path) -> None:
    async with _pod(tmp_path, flaky=True) as (_, attempt, agent, _argv, flaky):
        assert flaky is not None
        local_ack = tmp_path / "pod" / "trabalho" / "tentativa" / "ack" / "6.ack"
        # Os lotes de eventos não passam: o agente tenta de novo, com espera crescente.
        flaky.failing = {"eventos", "ack"}
        await _until(lambda: flaky.failures["eventos"] >= 2)
        assert not attempt.events_path.exists()
        # O lançador já confirmou, mas a espera pelo ack também não passa: nada é inventado.
        (attempt.ack_dir / "6.ack").write_text("ok")
        flaky.failing = {"ack"}
        assert [e.seq for e in await _wait_events(attempt, 2)] == [5, 6]
        await _until(lambda: flaky.failures["ack"] >= 2)
        assert not local_ack.exists() and not agent.done()
        # A confirmação passa; o worker segue, e a entrega do artefato e do resultado cai.
        flaky.failing = {"artefatos", "resultado"}
        await _until(local_ack.exists)
        await _until(lambda: flaky.failures["artefatos"] >= 2)
        assert not attempt.result_received.is_set()
        flaky.failing = {"resultado"}
        await _until(lambda: flaky.failures["resultado"] >= 2)
        assert (attempt.artifacts_dir / "tela-001-emitir-depois.png").exists()
        assert not attempt.result_received.is_set()
        flaky.failing = set()
        assert await asyncio.wait_for(agent, 30) == 0
    assert json.loads(attempt.result_path.read_bytes())["state"] == "succeeded"
    assert [e.seq for e in await _wait_events(attempt, 3)] == [5, 6, 7]
    assert (attempt.artifacts_dir / "tela-001-emitir-depois.png").read_bytes() == b"PNG" * 100


class _DropsFirstArtifact(httpx2.AsyncBaseTransport):
    """TCP de verdade; a conexão do primeiro envio de artefato cai no meio do corpo."""

    def __init__(self) -> None:
        self.inner = httpx2.AsyncHTTPTransport()
        self.dropped = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if "/artefatos/" in request.url.path and not self.dropped:
            self.dropped = True
            stream = request.stream
            assert isinstance(stream, httpx2.AsyncByteStream)
            original: httpx2.AsyncByteStream = stream

            class Dropping(httpx2.AsyncByteStream):
                async def __aiter__(self) -> AsyncIterator[bytes]:
                    async for chunk in original:
                        yield chunk
                        await asyncio.sleep(0.05)
                        raise httpx2.WriteError("a conexão caiu")

            request.stream = Dropping()
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def test_an_artifact_retried_while_the_first_upload_is_still_open_is_not_lost(
    tmp_path: Path,
) -> None:
    """A conexão do primeiro envio cai enquanto o disco do canal está lento: a nova
    tentativa chega com o primeiro envio ainda aberto (que depois falha e apaga o arquivo).
    O agente não pode dar o artefato por entregue nessa hora."""
    from rapa.runner.pod import _deliver

    channel = Channel()
    job = _job()
    attempt, ticket = channel.open(
        job, work_dir=tmp_path / "canal", artifacts_dir=tmp_path / "a", secrets={}, options={}
    )
    attempt.ack_dir.mkdir(parents=True)
    attempt.artifacts_dir.mkdir()
    real_disk, slow = channel._disk, [True]

    async def slow_disk(function: Any, *args: Any) -> Any:
        if slow[0] and getattr(function, "__name__", "") == "write":
            slow[0] = False  # a fila do disco segura a primeira gravação
            await asyncio.sleep(1.0)
        return await real_disk(function, *args)

    channel._disk = slow_disk  # type: ignore[method-assign]
    work = tmp_path / "pod"
    (work / "artefatos").mkdir(parents=True)
    downloaded = work / "artefatos" / "baixado-001.pdf"
    downloaded.write_bytes(b"P" * 200_000)
    artifact = {"name": downloaded.name, "path": str(downloaded)}
    (work / "result.json").write_text(json.dumps({"state": "succeeded", "artifacts": [artifact]}))
    async with ChannelServer(channel, host="127.0.0.1", port=0).running() as port:
        address = f"http://127.0.0.1:{port}/canal/{job.run_id}/1"
        transport = _DropsFirstArtifact()
        async with ChannelClient(address, ticket, transport=transport) as client:
            loop = asyncio.get_running_loop()
            assert await _deliver(client, work, loop.time() + 15) == 0
        assert transport.dropped and not slow[0]
        channel.close(job.run_id, job.attempt)
        assert await channel.drain(attempt)
    # O resultado cita o artefato, e ele está no canal, inteiro.
    assert attempt.result_received.is_set()
    assert (attempt.artifacts_dir / downloaded.name).read_bytes() == b"P" * 200_000


async def test_with_the_channel_down_nothing_is_confirmed_until_the_deadline(
    tmp_path: Path,
) -> None:
    channel = Channel()
    job = _job()
    attempt, ticket = channel.open(
        job, work_dir=tmp_path / "canal", artifacts_dir=tmp_path / "a", secrets={}, options={}
    )
    attempt.ack_dir.mkdir(parents=True)
    flaky = FlakyTransport(channel)
    client = ChannelClient(f"http://canal/canal/{job.run_id}/1", ticket, transport=flaky)
    work = tmp_path / "pod"
    (work / "ack").mkdir(parents=True)
    with (work / "events.jsonl").open("w") as out:
        for seq, kind, data in ((5, "run_started", {}), (6, "step_started", {"grava": True})):
            out.write(
                json.dumps(
                    {
                        "run_id": str(job.run_id),
                        "attempt": 1,
                        "seq": seq,
                        "at": "2026-11-22T00:00:00Z",
                        "kind": kind,
                        "step_id": "emitir",
                        "data": data,
                    }
                )
                + "\n"
            )
    (attempt.ack_dir / "6.ack").write_text("ok")  # o lançador confirmaria
    loop = asyncio.get_running_loop()
    async with client:
        # O canal fora do ar até o prazo: os eventos ficam pendentes, nenhum ack aparece.
        flaky.failing = {"eventos", "ack"}
        bridge = _Bridge(client, work, loop.time() + 1.5)
        started = loop.time()
        await bridge.flush()
        assert 1.4 <= loop.time() - started < 5
        assert len(bridge.pending) == 2 and flaky.failures["eventos"] >= 2
        assert not attempt.events_path.exists() and not (work / "ack" / "6.ack").exists()
        await bridge.close()
        # Os eventos passam, a espera pela confirmação não: até o prazo, sem ack local.
        flaky.failing = {"ack"}
        bridge = _Bridge(client, work, loop.time() + 1.5)
        await bridge.flush()
        assert not bridge.pending and list(bridge.acks) == [6]
        await asyncio.wait_for(bridge.acks[6], 5)
        assert flaky.failures["ack"] >= 2 and not (work / "ack" / "6.ack").exists()
        await bridge.close()
        # Com o canal de volta, a confirmação chega e só então vira o ack do worker.
        flaky.failing = set()
        bridge = _Bridge(client, work, loop.time() + 5)
        await bridge.flush()
        await asyncio.wait_for(bridge.acks[6], 5)
        assert (work / "ack" / "6.ack").exists()
        await bridge.close()

        # A entrega do resultado também desiste no prazo, sem inventar sucesso.
        async def down() -> bool:
            raise httpx2.ConnectError("o canal caiu")

        started = loop.time()
        assert await _retrying(down, loop.time() + 0.8, "resultado") is None
        assert 0.5 <= loop.time() - started < 3


async def test_input_files_come_from_the_channel_before_the_worker(tmp_path: Path) -> None:
    company = tmp_path / "empresa"
    company.mkdir()
    (company / "nota.pdf").write_text("conteudo da nota")
    options = PodOptions(egress_guard=True, files=("nota.pdf", "sumiu.pdf"))
    files = {"nota.pdf": company / "nota.pdf", "sumiu.pdf": company / "sumiu.pdf"}
    async with _pod(tmp_path, options, files=files) as (_, attempt, agent, argv, _flaky):
        await _wait_events(attempt, 2)
        (attempt.ack_dir / "6.ack").write_text("ok")
        assert await asyncio.wait_for(agent, 30) == 0
    folder, seen, left = _files_seen(argv)
    assert folder == tmp_path / "pod" / "trabalho" / "tentativa" / "arquivos"
    # O worker achou o arquivo baixado; o que o canal não tem ficou de fora (o passo de
    # envio falha com "arquivo não encontrado", como no local).
    assert seen == {"nota.pdf": "conteudo da nota"}
    assert not left  # a pasta da tentativa some no fim


def _files_seen(argv: Path) -> tuple[Path, dict[str, str], bool]:
    """A pasta de arquivos que o worker recebeu, o que ele achou nela e se ela sobrou."""
    args = json.loads(argv.read_text())
    folder = Path(args[args.index("--files-dir") + 1])
    seen = json.loads(argv.with_name(f"{argv.name}.arquivos").read_text())
    return folder, seen, folder.exists()


async def test_without_a_files_folder_the_worker_gets_none(tmp_path: Path) -> None:
    async with _pod(tmp_path) as (_, attempt, agent, argv, _flaky):
        await _wait_events(attempt, 2)
        (attempt.ack_dir / "6.ack").write_text("ok")
        assert await asyncio.wait_for(agent, 30) == 0
    assert "--files-dir" not in json.loads(argv.read_text())


def test_file_names_in_the_options_are_checked() -> None:
    for bad in ("../segredo", "/etc/passwd", "a/b", ".oculto"):
        with pytest.raises(ValueError, match="identificador de arquivo"):
            PodOptions.model_validate({"arquivos": [bad]})
    assert PodOptions.model_validate({"arquivos": ["nota.pdf"]}).files == ("nota.pdf",)


async def test_the_channel_client_never_follows_redirects(tmp_path: Path) -> None:
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(307, headers={"Location": "http://outro.exemplo/trabalho"})

    canal = tmp_path / "canal"
    canal.mkdir()
    (canal / "endereco").write_text(f"http://canal/canal/{new_id()}/1")
    (canal / "bilhete").write_text("b" * 43)
    code = await run_pod(canal, tmp_path, tmp_path, transport=httpx2.MockTransport(handle))
    assert code == 1
    assert len(seen) == 1 and "outro.exemplo" not in seen[0]


async def test_the_channel_client_ignores_the_proxy_of_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = Channel()
    job = _job()
    _, ticket = channel.open(
        job, work_dir=tmp_path / "w", artifacts_dir=tmp_path / "a", secrets={}, options={}
    )
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # um proxy que não existe
    async with ChannelServer(channel, host="127.0.0.1", port=0).running() as port:
        async with ChannelClient(f"http://127.0.0.1:{port}/canal/{job.run_id}/1", ticket) as client:
            payload = await client.fetch_job()
    assert RunnerJob.model_validate(payload["trabalho"]) == job


async def test_a_sigterm_after_the_attempt_does_not_kill_the_agent_on_its_way_out(
    tmp_path: Path,
) -> None:
    from rapa.runner.pod import _main, _outside_attempt

    previous = signal.getsignal(signal.SIGTERM)
    try:
        # O lançador apaga o pod logo depois do resultado: o SIGTERM que chega enquanto o
        # agente já está saindo não troca o código de saída dele (e nunca é o tratamento
        # padrão, que mataria o processo com -15).
        assert await _main(tmp_path / "sem-canal", tmp_path, tmp_path) == 1
        assert signal.getsignal(signal.SIGTERM) is _outside_attempt
        os.kill(os.getpid(), signal.SIGTERM)  # ignorado
        await asyncio.sleep(0.05)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_the_agent_log_does_not_carry_the_channel_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cada pedido do httpx2 sairia em INFO com a URL (a execução, a tentativa e o nome de
    cada arquivo de entrada e artefato); o agente só deixa os avisos dele."""
    from rapa.runner import pod

    async def no_channel(*_: Any) -> int:
        return 0

    quiet = logging.getLogger("httpx2")
    previous = quiet.level
    monkeypatch.setattr(pod, "_main", no_channel)
    try:
        assert pod.main([f"--canal={tmp_path}", f"--trabalho={tmp_path}", f"--tmp={tmp_path}"]) == 0
        assert not quiet.isEnabledFor(logging.INFO) and quiet.isEnabledFor(logging.WARNING)
    finally:
        quiet.setLevel(previous)


async def test_a_slow_request_answered_with_408_is_sent_again(tmp_path: Path) -> None:
    """O 408 do canal é o "pedido lento" (o disco dele demorou): o agente tenta de novo,
    em vez de matar o robô no meio (eventos) ou perder um resultado pronto."""
    from rapa.control import channel as channel_module
    from rapa.control.channel import ChannelLimits
    from rapa.runner.pod import _deliver

    channel = Channel(ChannelLimits(idle_timeout_s=0.3))
    job = _job()
    attempt, ticket = channel.open(
        job, work_dir=tmp_path / "canal", artifacts_dir=tmp_path / "a", secrets={}, options={}
    )
    attempt.ack_dir.mkdir(parents=True)
    attempt.artifacts_dir.mkdir()
    real_disk, slow = channel._disk, {channel_module._append, channel_module._write_atomic}

    async def slow_once(function: Any, *args: Any) -> Any:
        # A primeira gravação de cada tipo passa do tempo do pedido (a fila do disco).
        if function in slow:
            slow.discard(function)
            await asyncio.sleep(0.6)
        return await real_disk(function, *args)

    channel._disk = slow_once  # type: ignore[method-assign]
    transport = httpx2.ASGITransport(app=channel.app)
    client = ChannelClient(f"http://canal/canal/{job.run_id}/1", ticket, transport=transport)
    work = tmp_path / "pod"
    (work / "ack").mkdir(parents=True)
    (work / "artefatos").mkdir()
    event = {
        "run_id": str(job.run_id),
        "attempt": 1,
        "seq": 5,
        "at": "2026-11-22T00:00:00Z",
        "kind": "run_started",
        "step_id": None,
        "data": {},
    }
    (work / "events.jsonl").write_text(json.dumps(event) + "\n")
    (work / "result.json").write_text(json.dumps({"state": "succeeded", "artifacts": []}))
    loop = asyncio.get_running_loop()
    async with client:
        bridge = _Bridge(client, work, loop.time() + 10)
        await bridge.pump()  # 408: fica pendente para a próxima vez, sem erro
        await bridge.flush()
        await bridge.close()
        assert not bridge.pending and not bridge.over
        assert await _deliver(client, work, loop.time() + 10) == 0
    assert not slow  # as duas gravações lentas aconteceram (e receberam 408)
    assert [e.seq for e in await _wait_events(attempt, 1)][-1] == 5
    assert attempt.result_received.is_set()
    assert json.loads(attempt.result_path.read_bytes())["state"] == "succeeded"
