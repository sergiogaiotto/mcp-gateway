"""Lançador de pods contra uma API do Kubernetes falsa: manifestos, fases, prazo e órfãos."""

import asyncio
import base64
import json
import socket
import ssl
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
import yaml

from rapa.control.channel import Channel
from rapa.control.kube import PodSettings, channel_address, parse_node_selector, parse_tolerations
from rapa.control.launcher import (
    EXECUTOR_NOT_STARTED,
    KubernetesLauncher,
    KubernetesLauncherSettings,
    SecretsUnavailable,
    StaticSecretSource,
    _pod_secrets,
)
from rapa.control.services.dispatcher import WorkerNotStarted
from rapa.core.contracts import RunnerJob, RunState
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo
from rapa.session.egress import EgressPolicy
from tests.control.launcher.fake_kube import FakeKube, all_bytes
from tests.control.launcher.test_launcher import INPUTS, SECRETS, RecordingIngestor
from tests.control.launcher.tls import CertificateFiles, make_certificates

IMAGE = "registro.exemplo/rapa-executor:1.0"
ADDRESS = "http://10.1.2.3:8081"


def _job(*, deadline_s: float = 120.0, attempt: int = 1) -> RunnerJob:
    return RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=attempt,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs=INPUTS,
        deadline=datetime.now(UTC) + timedelta(seconds=deadline_s),
    )


def _pod_settings(**changes: Any) -> PodSettings:
    values: dict[str, Any] = {
        "image": IMAGE,
        "service_account": "rapa-executor",
        "pull_secret": "registro",
        "node_selector": {"rapa.dev/navegadores": "sim"},
        "tolerations": ({"key": "navegadores", "operator": "Exists", "effect": "NoSchedule"},),
        **changes,
    }
    return PodSettings(**values)


def _settings(tmp_path: Path, **changes: Any) -> KubernetesLauncherSettings:
    values: dict[str, Any] = {
        "work_dir": tmp_path / "trabalho",
        "artifacts_dir": tmp_path / "artefatos",
        "pod": _pod_settings(),
        "channel_address": ADDRESS,
        "egress": EgressPolicy.parse("10.20.0.0/16", "http://proxy.interno:3128"),
        "poll_s": 0.02,
        "status_interval_s": 0.05,
        "kill_grace_s": 1.0,
        "upload_grace_s": 1.0,
        **changes,
    }
    return KubernetesLauncherSettings(**values)


def _launcher(
    tmp_path: Path, fake: FakeKube, channel: Channel, job: RunnerJob | None = None, **changes: Any
) -> KubernetesLauncher:
    secrets = StaticSecretSource({job.company_id: SECRETS}) if job is not None else None
    return KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path, **changes),
        api=fake.api(tmp_path),
        channel=channel,
        secrets=secrets,
    )


def _waiting(reason: str) -> dict[str, Any]:
    return {
        "phase": "Pending",
        "containerStatuses": [{"name": "executor", "state": {"waiting": {"reason": reason}}}],
    }


