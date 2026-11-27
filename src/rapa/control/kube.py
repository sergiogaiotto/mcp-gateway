"""Cliente mínimo da API do Kubernetes e os manifestos dos pods de uso único.

O despachante (as execuções, :class:`rapa.control.launcher.KubernetesLauncher`) e a
API (as sessões de gravação, :class:`rapa.control.sessions.launcher.KubernetesSessionLauncher`)
criam um Secret e um Pod por tentativa ou sessão no namespace das execuções
(``RAPA_K8S_NAMESPACE_EXECUCOES``). Sem cliente pesado: :class:`KubeApi` fala com a API
por ``httpx2`` com a configuração do próprio pod (o token da conta de serviço, relido a
cada pedido porque o token projetado é trocado; o CA do cluster, que confere o TLS da
API, sem cair nos CAs do sistema: sem ele, nada sobe; e
``KUBERNETES_SERVICE_HOST``/``KUBERNETES_SERVICE_PORT``), sem redirecionamento, sem as
variáveis de proxy do ambiente e com prazos curtos. Os erros levam só o código HTTP e o motivo do ``Status`` do Kubernetes
(``AlreadyExists``, ``Forbidden``), nunca o corpo.

Os pods (:func:`pod_manifest`) seguem as mesmas regras de segurança: imagem
``RAPA_K8S_IMAGEM_EXECUTOR``, ``runtimeClassName`` de ``RAPA_K8S_RUNTIME_CLASS``
(padrão ``gvisor``), ``restartPolicy: Never``, sem token da conta de serviço montado,
sem as variáveis dos Services, usuário não root (10001), sistema de arquivos só de
leitura, sem capacidades, sem escalar privilégio, seccomp ``RuntimeDefault``,
``emptyDir`` com limite para ``/tmp`` (em memória) e para o trabalho, limites de CPU e
memória, prazo (``activeDeadlineSeconds``) e, como dono (``ownerReference``), o Secret
da tentativa: apagar o Secret apaga o pod junto.
"""

import base64
import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

__all__ = [
    "COMPONENT_LABEL",
    "EXECUTOR_APP",
    "LABEL_APP",
    "LABEL_ATTEMPT",
    "LABEL_COMPANY",
    "LABEL_RUN",
    "LABEL_SESSION",
    "SESSION_APP",
    "KubeApi",
    "KubeConfig",
    "KubeError",
    "PodSettings",
    "PodState",
    "channel_address",
    "parse_node_selector",
    "parse_quantity",
    "parse_tolerations",
    "pod_manifest",
    "pod_state",
    "secret_manifest",
]

logger = logging.getLogger("rapa.control.kube")

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LABEL_APP = "app.kubernetes.io/name"
COMPONENT_LABEL = "app.kubernetes.io/part-of"
LABEL_RUN = "rapa.dev/execucao"
LABEL_COMPANY = "rapa.dev/empresa"
LABEL_ATTEMPT = "rapa.dev/tentativa"
LABEL_SESSION = "rapa.dev/sessao"
#: Rótulo ``app.kubernetes.io/name`` dos pods de execução (as NetworkPolicies o usam).
EXECUTOR_APP = "rapa-executor"
#: Rótulo ``app.kubernetes.io/name`` dos pods das sessões de gravação.
SESSION_APP = "rapa-sessao"
#: Usuário (e grupo) dos contêineres: o da imagem, nunca root.
RUN_AS_USER = 10001

#: Motivos de espera de um contêiner que nunca vão sair sozinhos (a imagem, a configuração).
HARD_WAITING_REASONS = frozenset(
    {
        "ImagePullBackOff",
        "InvalidImageName",
        "ErrImageNeverPull",
        "CreateContainerConfigError",
        "CreateContainerError",
    }
)

_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|k|Ki|M|Mi|G|Gi|T|Ti)?$")
_LABEL_KEY = re.compile(
    r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$"
)
_LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
_DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_TOLERATION_KEYS = frozenset({"key", "operator", "value", "effect", "tolerationSeconds"})


