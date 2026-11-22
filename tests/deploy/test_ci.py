"""O CI do deploy (.github/workflows/ci.yml): os jobs chart, imagens e kind e os scripts deles."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.support import REPO

WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SCRIPTS = (
    "deploy/ci/instalar-bao.sh",
    "deploy/ci/testar-controle.sh",
    "deploy/ci/testar-executor.sh",
    "deploy/kind/instalar.sh",
    "deploy/kind/testar.sh",
    "deploy/kind/recolher-logs.sh",
    "deploy/banco/testar-restauracao.sh",
)


def jobs() -> dict[str, Any]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    result: dict[str, Any] = workflow["jobs"]
    return result


def steps(job: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = jobs()[job]["steps"]
    return result


def runs(job: str) -> str:
    return "\n".join(step.get("run", "") for step in steps(job))


def uses(job: str, action: str) -> list[dict[str, Any]]:
    return [step for step in steps(job) if step.get("uses", "").startswith(action + "@")]


def test_new_jobs_run_on_ubuntu_latest_next_to_the_old_one() -> None:
    found = jobs()
    assert {"testes", "chart", "imagens", "kind"} <= set(found)
    for name in ("chart", "imagens", "kind"):
        assert found[name]["runs-on"] == "ubuntu-latest"
        assert found[name]["timeout-minutes"] <= 60


def test_images_are_built_but_never_pushed() -> None:
    for job in ("imagens", "kind"):
        builds = uses(job, "docker/build-push-action")
        assert {b["with"]["target"] for b in builds} == {"controle", "executor"}
        for build in builds:
            assert build["with"]["push"] is False
            assert build["with"]["load"] is True
            assert build["with"]["file"] == "deploy/docker/Dockerfile"
        assert "docker push" not in runs(job) and "docker login" not in runs(job)
        assert not uses(job, "docker/login-action")


def test_images_job_runs_both_images_for_real() -> None:
    assert "postgres" in jobs()["imagens"]["services"]
    commands = runs("imagens")
    assert "deploy/ci/testar-controle.sh rapa-controle:ci" in commands
    assert "deploy/ci/testar-executor.sh rapa-executor:ci rapa-controle:ci" in commands


def test_trivy_is_only_informative() -> None:
    scans = uses("imagens", "aquasecurity/trivy-action")
    assert {s["with"]["image-ref"] for s in scans} == {"rapa-controle:ci", "rapa-executor:ci"}
    for scan in scans:
        assert scan["continue-on-error"] is True
        assert scan["with"]["exit-code"] == "0"


def test_chart_job_never_skips_the_chart_tests() -> None:
    job = jobs()["chart"]
    assert job["env"]["RAPA_TEST_CHART_OBRIGATORIO"] == "1"
    commands = runs("chart")
    assert "kubeconform" in commands
    assert 'pytest -q tests/deploy -m "not browser and not database"' in commands
    assert uses("chart", "azure/setup-helm")


def test_kind_job_installs_tests_and_collects_logs() -> None:
    kind = uses("kind", "helm/kind-action")
    assert kind and kind[0]["with"]["config"] == "deploy/kind/cluster.yaml"
    commands = runs("kind")
    assert commands.index("deploy/kind/instalar.sh") < commands.index("deploy/kind/testar.sh")
    collect = next(s for s in steps("kind") if "recolher-logs.sh" in s.get("run", ""))
    assert collect["if"] == "failure()"
    upload = uses("kind", "actions/upload-artifact")
    assert upload and upload[0]["if"] == "failure()"


def test_kind_install_follows_the_production_order() -> None:
    script = (REPO / "deploy/kind/instalar.sh").read_text(encoding="utf-8")
    order = [
        "cnpg/cloudnative-pg",
        "openbao/openbao",
        "seaweedfs.yaml",
        "portal.yaml",
        "rapa-banco-",
        "helm upgrade --install rapa-banco",
        "deploy/openbao",
        "helm upgrade --install rapa ",
        "empresa criar",
        'rollout restart "deployment/rapa-',
    ]
    positions = [script.index(item) for item in order]
    assert positions == sorted(positions)
    # A release do banco com os valores do kind e a própria; a da aplicação só com os do kind.
    assert '-f "$chart/values-banco.yaml"' in script
    assert script.count('-f "$chart/values-kind.yaml"') == 2


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_are_executable_bash_without_syntax_errors(script: str) -> None:
    path = REPO / script
    assert os.access(path, os.X_OK), script
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text or "set -uo pipefail" in text
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("sem bash")
    check = subprocess.run(  # noqa: S603 - o bash do PATH, só conferindo a sintaxe
        [bash, "-n", str(path)], capture_output=True, text=True, check=False
    )
    assert check.returncode == 0, check.stderr


def test_kind_manifests_are_valid_yaml() -> None:
    for name in ("cluster.yaml", "portal.yaml", "seaweedfs.yaml", "openbao-valores.yaml"):
        docs = list(yaml.safe_load_all((REPO / "deploy" / "kind" / name).read_text("utf-8")))
        assert docs and all(isinstance(d, dict) for d in docs), name


def test_portal_runs_restricted_in_kind() -> None:
    docs = list(yaml.safe_load_all(Path(REPO / "deploy/kind/portal.yaml").read_text("utf-8")))
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["image"] == "rapa-controle:ci"


def test_the_tests_job_runs_the_real_openbao_test_and_never_skips_it() -> None:
    """O teste das políticas contra o bao server -dev roda no CI com o binário oficial do
    OpenBao (versão e SHA-256 fixos), sozinho e na suíte inteira, e lá não pode ser pulado."""
    job = jobs()["testes"]
    assert job["env"]["RAPA_TEST_BAO_OBRIGATORIO"] == "1"
    assert job["env"]["RAPA_TEST_VIDEO_OBRIGATORIO"] == "1"
    names = [step.get("name", "") for step in steps("testes")]
    commands = runs("testes")
    install = commands.index("deploy/ci/instalar-bao.sh")
    real = commands.index("test_openbao_files.py::test_everything_against_bao_server_dev")
    assert install < real < commands.index("uv run pytest -q -o faulthandler_timeout=300")
    assert "curl" in next(
        s["run"] for s in steps("testes") if "apt-get install" in s.get("run", "")
    )
    assert any("OpenBao" in name for name in names)
    # O lint e o formato cobrem o que o gate cobre.
    assert "ruff check src tests deploy docs" in commands
    assert "ruff format --check src tests deploy docs" in commands
    assert job["timeout-minutes"] <= 60


def test_the_openbao_binary_is_pinned_and_checked_before_use() -> None:
    script = (REPO / "deploy/ci/instalar-bao.sh").read_text(encoding="utf-8")
    assert re.search(r"^versao=\d+\.\d+\.\d+$", script, flags=re.MULTILINE)
    assert re.search(r"^sha256=[0-9a-f]{64}$", script, flags=re.MULTILINE)
    assert "https://github.com/openbao/openbao/releases/download/" in script
    # O pacote só é aberto depois de conferido.
    assert script.index("sha256sum --check --strict") < script.index("tar -xzf")
    assert "--proto '=https'" in script


def test_kind_prepares_openbao_from_the_runner_and_checks_the_pods() -> None:
    commands = runs("kind")
    assert commands.index("deploy/ci/instalar-bao.sh") < commands.index("deploy/kind/instalar.sh")
    install = (REPO / "deploy/kind/instalar.sh").read_text(encoding="utf-8")
    # A imagem do OpenBao não tem o bash: o preparar.sh roda no runner, pelo port-forward.
    assert '"$raiz/deploy/openbao/preparar.sh"' in install
    assert "port-forward pod/openbao-0" in install and "sh /tmp/openbao" not in install
    check = (REPO / "deploy/kind/testar.sh").read_text(encoding="utf-8")
    assert "app.kubernetes.io/name=rapa-executor" in check
    assert "pod/rapa-execucao-" in check and "get pods,secrets" in check
    for step in steps("kind"):
        if "instalar.sh" in step.get("run", "") or "testar.sh" in step.get("run", ""):
            assert step.get("timeout-minutes", 0) <= 30, step["name"]