async def _fetch(channel: Channel, fake: FakeKube, pod: dict[str, Any]) -> dict[str, Any]:
    """Faz o papel do agente do pod: busca o trabalho com o bilhete do Secret."""
    data = fake.secret_data(pod["metadata"]["name"])
    address = data["endereco"].decode()
    path = address.removeprefix(ADDRESS)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=channel.app), base_url="http://canal", trust_env=False
    ) as http:
        response = await http.get(
            f"{path}/trabalho", headers={"Authorization": f"Bearer {data['bilhete'].decode()}"}
        )
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_the_pod_and_the_secret_follow_the_rules(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    seen: dict[str, Any] = {}

    def on_pod(pod: dict[str, Any]) -> None:
        seen["pod"] = json.loads(json.dumps(pod))
        seen["secret"] = json.loads(json.dumps(fake.secrets()[0]))
        seen["ticket"] = fake.secret_data(pod["metadata"]["name"])["bilhete"].decode()
        seen["attempt"] = channel.get(job.run_id, job.attempt)
        pod["status"] = _waiting("ImagePullBackOff")

    fake.on_pod = on_pod
    launcher = _launcher(tmp_path, fake, channel, job)
    result = await launcher(job)

    # A imagem não sobe: nada rodou, então é "falhou" (nunca indeterminado), com código claro.
    assert result["state"] == RunState.FAILED
    assert result["error_code"] == EXECUTOR_NOT_STARTED
    assert "ImagePullBackOff" in result["error_message"]
    pod, secret = seen["pod"], seen["secret"]
    name = f"rapa-execucao-{job.run_id}-1"
    labels = {
        "app.kubernetes.io/name": "rapa-executor",
        "app.kubernetes.io/part-of": "rapa",
        "rapa.dev/execucao": str(job.run_id),
        "rapa.dev/empresa": str(job.company_id),
        "rapa.dev/tentativa": "1",
    }
    # O Secret: o endereço do canal da tentativa e o bilhete (que vale no canal).
    assert secret["metadata"]["name"] == name and secret["metadata"]["labels"] == labels
    assert secret["immutable"] is True and secret["metadata"]["namespace"] == "rapa-execucoes"
    data = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}
    assert data["endereco"] == f"{ADDRESS}/canal/{job.run_id}/1"
    assert seen["attempt"] is not None and seen["attempt"]._ticket != data["bilhete"].encode()
    assert "Tr0ub4dor" not in json.dumps(secret) and "password" not in json.dumps(secret)
    # O pod: uso único, sem token, sem os Services, não root, só leitura, sem capacidades.
    meta, spec = pod["metadata"], pod["spec"]
    assert meta["name"] == name and meta["labels"] == labels
    assert meta["ownerReferences"] == [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "name": name,
            "uid": secret["metadata"]["uid"],
            "controller": False,
            "blockOwnerDeletion": False,
        }
    ]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["serviceAccountName"] == "rapa-executor"
    assert spec["imagePullSecrets"] == [{"name": "registro"}]
    assert spec["nodeSelector"] == {"rapa.dev/navegadores": "sim"}
    assert spec["tolerations"] == [
        {"key": "navegadores", "operator": "Exists", "effect": "NoSchedule"}
    ]
    assert spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "fsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert not spec["hostNetwork"] and not spec["hostPID"] and not spec["hostIPC"]
    # O prazo do pod: o da execução, a folga e a entrega do resultado.
    remaining = (job.deadline - datetime.now(UTC)).total_seconds()
    assert remaining + 15 < spec["activeDeadlineSeconds"] <= remaining + 15 + 2 + 3
    assert spec["terminationGracePeriodSeconds"] == 2
    (container,) = spec["containers"]
    assert container["image"] == IMAGE
    assert container["args"] == ["python", "-m", "rapa.runner.pod"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["resources"] == {
        "requests": {"cpu": "1", "memory": "2Gi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }
    assert {m["mountPath"]: m.get("readOnly", False) for m in container["volumeMounts"]} == {
        "/var/run/rapa/canal": True,
        "/tmp": False,
        "/trabalho": False,
    }
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["segredo"]["secret"] == {"secretName": name, "defaultMode": 0o440}
    assert volumes["tmp"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "64Mi"}
    assert volumes["trabalho"]["emptyDir"] == {"sizeLimit": "2Gi"}
    # Nada do plano de controle no ambiente do pod.
    assert container["env"] == [{"name": "PYTHONUNBUFFERED", "value": "1"}]
    # No fim, nada fica: nem o Secret, nem o pod, nem a tentativa no canal.
    assert fake.objects == {} and len(channel) == 0
    assert ("secrets", name) in [(p, n) for p, n, _ in fake.deleted]


async def test_without_a_runtime_class_for_kind(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    seen: list[dict[str, Any]] = []

    def on_pod(pod: dict[str, Any]) -> None:
        seen.append(json.loads(json.dumps(pod)))
        pod["status"] = _waiting("InvalidImageName")

    fake.on_pod = on_pod
    launcher = _launcher(
        tmp_path, fake, channel, job, pod=PodSettings(image=IMAGE, runtime_class=None)
    )
    result = await launcher(job)
    assert result["error_code"] == EXECUTOR_NOT_STARTED
    spec = seen[0]["spec"]
    for absent in ("runtimeClassName", "serviceAccountName", "imagePullSecrets", "nodeSelector"):
        assert absent not in spec


async def test_the_options_carry_the_guard_and_the_secrets_go_once(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    fetched: dict[str, Any] = {}

    async def fetch_then_fail(pod: dict[str, Any]) -> None:
        fetched.update(await _fetch(channel, fake, pod))
        fake.set_status(pod["metadata"]["name"], {"phase": "Failed"})

    loop = asyncio.get_running_loop()
    fake.on_pod = lambda pod: loop.create_task(fetch_then_fail(pod)) and None
    launcher = _launcher(tmp_path, fake, channel, job, browser_args=("--lang=pt-BR",))
    result = await launcher(job)

    assert fetched["opcoes"] == {
        "guarda_de_saida": True,
        "redes_internas_permitidas": ["10.20.0.0/16"],
        "proxy_saida": "http://proxy.interno:3128",
        "video": False,
        "espera_ack_s": 30.0,
        "argumentos_navegador": ["--lang=pt-BR"],
        "folga_s": 15.0,
        "arquivos": None,  # sem pasta de arquivos no despachante, como no local
    }
    assert fetched["segredos"]["portal"]["password"] == SECRETS["portal"]["password"]
    assert RunnerJob.model_validate(fetched["trabalho"]) == job
    # O pod buscou o trabalho e saiu sem resultado: pode ter rodado (o robô é irreversível).
    assert result["state"] == RunState.INDETERMINATE
    assert result["error_code"] == "executor_sem_resultado"
    # Os segredos nunca foram para o disco do despachante.
    assert b"Tr0ub4dor" not in all_bytes(tmp_path, skip=("token",))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (
            {
                "phase": "Pending",
                "conditions": [
                    {"type": "PodScheduled", "status": "False", "reason": "Unschedulable"}
                ],
            },
            "Unschedulable",
        ),
        ({"phase": "Pending"}, "Pending"),
        ({"phase": "Running"}, "não falou com o despachante"),
    ],
)
async def test_a_pod_that_does_not_start_in_time_fails_clearly(
    tmp_path: Path, status: dict[str, Any], reason: str
) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()

    def on_pod(pod: dict[str, Any]) -> None:
        pod["status"] = status

    fake.on_pod = on_pod
    launcher = _launcher(tmp_path, fake, channel, job, start_timeout_s=0.3)
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await launcher(job)
    assert loop.time() - started < 5
    assert result["state"] == RunState.FAILED and result["error_code"] == EXECUTOR_NOT_STARTED
    assert reason in result["error_message"]
    assert fake.objects == {}


async def test_a_pod_that_ends_without_fetching_the_job_never_ran(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()

    def on_pod(pod: dict[str, Any]) -> None:
        pod["status"] = {"phase": "Failed"}

    fake.on_pod = on_pod
    result = await _launcher(tmp_path, fake, channel, job)(job)
    assert result["state"] == RunState.FAILED and result["error_code"] == EXECUTOR_NOT_STARTED


async def test_at_the_deadline_the_pod_is_deleted_with_the_grace(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job(deadline_s=0.5)
    loop = asyncio.get_running_loop()

    async def fetch(pod: dict[str, Any]) -> None:
        await _fetch(channel, fake, pod)
        fake.set_status(pod["metadata"]["name"], {"phase": "Running"})

    fake.on_pod = lambda pod: loop.create_task(fetch(pod)) and None
    launcher = _launcher(tmp_path, fake, channel, job, deadline_grace_s=0.3)
    started = loop.time()
    result = await launcher(job)
    elapsed = loop.time() - started
    # O prazo (0,5 s), a folga (0,3 s) e a espera pela entrega (2 s + 5 s no máximo).
    assert 0.7 <= elapsed < 10
    name = f"rapa-execucao-{job.run_id}-1"
    pod_deletes = [options for plural, n, options in fake.deleted if plural == "pods" and n == name]
    assert pod_deletes and pod_deletes[0]["gracePeriodSeconds"] == 2
    assert result["error_code"] == "executor_sem_resultado"
    assert fake.objects == {}


async def test_a_cancelled_launch_deletes_the_pod_and_closes_the_attempt(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    created = asyncio.Event()

    def on_pod(pod: dict[str, Any]) -> None:
        pod["status"] = {"phase": "Running"}
        created.set()

    fake.on_pod = on_pod
    task = asyncio.create_task(_launcher(tmp_path, fake, channel, job)(job))
    await asyncio.wait_for(created.wait(), 5)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.objects == {} and len(channel) == 0
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()


async def test_an_api_failure_before_the_pod_is_worker_not_started(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    fake.refuse["pods"] = 403
    job = _job()
    with pytest.raises(WorkerNotStarted):
        await _launcher(tmp_path, fake, channel, job)(job)
    # O Secret que chegou a ser criado não fica para trás, nem a tentativa no canal.
    assert fake.objects == {} and len(channel) == 0
    fake.refuse = {"secrets": 403}
    with pytest.raises(WorkerNotStarted):
        await _launcher(tmp_path, fake, channel, job)(job)
    assert fake.objects == {} and len(channel) == 0


class _MissingSecrets:
    """Uma fonte de segredos (como o OpenBao) que diz que um segredo falta."""

    async def secrets_for(self, job: RunnerJob) -> dict[str, dict[str, str]]:
        raise SecretsUnavailable("a credencial 'portal' não está registrada")


class _BrokenCertificate:
    """Uma fonte que entrega um certificado A1 que não é base64."""

    async def secrets_for(self, job: RunnerJob) -> dict[str, dict[str, str]]:
        return {"a1": {"pfx_base64": "não é base64!", "pfx_password": "senha-do-a1"}}


@pytest.mark.parametrize("source", [_MissingSecrets(), _BrokenCertificate()])
async def test_a_missing_secret_fails_without_creating_the_pod(tmp_path: Path, source: Any) -> None:
    """Como no lançador local: sem o segredo, ``segredo_indisponivel`` e nenhum pod."""
    fake, channel = FakeKube(), Channel()
    job = _job()
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path),
        api=fake.api(tmp_path),
        channel=channel,
        secrets=source,
    )
    result = await launcher(job)
    assert result["state"] == RunState.FAILED
    assert result["error_code"] == "segredo_indisponivel"
    assert "não estão disponíveis" in result["error_message"]
    assert "não é base64" not in result["error_message"]  # nunca o valor
    assert fake.objects == {} and len(channel) == 0
    assert not (tmp_path / "trabalho" / f"{job.run_id}-1").exists()


def test_a_certificate_file_goes_to_the_pod_as_base64(tmp_path: Path) -> None:
    pfx = tmp_path / "certificado.pfx"
    pfx.write_bytes(b"\x30\x82conteudo-do-pfx")
    converted = _pod_secrets(
        {
            "a1": {"pfx_path": str(pfx), "pfx_password": "senha-do-a1"},
            "sumido": {"pfx_path": str(tmp_path / "nao-existe.pfx")},
            "portal": {"username": "ana"},
        }
    )
    assert converted["a1"] == {
        "pfx_base64": base64.b64encode(pfx.read_bytes()).decode(),
        "pfx_password": "senha-do-a1",
    }
    # O caminho desta máquina nunca vai para o pod; sem o arquivo, o certificado fica de fora.
    assert converted["sumido"] == {} and converted["portal"] == {"username": "ana"}


class Registry:
    def __init__(self, live: set[tuple[UUID, int]]) -> None:
        self.live = live
        self.asked: list[set[tuple[UUID, int]]] = []

    async def live_attempts(self, attempts: Collection[tuple[UUID, int]]) -> set[tuple[UUID, int]]:
        self.asked.append(set(attempts))
        return self.live & set(attempts)


def _labelled(
    name: str, run_id: UUID, attempt: int, *, app: str = "rapa-executor"
) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/name": app,
                "rapa.dev/execucao": str(run_id),
                "rapa.dev/tentativa": str(attempt),
            },
        }
    }


async def test_the_sweep_removes_only_pods_without_a_live_attempt(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    live_run, dead_run, mine_run, lonely_run = new_id(), new_id(), new_id(), new_id()
    for name, run_id in (("viva", live_run), ("morta", dead_run), ("minha", mine_run)):
        fake.add("pods", _labelled(name, run_id, 1))
        fake.add("secrets", _labelled(name, run_id, 1))
    fake.add("secrets", _labelled("sozinho", lonely_run, 2))
    fake.add("pods", _labelled("outra-coisa", dead_run, 1, app="outro-app"))
    registry = Registry({(live_run, 1)})
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path),
        api=fake.api(tmp_path),
        channel=channel,
        attempts=registry,
    )
    launcher._live.add((mine_run, 1))  # uma tentativa que este processo acompanha
    assert await launcher.sweep() == 2
    remaining = sorted((plural, name) for _, plural, name in fake.objects)
    assert remaining == [
        ("pods", "minha"),
        ("pods", "outra-coisa"),
        ("pods", "viva"),
        ("secrets", "minha"),
        ("secrets", "viva"),
    ]
    # A tentativa deste processo nem vai para o banco.
    assert all((mine_run, 1) not in asked for asked in registry.asked)
    # Sem o banco, nada que não seja deste processo é apagado.
    fake.add("pods", _labelled("morta", dead_run, 1))
    blind = KubernetesLauncher(
        RecordingIngestor(), _settings(tmp_path), api=fake.api(tmp_path), channel=channel
    )
    assert await blind.sweep() == 0
    assert ("rapa-execucoes", "pods", "morta") in fake.objects


async def test_the_sweeper_runs_at_start_and_stops(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    fake.add("pods", _labelled("morta", new_id(), 1))
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path, sweep_interval_s=30),
        api=fake.api(tmp_path),
        channel=channel,
        attempts=Registry(set()),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(launcher.run_sweeper(stop))
    for _ in range(100):
        if not fake.objects:
            break
        await asyncio.sleep(0.02)
    assert fake.objects == {}
    stop.set()
    await asyncio.wait_for(task, 2)


def test_pod_settings_are_checked() -> None:
    for changes, message in (
        ({"image": " "}, "imagem do executor"),
        ({"namespace": "Rapa_Execucoes"}, "namespace"),
        ({"cpu": "um"}, "CPU"),
        ({"memory": "0"}, "memória"),
        ({"runtime_class": "gVisor!"}, "RuntimeClass"),
    ):
        with pytest.raises(ValueError, match=message):
            _pod_settings(**changes)
    assert parse_node_selector('{"kubernetes.io/arch": "amd64"}') == {"kubernetes.io/arch": "amd64"}
    with pytest.raises(ValueError, match="seletor de nós"):
        parse_node_selector('["a"]')
    with pytest.raises(ValueError, match="tolerância"):
        parse_tolerations('[{"key": "a", "privileged": true}]')
    assert parse_tolerations('[{"key": "a", "operator": "Exists", "tolerationSeconds": 30}]') == (
        {"key": "a", "operator": "Exists", "tolerationSeconds": 30},
    )


def test_the_channel_address_points_to_this_replica() -> None:
    assert channel_address(None, 8081, resolve_ip=lambda: "10.1.2.3") == "http://10.1.2.3:8081"
    assert channel_address(None, 8081, resolve_ip=lambda: "fd00::7") == "http://[fd00::7]:8081"
    assert (
        channel_address("https://{ip}:8443/", 9, resolve_ip=lambda: "10.1.2.3")
        == "https://10.1.2.3:8443"
    )
    # O nome de um Service (o exemplo do contrato) ou um endereço fixo: com duas réplicas,
    # parte dos pods cairia na réplica que não tem a tentativa. Recusado na partida.
    for fixed in ("http://rapa-despachante-canal.rapa.svc:8081", "http://10.1.2.3:8081"):
        with pytest.raises(ValueError, match="precisa levar"):
            channel_address(fixed, 8081, resolve_ip=lambda: "10.1.2.3")
    for bad in ("ftp://{ip}:1", "http://u:s@{ip}:1", "http://{ip}:1/caminho", "http://{ip}:porta"):
        with pytest.raises(ValueError, match="canal"):
            channel_address(bad, 8081, resolve_ip=lambda: "10.1.2.3")


def test_the_local_launcher_passes_the_guard_to_the_worker(tmp_path: Path) -> None:
    from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher

    job = _job()
    plain = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(work_dir=tmp_path / "t", artifacts_dir=tmp_path / "a"),
    )
    guarded = LocalProcessLauncher(
        RecordingIngestor(),
        LocalLauncherSettings(
            work_dir=tmp_path / "t",
            artifacts_dir=tmp_path / "a",
            egress=EgressPolicy.parse("10.20.0.0/16", "http://proxy.interno:3128"),
        ),
    )
    work, artifacts = guarded._paths(job)
    assert "--guarda-de-saida" not in plain._command(job, work, artifacts, with_secrets=False)
    command = guarded._command(job, work, artifacts, with_secrets=False)
    start = command.index("--guarda-de-saida")
    assert command[start : start + 3] == [
        "--guarda-de-saida",
        "--rede-interna-permitida=10.20.0.0/16",
        "--proxy-saida=http://proxy.interno:3128",
    ]