class KubeError(RuntimeError):
    """A API do Kubernetes recusou o pedido (a mensagem só tem o código e o motivo)."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"a API do Kubernetes respondeu {status} ({reason})")
        self.status = status
        self.reason = reason


def parse_quantity(value: str, what: str) -> str:
    """Uma quantidade do Kubernetes (``1``, ``500m``, ``2Gi``)."""
    text = value.strip()
    if not _QUANTITY.fullmatch(text) or float(re.sub(r"[A-Za-z]+$", "", text)) <= 0:
        raise ValueError(f"{what} precisa ser uma quantidade do Kubernetes, como 1, 500m ou 2Gi")
    return text


def _dns_name(value: str, what: str) -> str:
    text = value.strip()
    if not _DNS_NAME.fullmatch(text):
        raise ValueError(f"{what} precisa ser um nome do Kubernetes (minúsculas, números e -)")
    return text


def parse_node_selector(raw: str | Mapping[str, Any] | None) -> dict[str, str]:
    """O ``nodeSelector`` dos pods (JSON ``{"rótulo": "valor"}``)."""
    if raw is None or raw == "":
        return {}
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, Mapping):
        raise ValueError('o seletor de nós precisa ser um objeto JSON {"rótulo": "valor"}')
    out: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("o seletor de nós só aceita rótulos e valores de texto")
        if not _LABEL_KEY.fullmatch(key) or not _LABEL_VALUE.fullmatch(value):
            raise ValueError(f"rótulo inválido no seletor de nós: '{key}'")
        out[key] = value
    return out


def parse_tolerations(raw: str | Sequence[Any] | None) -> tuple[dict[str, Any], ...]:
    """As ``tolerations`` dos pods (JSON, uma lista como a do Kubernetes)."""
    if raw is None or raw == "":
        return ()
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, list):
        raise ValueError("as tolerâncias precisam ser uma lista JSON, como no Kubernetes")
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping) or not set(item) <= _TOLERATION_KEYS:
            raise ValueError(
                "cada tolerância só aceita key, operator, value, effect e tolerationSeconds"
            )
        for key, value in item.items():
            ok = isinstance(value, int) if key == "tolerationSeconds" else isinstance(value, str)
            if not ok or isinstance(value, bool):
                raise ValueError(f"o campo '{key}' de uma tolerância tem o tipo errado")
        out.append(dict(item))
    return tuple(out)


def _own_address(hostname: str | None = None) -> str:
    """O IP deste pod (o Kubernetes põe o nome do pod no ``/etc/hosts`` com o IP dele).

    O IPv4 vem antes: num nó com as duas pilhas, ``getaddrinfo`` pode responder primeiro o
    IPv6. Um IPv6 só vale se não houver IPv4 (e nunca o link-local, que precisa de zona);
    o canal escuta nas duas pilhas quando o sistema deixa (:class:`rapa.control.channel.ChannelServer`).
    """
    name = hostname or socket.gethostname()
    try:
        infos = socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except OSError:
        infos = []
    found: dict[int, str] = {}
    for info in infos:
        address = str(info[4][0])
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            continue
        found.setdefault(ip.version, str(ip))
    for version in (4, 6):
        if version in found:
            return found[version]
    raise ValueError(
        "não foi possível descobrir o IP deste pod para o endereço do canal "
        "(RAPA_CANAL_ENDERECO com {ip})"
    )


def channel_address(
    template: str | None, port: int, *, resolve_ip: Callable[[], str] | None = None
) -> str:
    """O endereço do canal que vai para os pods (sem barra no fim).

    O padrão é ``http://{ip}:<porta>``, e ``{ip}`` vira o IP deste pod: o bilhete e a
    pasta de trabalho de cada tentativa ficam na réplica do despachante que a lançou,
    então o pod precisa falar com ela, não com qualquer réplica atrás de um Service. Por
    isso um endereço sem ``{ip}`` (o nome de um Service, como
    ``http://rapa-despachante-canal.rapa.svc:8081``) é recusado na partida: com duas
    réplicas, parte dos pods cairia na réplica errada e falharia sem rodar.
    """
    raw = (template or f"http://{{ip}}:{port}").strip()
    if "{ip}" not in raw:
        raise ValueError(
            "o endereço do canal precisa levar {ip} no lugar do host (ex. http://{ip}:8081): "
            "cada pod fala com a réplica do despachante que o lançou, e o nome de um "
            "Service mandaria parte deles para outra réplica"
        )
    ip = (resolve_ip or _own_address)()
    raw = raw.replace("{ip}", f"[{ip}]" if ":" in ip else ip)
    try:
        split = urlsplit(raw)
        split.port  # noqa: B018 - confere a porta
    except ValueError:
        raise ValueError("o endereço do canal precisa ser http://host:porta") from None
    if split.scheme not in ("http", "https") or not split.hostname:
        raise ValueError("o endereço do canal precisa ser http://host:porta")
    if split.username is not None or split.password is not None:
        raise ValueError("o endereço do canal não pode ter usuário e senha")
    if split.path not in ("", "/") or split.query or split.fragment:
        raise ValueError("o endereço do canal é só http://host:porta, sem caminho")
    return raw.rstrip("/")


@dataclass(frozen=True)
class KubeConfig:
    """Onde está a API e com que credencial falar com ela."""

    base_url: str
    token_path: Path | None = None
    ca_path: Path | None = None

    @classmethod
    def in_cluster(
        cls, env: Mapping[str, str] | None = None, service_account: Path | None = None
    ) -> KubeConfig:
        """A configuração do pod: a conta de serviço montada e o Service ``kubernetes``."""
        env = os.environ if env is None else env
        service_account = SERVICE_ACCOUNT_DIR if service_account is None else service_account
        host = env.get("KUBERNETES_SERVICE_HOST", "").strip()
        port = env.get("KUBERNETES_SERVICE_PORT", "443").strip() or "443"
        if not host:
            raise ValueError(
                "sem KUBERNETES_SERVICE_HOST: o modo kubernetes precisa rodar num pod do cluster"
            )
        if not port.isdigit():
            raise ValueError("KUBERNETES_SERVICE_PORT precisa ser um número")
        name = f"[{host}]" if ":" in host else host
        return cls(
            base_url=f"https://{name}:{port}",
            token_path=service_account / "token",
            ca_path=service_account / "ca.crt",
        )


class KubeApi:
    """O pouco da API do Kubernetes que os lançadores usam (pods e secrets)."""

    def __init__(
        self,
        config: KubeConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.config = config
        verify: ssl.SSLContext | bool = True
        if transport is None and config.ca_path is not None:
            # O CA do cluster confere a API; sem ele, nada sobe (nunca os CAs do sistema).
            if not config.ca_path.is_file():
                raise ValueError(
                    "o CA do cluster não foi encontrado (o ca.crt da conta de serviço montada)"
                )
            try:
                verify = ssl.create_default_context(cafile=str(config.ca_path))
            except OSError, ssl.SSLError:
                raise ValueError("o CA do cluster não pôde ser lido (o ca.crt)") from None
        self._http = httpx2.AsyncClient(
            base_url=config.base_url,
            timeout=httpx2.Timeout(timeout_s, connect=min(5.0, timeout_s)),
            follow_redirects=False,
            trust_env=False,
            verify=verify,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"KubeApi({self.config.base_url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        path = self.config.token_path
        if path is None:
            return {}
        try:
            # Relido a cada pedido: o token projetado da conta de serviço é trocado.
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        response = await self._http.request(
            method,
            path,
            json=dict(body) if body is not None else None,
            params=dict(params) if params else None,
            headers=self._headers(),
        )
        if missing_ok and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise KubeError(response.status_code, _reason(response))
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _path(namespace: str, plural: str, name: str | None = None) -> str:
        base = f"/api/v1/namespaces/{namespace}/{plural}"
        return f"{base}/{name}" if name else base

    async def create(self, namespace: str, plural: str, body: Mapping[str, Any]) -> dict[str, Any]:
        created = await self._request("POST", self._path(namespace, plural), body=body)
        return created or {}

    async def get(self, namespace: str, plural: str, name: str) -> dict[str, Any] | None:
        return await self._request("GET", self._path(namespace, plural, name), missing_ok=True)

    async def delete(
        self, namespace: str, plural: str, name: str, *, grace_s: int | None = None
    ) -> bool:
        """Apaga (em segundo plano: o coletor apaga o que for do objeto); ``False`` se não existia."""
        options: dict[str, Any] = {
            "kind": "DeleteOptions",
            "apiVersion": "v1",
            "propagationPolicy": "Background",
        }
        if grace_s is not None:
            options["gracePeriodSeconds"] = max(0, int(grace_s))
        done = await self._request(
            "DELETE", self._path(namespace, plural, name), body=options, missing_ok=True
        )
        return done is not None

    async def list(
        self, namespace: str, plural: str, *, label_selector: str
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", self._path(namespace, plural), params={"labelSelector": label_selector}
        )
        items = (data or {}).get("items")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _reason(response: httpx2.Response) -> str:
    """O motivo do ``Status`` do Kubernetes (``AlreadyExists``), nunca a mensagem inteira."""
    with contextlib.suppress(ValueError):
        data = response.json()
        if isinstance(data, dict):
            reason = data.get("reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Za-z]{1,64}", reason):
                return reason
    return "sem motivo"


@dataclass(frozen=True)
class PodSettings:
    """Como os pods de uso único rodam (``RAPA_K8S_*``)."""

    image: str
    namespace: str = "rapa-execucoes"
    #: ``None`` só para kind e desenvolvimento (sem gVisor).
    runtime_class: str | None = "gvisor"
    cpu: str = "1"
    memory: str = "2Gi"
    #: A conta de serviço dos pods (sem permissão nenhuma e sem token montado).
    service_account: str | None = None
    pull_secret: str | None = None
    node_selector: Mapping[str, str] = field(default_factory=dict)
    tolerations: tuple[Mapping[str, Any], ...] = ()
    #: Limite do ``emptyDir`` do trabalho (pastas da tentativa, artefatos e o perfil).
    work_size: str = "2Gi"
    #: Limite do ``emptyDir`` em memória de ``/tmp`` (os segredos e o certificado A1).
    tmp_size: str = "64Mi"

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("informe a imagem do executor (RAPA_K8S_IMAGEM_EXECUTOR)")
        _dns_name(self.namespace, "o namespace das execuções")
        if self.runtime_class is not None:
            _dns_name(self.runtime_class, "a RuntimeClass")
        if self.service_account is not None:
            _dns_name(self.service_account, "a conta de serviço dos pods")
        if self.pull_secret is not None:
            _dns_name(self.pull_secret, "o Secret da imagem")
        parse_quantity(self.cpu, "a CPU dos pods")
        parse_quantity(self.memory, "a memória dos pods")
        parse_quantity(self.work_size, "o tamanho do trabalho")
        parse_quantity(self.tmp_size, "o tamanho do /tmp")


def secret_manifest(
    settings: PodSettings, *, name: str, labels: Mapping[str, str], data: Mapping[str, bytes]
) -> dict[str, Any]:
    """O Secret da tentativa ou da sessão (imutável; os valores em base64)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": settings.namespace, "labels": dict(labels)},
        "type": "Opaque",
        "immutable": True,
        "data": {key: base64.b64encode(value).decode("ascii") for key, value in data.items()},
    }


