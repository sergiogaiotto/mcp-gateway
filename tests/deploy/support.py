"""Auxiliares dos testes do deploy: o ``helm``, o ``kubeconform`` e os manifestos renderizados.

Sem o ``helm`` ou o ``kubeconform`` no PATH, os testes que precisam deles são pulados; no CI
(``RAPA_TEST_CHART_OBRIGATORIO=1``, o job ``chart``), falham. Os esquemas do ``kubeconform``
(os do Kubernetes e os dos CRDs: CloudNativePG, Gateway API, Envoy Gateway, Argo CD e
VictoriaMetrics) ficam num cache em ``RAPA_KUBECONFORM_CACHE`` (padrão:
``~/.cache/rapa-kubeconform``); sem rede e sem o esquema no cache, o teste é pulado (no CI,
falha).
"""

import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "deploy" / "helm" / "rapa"
#: A versão do Kubernetes dos esquemas e do ``helm template`` (a do EKS no Outposts).
KUBE_VERSION = "1.33.0"
REQUIRED_ENV = "RAPA_TEST_CHART_OBRIGATORIO"
CACHE_ENV = "RAPA_KUBECONFORM_CACHE"
#: Os esquemas dos CRDs (catálogo aberto, Apache-2.0).
CRD_SCHEMAS = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

#: Os conjuntos de valores renderizados nos testes: o padrão, o do kind, o de produção de
#: exemplo, e as releases só do banco do kind e de produção.
VARIANTS: dict[str, tuple[str, ...]] = {
    "padrao": (),
    "kind": ("values-kind.yaml",),
    "producao": ("values-producao-exemplo.yaml",),
    "banco-kind": ("values-kind.yaml", "values-banco.yaml"),
    "banco-producao": ("values-producao-exemplo.yaml", "values-banco.yaml"),
}


def tool(name: str) -> str:
    """O caminho de ``name`` no PATH, ou pula o teste (falha, se o chart é obrigatório)."""
    found = shutil.which(name)
    if found is not None:
        return found
    message = f"{name} não está no PATH"
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(f"{message}, e {REQUIRED_ENV}=1 pede os testes do chart")
    pytest.skip(message)


@dataclass(frozen=True)
class Render:
    """Os manifestos de um ``helm template``."""

    text: str
    docs: tuple[dict[str, Any], ...]

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [d for d in self.docs if d.get("kind") == kind]

    def get(self, kind: str, name: str, namespace: str | None = None) -> dict[str, Any]:
        for doc in self.of_kind(kind):
            meta = doc["metadata"]
            if meta["name"] == name and (namespace is None or meta.get("namespace") == namespace):
                return doc
        raise AssertionError(f"{kind}/{name} não está no render")

    def names(self, kind: str) -> set[str]:
        return {d["metadata"]["name"] for d in self.of_kind(kind)}

    def pod_specs(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Cada pod do render (Deployments, Jobs e PodTemplates), com o nome de quem o cria."""
        for doc in self.docs:
            if doc.get("kind") == "PodTemplate":
                yield f"PodTemplate/{doc['metadata']['name']}", doc["template"]["spec"]
            elif doc.get("kind") in {"Deployment", "Job", "StatefulSet", "DaemonSet", "CronJob"}:
                template = doc["spec"]
                if doc["kind"] == "CronJob":
                    template = template["jobTemplate"]["spec"]
                yield f"{doc['kind']}/{doc['metadata']['name']}", template["template"]["spec"]


def container_env(pod: Mapping[str, Any], container: str | None = None) -> dict[str, Any]:
    """As variáveis de um contêiner: o valor literal, ou o ``valueFrom``."""
    containers = pod["containers"]
    chosen = (
        containers[0]
        if container is None
        else next(c for c in containers if c["name"] == container)
    )
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in chosen.get("env", [])}


def deployment_pod(render: Render, name: str) -> dict[str, Any]:
    spec: dict[str, Any] = render.get("Deployment", name)["spec"]["template"]["spec"]
    return spec


def _helm_args(
    values: Sequence[str], sets: Mapping[str, str] | None, set_json: Mapping[str, Any] | None
) -> list[str]:
    args: list[str] = []
    for name in values:
        args += ["-f", str(CHART / name)]
    for key, value in (sets or {}).items():
        args += ["--set", f"{key}={value}"]
    for key, value in (set_json or {}).items():
        args += ["--set-json", f"{key}={json.dumps(value)}"]
    return args


def run_helm(*args: str) -> subprocess.CompletedProcess[str]:
    helm = tool("helm")
    return subprocess.run(  # noqa: S603 - o helm do PATH, com argumentos fixos
        [helm, *args], capture_output=True, text=True, timeout=120, check=False
    )


def template(
    *values: str,
    sets: Mapping[str, str] | None = None,
    set_json: Mapping[str, Any] | None = None,
    release: str = "rapa",
    namespace: str = "rapa",
) -> Render:
    """``helm template`` do chart com os arquivos de valores (relativos ao chart)."""
    result = run_helm(
        "template",
        release,
        str(CHART),
        "--namespace",
        namespace,
        "--kube-version",
        KUBE_VERSION,
        *_helm_args(values, sets, set_json),
    )
    assert result.returncode == 0, result.stderr
    docs = tuple(d for d in yaml.safe_load_all(result.stdout) if d)
    return Render(text=result.stdout, docs=docs)


def template_error(
    *values: str,
    sets: Mapping[str, str] | None = None,
    set_json: Mapping[str, Any] | None = None,
) -> str:
    """O erro de um ``helm template`` que precisa falhar."""
    result = run_helm(
        "template",
        "rapa",
        str(CHART),
        "--namespace",
        "rapa",
        "--kube-version",
        KUBE_VERSION,
        *_helm_args(values, sets, set_json),
    )
    assert result.returncode != 0, "o helm template devia ter recusado esses valores"
    return result.stderr


def kubeconform(text: str) -> list[dict[str, Any]]:
    """Valida os manifestos com ``kubeconform -strict``; devolve os recursos que falharam.

    Um esquema que não baixa (sem rede e fora do cache) pula o teste, a não ser no CI.
    """
    binary = tool("kubeconform")
    cache = Path(os.environ.get(CACHE_ENV) or Path.home() / ".cache" / "rapa-kubeconform")
    cache.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - o kubeconform do PATH, com argumentos fixos
        [
            binary,
            "-strict",
            "-output",
            "json",
            "-kubernetes-version",
            KUBE_VERSION,
            "-cache",
            str(cache),
            "-schema-location",
            "default",
            "-schema-location",
            CRD_SCHEMAS,
            "-",
        ],
        input=text,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    report = json.loads(result.stdout or "{}")
    problems = [r for r in report.get("resources", []) if r.get("status") != "statusValid"]
    unreachable = [
        r
        for r in problems
        if r.get("status") == "statusError" and "could not find schema" in r.get("msg", "")
    ]
    if unreachable and os.environ.get(REQUIRED_ENV) != "1":
        pytest.skip(
            "o kubeconform não achou o esquema de "
            + ", ".join(sorted({r.get("kind", "?") for r in unreachable}))
            + " (sem rede e sem cache)"
        )
    if result.returncode != 0 and not problems:
        raise AssertionError(result.stderr or result.stdout)
    return problems
