"""O agente do pod contra o canal de verdade, com o worker de verdade e o portal de teste.

O "pod" é um subprocesso local (``python -m rapa.runner.pod``) que a API do Kubernetes
falsa sobe quando o lançador cria o pod, com o Secret escrito numa pasta (como o
kubelet o monta). Apagar o pod manda SIGTERM ao agente (como o kubelet).
"""

import asyncio
import base64
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rapa.control.artifacts import LocalArtifactStore
from rapa.control.channel import Channel, ChannelServer
from rapa.control.kube import PodSettings
from rapa.control.launcher import KubernetesLauncher, KubernetesLauncherSettings, StaticSecretSource
from rapa.core.contracts import RunnerJob, RunState
from rapa.core.events import RunEventKind
from rapa.core.ids import new_id
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET
from rapa.fixtures.robots import emitir_protocolo
from rapa.fixtures.server import HOST_RESOLVER_ARG, RunningPortal
from rapa.runner.pod import materialize_secrets
from rapa.session.egress import EgressPolicy
from tests.control.launcher.fake_kube import FakeKube, all_bytes
from tests.control.launcher.test_launcher import INPUTS, SECRETS, RecordingIngestor


class LocalPods:
    """Sobe o agente de cada pod criado como um subprocesso e segue a fase dele."""

    def __init__(self, fake: FakeKube, root: Path, chromium: str) -> None:
        self.fake = fake
        self.root = root
        self.chromium = chromium
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.exits: list[int] = []
        self.roots: list[Path] = []
        self.leftovers: list[list[str]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        fake.on_pod = self._created
        fake.on_delete = self._deleted

    def _created(self, pod: dict[str, Any]) -> None:
        task = asyncio.get_running_loop().create_task(self._run(pod["metadata"]["name"]))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _deleted(self, pod: dict[str, Any]) -> None:
        process = self.processes.get(pod["metadata"]["name"])
        if process is not None and process.returncode is None:
            process.terminate()  # o kubelet manda SIGTERM

    async def _run(self, name: str) -> None:
        # Caminho curto, como no pod (/trabalho): o Chromium põe um socket Unix no TMPDIR.
        base = Path(tempfile.mkdtemp(prefix="pod-", dir=self.root))
        canal, trabalho, tmp = base / "canal", base / "trabalho", base / "tmp"
        home = base / "home"
        for folder in (canal, trabalho, tmp, home):
            folder.mkdir(parents=True)
        self.roots.append(base)
        for key, value in self.fake.secret_data(name).items():
            (canal / key).write_bytes(value)
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "PLAYWRIGHT_BROWSERS_PATH"}
        }
        env |= {"HOME": str(home), "RAPA_CHROMIUM_PATH": self.chromium}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "rapa.runner.pod",
            "--canal",
            str(canal),
            "--trabalho",
            str(trabalho),
            "--tmp",
            str(tmp),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.processes[name] = process
        self.fake.set_status(name, {"phase": "Running", "podIP": "127.0.0.1"})
        code = await process.wait()
        self.exits.append(code)
        # O que o agente deixou no emptyDir do trabalho e no tmpfs (deve ser nada).
        self.leftovers.append(sorted(str(p) for p in (*trabalho.iterdir(), *tmp.iterdir())))
        self.fake.set_status(name, {"phase": "Succeeded" if code == 0 else "Failed"})

    async def aclose(self) -> None:
        for process in self.processes.values():
            if process.returncode is None:
                process.kill()
                await process.wait()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for base in self.roots:
            shutil.rmtree(base, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _job(portal: RunningPortal, company_id: Any) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=company_id,
        attempt=1,
        manifest=emitir_protocolo(portal.base_url, portal.host),
        inputs=INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def _launcher(
    tmp_path: Path,
    fake: FakeKube,
    channel: Channel,
    port: int,
    ingestor: RecordingIngestor,
    company: Any,
    **changes: Any,
) -> KubernetesLauncher:
    return KubernetesLauncher(
        ingestor,
        KubernetesLauncherSettings(
            work_dir=tmp_path / "despachante" / "trabalho",
            artifacts_dir=tmp_path / "despachante" / "artefatos",
            pod=PodSettings(image="registro.exemplo/rapa-executor:1.0"),
            channel_address=f"http://127.0.0.1:{port}",
            browser_args=(HOST_RESOLVER_ARG,),
            egress=EgressPolicy(),
            status_interval_s=0.2,
            **changes,
        ),
        api=fake.api(tmp_path),
        channel=channel,
        secrets=StaticSecretSource({company: SECRETS}),
    )


@pytest.mark.browser
async def test_a_whole_run_through_the_pod_agent_and_the_channel(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    fake, channel = FakeKube(), Channel()
    pods = LocalPods(fake, Path(tempfile.gettempdir()), chromium_path)
    company = new_id()
    job = _job(portal_v1, company)
    ingestor = RecordingIngestor()
    port = _free_port()
    server = ChannelServer(channel, host="127.0.0.1", port=port)

    async def late_channel() -> None:
        # O canal só abre depois de o pod subir: o agente tenta de novo até conseguir.
        await asyncio.sleep(1.5)
        await server.start()

    opener = asyncio.create_task(late_channel())
    try:
        result = await _launcher(tmp_path, fake, channel, port, ingestor, company)(job)
    finally:
        await opener
        await server.close()
        await pods.aclose()

    assert result["state"] == RunState.SUCCEEDED, result["error_message"]
    assert len(portal_v1.state.protocolos) == 1
    assert result["outputs"] == {"protocolo": portal_v1.state.protocolos[0]["numero"]}
    # Os eventos chegaram na ordem, e o aviso irreversível foi confirmado pelo canal.
    seqs = sorted(ingestor.events)
    assert seqs == list(range(len(seqs)))
    kinds = [ingestor.events[s].kind for s in seqs]
    assert kinds[0] is RunEventKind.RUN_STARTED and kinds[-1] is RunEventKind.RUN_SUCCEEDED
    assert any(e.data.get("irreversivel") for e in ingestor.events.values())
    # Os artefatos estão na pasta local da tentativa, onde o store de sempre os aceita.
    store = LocalArtifactStore(tmp_path / "despachante" / "artefatos")
    shots = [a for a in result["artifacts"] if a["kind"] == "screenshot"]
    assert shots
    for artifact in result["artifacts"]:
        info = await store.put(
            company, job.run_id, 1, artifact["name"], artifact["kind"], Path(artifact["path"])
        )
        assert info.sha256 == artifact["sha256"] and info.size == artifact["size"]
    # O agente saiu bem, e nada ficou: nem o pod, nem o Secret, nem as pastas do pod.
    assert pods.exits == [0]
    assert fake.objects == {} and len(channel) == 0
    assert pods.leftovers == [[]]
    assert not (tmp_path / "despachante" / "trabalho" / f"{job.run_id}-1").exists()
    # Os segredos nunca foram para o disco do despachante nem para os eventos e o resultado.
    dump = json.dumps(result) + "".join(e.model_dump_json() for e in ingestor.events.values())
    files = all_bytes(tmp_path)
    for secret in (DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert secret not in dump and secret.encode() not in files


@pytest.mark.browser
async def test_the_agent_never_invents_a_confirmation(
    portal_v1: RunningPortal, chromium_path: str, tmp_path: Path
) -> None:
    fake, channel = FakeKube(), Channel()
    pods = LocalPods(fake, Path(tempfile.gettempdir()), chromium_path)
    company = new_id()
    job = _job(portal_v1, company)
    # O banco não grava o aviso irreversível: o lançador nunca confirma.
    ingestor = RecordingIngestor(refuse_irreversible=True)
    server = ChannelServer(channel, host="127.0.0.1", port=0)
    async with server.running() as port:
        try:
            launcher = _launcher(
                tmp_path, fake, channel, port, ingestor, company, ack_timeout_s=1.5
            )
            result = await launcher(job)
        finally:
            await pods.aclose()
    assert result["state"] == RunState.FAILED
    assert result["error_code"] == "sem_confirmacao"
    assert result["irreversible_started"] is False
    assert portal_v1.state.protocolos == []


def test_secrets_become_private_files_in_the_tmpfs(tmp_path: Path) -> None:
    certificate = b"\x30\x82certificado"
    path = materialize_secrets(
        {
            "a1": {
                "pfx_base64": base64.b64encode(certificate).decode(),
                "pfx_password": "senha-do-a1",
                "pfx_path": "/caminho/do/despachante.pfx",
            },
            "quebrado": {"pfx_base64": "isto não é base64!", "pfx_password": "x"},
            "portal": {"username": "ana", "password": "Tr0ub4dor&3"},
        },
        tmp_path,
    )
    data = json.loads(path.read_text())
    pfx = Path(data["a1"]["pfx_path"])
    # O certificado vira um arquivo .pfx 0600 no tmpfs; o caminho do despachante não vale.
    assert pfx.parent == tmp_path and pfx.suffix == ".pfx" and pfx.read_bytes() == certificate
    assert data["a1"] == {"pfx_password": "senha-do-a1", "pfx_path": str(pfx)}
    # Um certificado que não decodifica fica de fora inteiro (o worker falha claro).
    assert data["quebrado"] == {}
    assert data["portal"] == {"username": "ana", "password": "Tr0ub4dor&3"}
    for file in (path, pfx):
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="precisam ser texto") as caught:
        materialize_secrets({"portal": {"password": 12345678}}, tmp_path / "outra")
    assert "12345678" not in str(caught.value)