def _owner(secret: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = secret.get("metadata") or {}
    name, uid = metadata.get("name"), metadata.get("uid")
    if not isinstance(name, str) or not isinstance(uid, str) or not uid:
        raise ValueError("o Secret criado não tem nome e uid")
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "name": name,
            "uid": uid,
            "controller": False,
            "blockOwnerDeletion": False,
        }
    ]


def pod_manifest(
    settings: PodSettings,
    *,
    name: str,
    labels: Mapping[str, str],
    owner: Mapping[str, Any],
    args: Sequence[str],
    mount_path: str,
    active_deadline_s: float,
    termination_grace_s: float,
    ports: Sequence[Mapping[str, Any]] = (),
    readiness: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """O pod de uso único, com as regras de segurança de todos (ver o módulo).

    ``args`` vão depois do ``ENTRYPOINT`` da imagem (o ``tini``). O Secret do dono
    fica montado só para leitura em ``mount_path``. ``env`` acrescenta variáveis (nunca
    um segredo: elas ficam no manifesto do pod).
    """
    secret_name = str((owner.get("metadata") or {}).get("name"))
    container_security = {
        "allowPrivilegeEscalation": False,
        "privileged": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": RUN_AS_USER,
        "runAsGroup": RUN_AS_USER,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    resources = {"cpu": settings.cpu, "memory": settings.memory}
    container: dict[str, Any] = {
        "name": "executor",
        "image": settings.image,
        "imagePullPolicy": "IfNotPresent",
        "args": list(args),
        "env": [
            {"name": "PYTHONUNBUFFERED", "value": "1"},
            *({"name": key, "value": value} for key, value in (env or {}).items()),
        ],
        "securityContext": container_security,
        "resources": {"requests": dict(resources), "limits": dict(resources)},
        "volumeMounts": [
            {"name": "segredo", "mountPath": mount_path, "readOnly": True},
            {"name": "tmp", "mountPath": "/tmp"},  # noqa: S108 - emptyDir em memória do pod
            {"name": "trabalho", "mountPath": "/trabalho"},
        ],
    }
    if ports:
        container["ports"] = [dict(port) for port in ports]
    if readiness is not None:
        container["readinessProbe"] = dict(readiness)
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "activeDeadlineSeconds": max(1, int(active_deadline_s + 0.999)),
        "terminationGracePeriodSeconds": max(1, int(termination_grace_s + 0.999)),
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": RUN_AS_USER,
            "runAsGroup": RUN_AS_USER,
            "fsGroup": RUN_AS_USER,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [container],
        "volumes": [
            {
                "name": "segredo",
                # Só leitura, para o grupo do contêiner (o fsGroup), e nada para os outros.
                "secret": {"secretName": secret_name, "defaultMode": 0o440},
            },
            {"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": settings.tmp_size}},
            {"name": "trabalho", "emptyDir": {"sizeLimit": settings.work_size}},
        ],
    }
    if settings.runtime_class is not None:
        spec["runtimeClassName"] = settings.runtime_class
    if settings.service_account is not None:
        spec["serviceAccountName"] = settings.service_account
    if settings.pull_secret is not None:
        spec["imagePullSecrets"] = [{"name": settings.pull_secret}]
    if settings.node_selector:
        spec["nodeSelector"] = dict(settings.node_selector)
    if settings.tolerations:
        spec["tolerations"] = [dict(item) for item in settings.tolerations]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": settings.namespace,
            "labels": dict(labels),
            "ownerReferences": _owner(owner),
        },
        "spec": spec,
    }