async def test_the_kube_api_rereads_the_token_and_never_follows_redirects(tmp_path: Path) -> None:
    from rapa.control.kube import KubeApi, KubeConfig, KubeError

    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/desvio"):
            return httpx2.Response(302, headers={"Location": "https://outro.exemplo/"})
        return httpx2.Response(
            403, json={"kind": "Status", "reason": "Forbidden", "message": "segredo Tr0ub4dor"}
        )

    token = tmp_path / "token"
    token.write_text("primeiro\n")
    api = KubeApi(
        KubeConfig(base_url="https://kube.test", token_path=token),
        transport=httpx2.MockTransport(handle),
    )
    with pytest.raises(KubeError) as caught:
        await api.get("rapa-execucoes", "pods", "x")
    # Só o código e o motivo: nunca a mensagem (que poderia ter dados).
    assert str(caught.value) == "a API do Kubernetes respondeu 403 (Forbidden)"
    token.write_text("segundo")  # o token projetado foi trocado
    with pytest.raises(KubeError):
        await api.get("rapa-execucoes", "pods", "desvio")
    assert seen == ["Bearer primeiro", "Bearer segundo"]
    await api.aclose()


async def test_the_result_points_to_the_local_files_and_extras_are_dropped(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()

    async def pod_side(pod: dict[str, Any]) -> None:
        data = fake.secret_data(pod["metadata"]["name"])
        path = data["endereco"].decode().removeprefix(ADDRESS)
        headers = {"Authorization": f"Bearer {data['bilhete'].decode()}"}
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=channel.app), base_url="http://canal"
        ) as http:
            await http.get(f"{path}/trabalho", headers=headers)
            for name in ("tela-001.png", "intruso.bin"):
                response = await http.put(
                    f"{path}/artefatos/{name}", content=b"dados", headers=headers
                )
                assert response.status_code == 201
            body = {
                "run_id": str(job.run_id),
                "attempt": 1,
                "state": "succeeded",
                "outputs": {"protocolo": "1"},
                "error_code": None,
                "error_message": None,
                "failed_step_id": None,
                "irreversible_started": True,
                "steps_done": 3,
                "artifacts": [
                    {
                        "kind": "screenshot",
                        "name": "tela-001.png",
                        "path": "/trabalho/tentativa/artefatos/tela-001.png",
                        "size": 5,
                        "sha256": "0" * 64,
                    }
                ],
            }
            response = await http.put(
                f"{path}/resultado", content=json.dumps(body), headers=headers
            )
            assert response.status_code == 204

    loop = asyncio.get_running_loop()
    fake.on_pod = lambda pod: loop.create_task(pod_side(pod)) and None
    launcher = _launcher(tmp_path, fake, channel, job)
    result = await launcher(job)
    assert result["state"] == RunState.SUCCEEDED
    folder = tmp_path / "artefatos" / str(job.company_id) / str(job.run_id) / "1"
    assert result["artifacts"][0]["path"] == str(folder / "tela-001.png")
    # O que o pod mandou e o resultado não cita nunca vira evidência.
    assert sorted(p.name for p in folder.iterdir()) == ["tela-001.png"]


