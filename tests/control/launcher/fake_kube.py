"""Uma API do Kubernetes falsa (pods e secrets em memória) para os lançadores de pods."""

import base64
import json
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2

from rapa.control.kube import KubeApi, KubeConfig

_PATH = re.compile(
    r"^/api/v1/namespaces/(?P<ns>[^/]+)/(?P<plural>pods|secrets)(?:/(?P<name>[^/]+))?$"
)

type Hook = Callable[[dict[str, Any]], None]
_TOKEN = "token-da-conta"


class FakeKube:
    """Guarda os objetos criados e deixa o teste mudar a situação dos pods.

    ``on_pod`` roda quando um pod é criado (o teste pode, por exemplo, subir o agente
    do pod como um subprocesso); ``on_delete`` quando um pod é apagado.
    """

    def __init__(self, *, token: str | None = None) -> None:
        self.token = token or _TOKEN
        self.objects: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str, dict[str, Any]]] = []
        self.requests: list[tuple[str, str]] = []
        self.on_pod: Hook | None = None
        self.on_delete: Hook | None = None
        #: Recusa a criação deste tipo de objeto com este código (403, 409...).
        self.refuse: dict[str, int] = {}
        self.lock = threading.RLock()

    def api(self, tmp_path: Path) -> KubeApi:
        token = tmp_path / "token"
        token.write_text(self.token)
        return KubeApi(
            KubeConfig(base_url="https://kube.test", token_path=token),
            transport=httpx2.MockTransport(self.handle),
        )

    def pods(self) -> list[dict[str, Any]]:
        return [obj for (_, plural, _), obj in self.objects.items() if plural == "pods"]

    def secrets(self) -> list[dict[str, Any]]:
        return [obj for (_, plural, _), obj in self.objects.items() if plural == "secrets"]

    def secret_data(self, name: str, namespace: str = "rapa-execucoes") -> dict[str, bytes]:
        secret = self.objects[(namespace, "secrets", name)]
        return {key: base64.b64decode(value) for key, value in secret["data"].items()}

    def set_status(
        self, name: str, status: dict[str, Any], namespace: str = "rapa-execucoes"
    ) -> None:
        with self.lock:
            pod = self.objects.get((namespace, "pods", name))
            if pod is not None:
                pod["status"] = status

    def add(self, plural: str, body: dict[str, Any], namespace: str = "rapa-execucoes") -> None:
        body.setdefault("metadata", {}).setdefault("uid", str(uuid4()))
        self.objects[(namespace, plural, body["metadata"]["name"])] = body

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx2.Response(401, json={"kind": "Status", "reason": "Unauthorized"})
        match = _PATH.match(request.url.path)
        if match is None:
            return httpx2.Response(404, json={"kind": "Status", "reason": "NotFound"})
        ns, plural, name = match["ns"], match["plural"], match["name"]
        self.requests.append((request.method, request.url.path))
        hook: Hook | None = None
        with self.lock:
            if request.method == "POST" and name is None:
                if plural in self.refuse:
                    return httpx2.Response(
                        self.refuse[plural], json={"kind": "Status", "reason": "Forbidden"}
                    )
                body = json.loads(request.content)
                key = (ns, plural, body["metadata"]["name"])
                if key in self.objects:
                    return httpx2.Response(409, json={"kind": "Status", "reason": "AlreadyExists"})
                body["metadata"]["uid"] = str(uuid4())
                body["metadata"]["creationTimestamp"] = datetime.now(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                if plural == "pods":
                    body["status"] = {"phase": "Pending"}
                    hook = self.on_pod
                self.objects[key] = body
                self.created.append(body)
                response = httpx2.Response(201, json=body)
            elif request.method == "GET" and name is not None:
                obj = self.objects.get((ns, plural, name))
                response = (
                    httpx2.Response(200, json=obj)
                    if obj is not None
                    else httpx2.Response(404, json={"kind": "Status", "reason": "NotFound"})
                )
            elif request.method == "GET":
                selector = dict(
                    part.split("=", 1)
                    for part in request.url.params.get("labelSelector", "").split(",")
                    if part
                )
                items = [
                    obj
                    for (o_ns, o_plural, _), obj in self.objects.items()
                    if o_ns == ns
                    and o_plural == plural
                    and all(
                        obj["metadata"].get("labels", {}).get(k) == v for k, v in selector.items()
                    )
                ]
                response = httpx2.Response(200, json={"kind": "List", "items": items})
            elif request.method == "DELETE" and name is not None:
                options = json.loads(request.content) if request.content else {}
                obj = self.objects.pop((ns, plural, name), None)
                if obj is None:
                    return httpx2.Response(404, json={"kind": "Status", "reason": "NotFound"})
                self.deleted.append((plural, name, options))
                if plural == "secrets":
                    # O coletor apaga o pod de que o Secret é dono.
                    uid = obj["metadata"]["uid"]
                    for key, pod in list(self.objects.items()):
                        owners = pod["metadata"].get("ownerReferences") or []
                        if key[1] == "pods" and any(o.get("uid") == uid for o in owners):
                            self.objects.pop(key)
                            if self.on_delete is not None:
                                self.on_delete(pod)
                elif self.on_delete is not None:
                    hook_obj = obj
                    response = httpx2.Response(200, json={"kind": "Status", "status": "Success"})
                    self.on_delete(hook_obj)
                    return response
                response = httpx2.Response(200, json={"kind": "Status", "status": "Success"})
            else:
                response = httpx2.Response(
                    405, json={"kind": "Status", "reason": "MethodNotAllowed"}
                )
        if hook is not None:
            hook(self.objects[(ns, plural, json.loads(request.content)["metadata"]["name"])])
        return response


def all_bytes(root: Path, *, skip: tuple[str, ...] = ()) -> bytes:
    """O conteúdo de todos os arquivos abaixo de ``root`` (para procurar segredos)."""
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file() and p.name not in skip)
