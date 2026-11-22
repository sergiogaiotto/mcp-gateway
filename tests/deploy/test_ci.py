"""O CI do deploy (.github/workflows/ci.yml): os jobs chart, imagens e kind e os scripts deles."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.support import REPO

WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SCRIPTS = (
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