@dataclass(frozen=True)
class PodState:
    """O que interessa da situação de um pod."""

    #: ``Pending``, ``Running``, ``Succeeded``, ``Failed``, ``Unknown`` ou ``Gone`` (apagado).
    phase: str
    ready: bool = False
    ip: str | None = None
    #: O motivo de espera do contêiner (``ImagePullBackOff``) ou do agendamento
    #: (``Unschedulable``).
    reason: str | None = None

    @property
    def finished(self) -> bool:
        return self.phase in ("Succeeded", "Failed", "Gone")

    @property
    def stuck(self) -> bool:
        """O contêiner nunca vai subir sozinho (a imagem ou a configuração)."""
        return self.reason in HARD_WAITING_REASONS


def pod_state(pod: Mapping[str, Any] | None) -> PodState:
    if pod is None:
        return PodState("Gone")
    status = pod.get("status") or {}
    phase = str(status.get("phase") or "Unknown")
    ready = False
    reason: str | None = None
    for condition in status.get("conditions") or []:
        if not isinstance(condition, Mapping):
            continue
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            ready = True
        if condition.get("type") == "PodScheduled" and condition.get("status") == "False":
            reason = str(condition.get("reason") or "Unschedulable")
    for container in status.get("containerStatuses") or []:
        if not isinstance(container, Mapping):
            continue
        waiting = (container.get("state") or {}).get("waiting")
        if isinstance(waiting, Mapping) and waiting.get("reason"):
            reason = str(waiting["reason"])
    ip = status.get("podIP")
    return PodState(
        phase=phase, ready=ready, ip=ip if isinstance(ip, str) and ip else None, reason=reason
    )