def _result_body(job: RunnerJob, artifacts: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {
            "run_id": str(job.run_id),
            "attempt": job.attempt,
            "state": "succeeded",
            "outputs": {"protocolo": "1"},
            "error_code": None,
            "error_message": None,
            "failed_step_id": None,
            "irreversible_started": True,
            "steps_done": 3,
            "artifacts": artifacts or [],
        }
    )


def _pod_client(
    channel: Channel, fake: FakeKube, pod: dict[str, Any]
) -> tuple[httpx2.AsyncClient, str]:
    """O cliente do "agente" falando com o canal pelo endereço e bilhete do Secret."""
    data = fake.secret_data(pod["metadata"]["name"])
    path = data["endereco"].decode().removeprefix(ADDRESS)
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=channel.app),
        base_url="http://canal",
        headers={"Authorization": f"Bearer {data['bilhete'].decode()}"},
        trust_env=False,
    )
    return http, path


async def test_the_attempt_is_closed_before_the_result_is_decided(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    fake.on_pod = lambda pod: pod.update(status={"phase": "Pending"})
    launcher = _launcher(tmp_path, fake, channel, job, start_timeout_s=0.2)
    seen: dict[str, Any] = {}
    decide = launcher._result

    def spy(*args: Any) -> dict[str, Any]:
        attempt = args[3]
        # Na hora de dizer "nada rodou", o pod já não consegue buscar o trabalho (nem os
        # segredos): a tentativa fechou no canal antes, sem espera no meio.
        seen["closed"] = attempt.closed
        seen["open"] = channel.get(job.run_id, job.attempt) is not None
        seen["payload"] = attempt._payload
        return decide(*args)

    launcher._result = spy  # type: ignore[method-assign]
    result = await launcher(job)
    assert result["error_code"] == EXECUTOR_NOT_STARTED and result["state"] == RunState.FAILED
    assert seen == {"closed": True, "open": False, "payload": None}


async def test_uploads_in_flight_stop_before_unlisted_files_are_dropped(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    statuses: dict[str, int] = {}
    gate = asyncio.Event()
    folder = tmp_path / "artefatos" / str(job.company_id) / str(job.run_id) / "1"

    async def slow() -> AsyncIterator[bytes]:
        yield b"x" * 1000
        await gate.wait()  # o pod segura o envio aberto depois do resultado
        yield b"y" * 1000

    async def pod_side(pod: dict[str, Any]) -> None:
        http, path = _pod_client(channel, fake, pod)
        async with http:
            await http.get(f"{path}/trabalho")
            upload = asyncio.create_task(http.put(f"{path}/artefatos/intruso.bin", content=slow()))
            for _ in range(500):
                if (folder / "intruso.bin").exists():
                    break
                await asyncio.sleep(0.01)
            response = await http.put(f"{path}/resultado", content=_result_body(job))
            statuses["resultado"] = response.status_code
            statuses["intruso"] = (await upload).status_code

    loop = asyncio.get_running_loop()
    tasks: list[asyncio.Task[None]] = []
    fake.on_pod = lambda pod: tasks.append(loop.create_task(pod_side(pod)))
    launcher = _launcher(tmp_path, fake, channel, job)
    at_decision: dict[str, Any] = {}
    decide = launcher._result

    def spy(*args: Any) -> dict[str, Any]:
        attempt = args[3]
        at_decision.update(closed=attempt.closed, active=attempt.active)
        return decide(*args)

    launcher._result = spy  # type: ignore[method-assign]
    started = loop.time()
    result = await launcher(job)
    await asyncio.gather(*tasks)
    assert result["state"] == RunState.SUCCEEDED
    # O envio que estava no meio acabou no fechamento, antes da decisão e da limpeza do
    # que o resultado não cita (e sem esperar o prazo do pedido).
    assert at_decision == {"closed": True, "active": 0}
    assert loop.time() - started < 8
    assert statuses == {"resultado": 204, "intruso": 410}
    assert list(folder.iterdir()) == []


async def test_without_a_result_nothing_the_pod_sent_stays(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    folder = tmp_path / "artefatos" / str(job.company_id) / str(job.run_id) / "1"

    async def pod_side(pod: dict[str, Any]) -> None:
        http, path = _pod_client(channel, fake, pod)
        async with http:
            await http.get(f"{path}/trabalho")
            response = await http.put(f"{path}/artefatos/tela-001.png", content=b"png")
            assert response.status_code == 201
        fake.set_status(pod["metadata"]["name"], {"phase": "Failed"})

    loop = asyncio.get_running_loop()
    fake.on_pod = lambda pod: loop.create_task(pod_side(pod)) and None
    result = await _launcher(tmp_path, fake, channel, job)(job)
    assert result["error_code"] == "executor_sem_resultado"
    assert list(folder.iterdir()) == []


async def test_an_api_refusal_is_executor_nao_iniciou_with_the_reason(tmp_path: Path) -> None:
    from rapa.control.services.dispatcher import Dispatcher
    from tests.control.db.test_dispatcher import _FlakyQueue

    fake, channel = FakeKube(), Channel()
    fake.refuse["pods"] = 403  # a cota, a RuntimeClass que falta, a permissão
    job = _job()
    launcher = _launcher(tmp_path, fake, channel, job)
    with pytest.raises(WorkerNotStarted) as caught:
        await launcher(job)
    assert caught.value.code == EXECUTOR_NOT_STARTED
    assert "Forbidden" in caught.value.public_message
    assert "nenhum passo foi executado" in caught.value.public_message
    # Pelo despachante: o mesmo código das outras falhas de pod que nunca rodou.
    queue = _FlakyQueue()
    queue.calls["claim"] = 3
    queue.job = job
    dispatcher = Dispatcher(queue, launcher, heartbeat_interval=0.01)  # type: ignore[arg-type]
    assert await dispatcher.run_once() == 1
    await dispatcher.drain()
    (finished,) = queue.finished
    assert finished["state"] == "failed" and finished["error_code"] == EXECUTOR_NOT_STARTED
    assert "Forbidden" in finished["error_message"]
    assert finished["irreversible_started"] is False


def _file_job(company: UUID, inputs: dict[str, Any]) -> RunnerJob:
    from rapa.core.robot import Manifest

    manifest = Manifest.model_validate(
        {
            "name": "enviar_documento",
            "title": "Enviar documento",
            "description": "Envia um arquivo.",
            "allowed_domains": ["portal-v1.rapa.test"],
            "inputs": [
                {"name": "documento", "title": "Documento", "type": "file"},
                {"name": "anexo", "title": "Anexo", "type": "file", "required": False},
                {"name": "fora", "title": "Fora", "type": "file", "required": False},
                {"name": "nome", "title": "Nome", "type": "string", "required": False},
            ],
            "steps": [
                {
                    "kind": "navigate",
                    "id": "abrir",
                    "title": "Abrir",
                    "url": "http://portal-v1.rapa.test:8080/arquivos",
                },
                {
                    "kind": "upload",
                    "id": "escolher",
                    "title": "Escolher o arquivo",
                    "target": {
                        "intent": "campo",
                        "locators": [{"by": "label", "value": "Arquivo"}],
                    },
                    "file_input": "documento",
                },
            ],
        }
    )
    return RunnerJob(
        run_id=new_id(),
        company_id=company,
        attempt=1,
        manifest=manifest,
        inputs=inputs,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


async def test_input_files_go_to_the_pod_through_the_channel(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    company = new_id()
    files = tmp_path / "arquivos"
    (files / str(company)).mkdir(parents=True)
    (files / str(company) / "nota.pdf").write_bytes(b"%PDF conteudo")
    (files / str(company) / "sem-uso.pdf").write_bytes(b"outro")
    (files / str(new_id())).mkdir()
    (files / "solto.pdf").write_bytes(b"na raiz")
    job = _file_job(
        company,
        {
            "documento": "nota.pdf",
            "anexo": "../solto.pdf",  # fora da pasta da empresa
            "fora": "nao-existe.pdf",
            "nome": "sem-uso.pdf",  # não é uma entrada de arquivo
        },
    )
    seen: dict[str, Any] = {}

    async def pod_side(pod: dict[str, Any]) -> None:
        http, path = _pod_client(channel, fake, pod)
        async with http:
            body = (await http.get(f"{path}/trabalho")).json()
            seen["arquivos"] = body["opcoes"]["arquivos"]
            got = await http.get(f"{path}/arquivos/nota.pdf")
            seen["nota"] = (got.status_code, got.content)
            for name in ("sem-uso.pdf", "solto.pdf", "nao-existe.pdf"):
                seen[name] = (await http.get(f"{path}/arquivos/{name}")).status_code
            await http.put(f"{path}/resultado", content=_result_body(job))

    loop = asyncio.get_running_loop()
    fake.on_pod = lambda pod: loop.create_task(pod_side(pod)) and None
    launcher = _launcher(tmp_path, fake, channel, job, files_dir=files)
    result = await launcher(job)
    assert result["state"] == RunState.SUCCEEDED
    # Só o arquivo que uma entrada de arquivo cita, na pasta da empresa da tarefa.
    assert seen["arquivos"] == ["nota.pdf"]
    assert seen["nota"] == (200, b"%PDF conteudo")
    assert seen["sem-uso.pdf"] == seen["solto.pdf"] == seen["nao-existe.pdf"] == 404
    # Sem pasta de arquivos no despachante, o pod também fica sem (como o worker local).
    assert launcher._input_files(job) == {"nota.pdf": (files / str(company) / "nota.pdf")}
    no_files = _launcher(tmp_path, fake, channel, job)
    assert no_files._input_files(job) is None and no_files._options().files is None


async def test_the_launcher_wakes_the_ack_waits_of_its_attempt(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    job = _job()
    launcher = _launcher(tmp_path, fake, channel, job)
    attempt, _ = channel.open(
        job, work_dir=tmp_path / "w", artifacts_dir=tmp_path / "a", secrets={}, options={}
    )
    signal = attempt._ack_signal
    launcher._acknowledged(job)
    assert signal.is_set() and attempt._ack_signal is not signal
    launcher._acknowledged(_job())  # outra tentativa: nada a acordar


async def test_the_sweep_removes_objects_without_a_readable_attempt(tmp_path: Path) -> None:
    fake, channel = FakeKube(), Channel()
    run_id = new_id()
    fake.add("pods", _labelled("rotulo-ruim", run_id, 1))
    fake.objects[("rapa-execucoes", "pods", "rotulo-ruim")]["metadata"]["labels"][
        "rapa.dev/execucao"
    ] = "nao-e-uuid"
    fake.add(
        "secrets",
        {
            "metadata": {
                "name": "sem-tentativa",
                "labels": {"app.kubernetes.io/name": "rapa-executor"},
            }
        },
    )
    fake.add("pods", _labelled("viva", run_id, 1))
    registry = Registry({(run_id, 1)})
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path),
        api=fake.api(tmp_path),
        channel=channel,
        attempts=registry,
    )
    # Nenhum lançador cria um objeto de execução sem tentativa legível: ele nunca é de uma
    # tentativa viva e não pode ficar para sempre.
    assert await launcher.sweep() == 2
    assert sorted(name for _, _, name in fake.objects) == ["viva"]


async def test_the_sweep_leaves_the_egress_test_pod_long_enough_to_run(tmp_path: Path) -> None:
    """O pod de ``deploy/rede/teste-de-saida.yaml`` (OPERACAO 1.9) tem o rótulo de execução
    e nenhuma tentativa: a varredura do despachante (a cada minuto, em cada réplica) não
    pode apagá-lo no meio da conferência; só um que ficou para trás sai."""
    manifest = yaml.safe_load(
        (Path(__file__).parents[3] / "deploy" / "rede" / "teste-de-saida.yaml").read_text()
    )
    fake, channel = FakeKube(), Channel()
    now = datetime.now(UTC)
    for name, age in (("teste-de-saida", timedelta(seconds=20)), ("esquecido", timedelta(hours=1))):
        pod = json.loads(json.dumps(manifest))
        pod["metadata"]["name"] = name
        pod["metadata"]["creationTimestamp"] = (now - age).strftime("%Y-%m-%dT%H:%M:%SZ")
        fake.add("pods", pod)
    launcher = KubernetesLauncher(
        RecordingIngestor(),
        _settings(tmp_path),
        api=fake.api(tmp_path),
        channel=channel,
        attempts=Registry(set()),
    )
    assert await launcher.sweep() == 1
    assert sorted(name for _, _, name in fake.objects) == ["teste-de-saida"]


def test_the_pod_ip_prefers_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    from rapa.control import kube

    answers: list[tuple[Any, ...]] = []

    def getaddrinfo(*_: Any, **__: Any) -> list[tuple[Any, ...]]:
        return answers

    monkeypatch.setattr(kube.socket, "getaddrinfo", getaddrinfo)

    def info(address: str) -> tuple[Any, ...]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return (family, socket.SOCK_STREAM, 6, "", (address, 0))

    # Num nó com as duas pilhas, o IPv6 pode vir primeiro: o IPv4 vale.
    answers[:] = [info("127.0.0.1"), info("fe80::1"), info("fd00::7"), info("10.1.2.3")]
    assert kube._own_address("pod") == "10.1.2.3"
    # Só IPv6 (fora do loopback e do link-local): ele vale.
    answers[:] = [info("::1"), info("fe80::1"), info("fd00::7")]
    assert kube._own_address("pod") == "fd00::7"
    answers[:] = [info("127.0.0.1"), info("::1")]
    with pytest.raises(ValueError, match="IP deste pod"):
        kube._own_address("pod")


@asynccontextmanager
async def _tls_api_server(certs: CertificateFiles) -> AsyncIterator[tuple[int, list[bytes]]]:
    """Uma "API do Kubernetes" em HTTPS de verdade, com o certificado do CA de teste."""
    heads: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            heads.append(await reader.readuntil(b"\r\n\r\n"))
            body = json.dumps({"kind": "Pod", "status": {"phase": "Running"}}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except OSError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(str(certs.cert), str(certs.key))
    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
    try:
        yield int(server.sockets[0].getsockname()[1]), heads
    finally:
        server.close()
        await server.wait_closed()


async def test_the_kube_api_checks_tls_with_the_cluster_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapa.control.kube import KubeApi, KubeConfig

    certs = make_certificates(tmp_path / "cluster")
    stranger = make_certificates(tmp_path / "outro")
    token = tmp_path / "token"
    token.write_text("token-da-conta")
    # Um proxy no ambiente nunca vale para a API (e mataria a conexão se valesse).
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    async with _tls_api_server(certs) as (port, heads):
        base = f"https://127.0.0.1:{port}"
        api = KubeApi(KubeConfig(base_url=base, token_path=token, ca_path=certs.ca))
        try:
            pod = await api.get("rapa-execucoes", "pods", "x")
        finally:
            await api.aclose()
        assert pod == {"kind": "Pod", "status": {"phase": "Running"}}
        assert b"authorization: bearer token-da-conta" in heads[0].lower()
        # Com outro CA, o certificado da "API" não confere: nada passa.
        api = KubeApi(KubeConfig(base_url=base, token_path=token, ca_path=stranger.ca))
        try:
            with pytest.raises(httpx2.ConnectError):
                await api.get("rapa-execucoes", "pods", "x")
        finally:
            await api.aclose()
        assert len(heads) == 1  # o pedido com o token nunca saiu para quem não conferiu
    # Sem o CA do cluster, o cliente nem sobe (nunca os CAs do sistema no lugar dele).
    with pytest.raises(ValueError, match="CA do cluster"):
        KubeApi(KubeConfig(base_url="https://kube.test", ca_path=tmp_path / "nao-existe.crt"))
    (tmp_path / "quebrado.crt").write_text("isto não é um certificado")
    with pytest.raises(ValueError, match="CA do cluster"):
        KubeApi(KubeConfig(base_url="https://kube.test", ca_path=tmp_path / "quebrado.crt"))
