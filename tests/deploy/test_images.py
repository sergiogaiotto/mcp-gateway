"""As imagens do RaPA (deploy/docker/Dockerfile): as regras que se conferem sem construir.

A construção de verdade, a imagem de controle contra o PostgreSQL e o worker dentro da
imagem do executor ficam no job ``imagens`` do CI (não há Docker aqui).
"""

import ast
import functools
import importlib.metadata
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.deploy.support import REPO

DOCKERFILE = REPO / "deploy" / "docker" / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"


@dataclass
class Stage:
    name: str
    base: str
    instructions: list[tuple[str, str]] = field(default_factory=list)

    def all(self, keyword: str) -> list[str]:
        return [args for key, args in self.instructions if key == keyword]

    @property
    def text(self) -> str:
        return "\n".join(f"{key} {args}" for key, args in self.instructions)


@functools.cache
def stages() -> dict[str, Stage]:
    """As etapas do Dockerfile, com as instruções de cada uma (continuações juntadas)."""
    logical: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        logical.append(pending + line)
        pending = ""
    out: dict[str, Stage] = {}
    current: Stage | None = None
    for line in logical:
        keyword, _, args = line.partition(" ")
        keyword = keyword.upper()
        if keyword == "FROM":
            match = re.fullmatch(r"(\S+)\s+AS\s+(\S+)", args.strip(), flags=re.IGNORECASE)
            assert match, line
            current = Stage(name=match[2], base=match[1])
            out[current.name] = current
        elif current is not None:
            current.instructions.append((keyword, args.strip()))
    return out


def chain(name: str) -> list[Stage]:
    """A etapa e as de que ela herda (a mais distante primeiro)."""
    found = stages()
    out = []
    stage: Stage | None = found[name]
    while stage is not None:
        out.insert(0, stage)
        stage = found.get(stage.base)
    return out


def final_env(name: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for stage in chain(name):
        for args in stage.all("ENV"):
            for key, value in re.findall(r"(\w+)=(\S+)", args):
                env[key] = value
    return env


def last(name: str, keyword: str) -> str:
    values = [args for stage in chain(name) for args in stage.all(keyword)]
    assert values, f"{name} sem {keyword}"
    return values[-1]


def apt_packages(name: str) -> set[str]:
    packages: set[str] = set()
    for stage in chain(name):
        for args in stage.all("RUN"):
            for match in re.finditer(r"apt-get install ([^&;]+)", args):
                packages |= {w for w in match[1].split() if not w.startswith("-")}
    return packages


def test_two_targets_on_debian_13_python() -> None:
    found = stages()
    assert {"controle", "executor"} <= set(found)
    header = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG PYTHON_IMAGE=python:3.14-slim-trixie" in header
    for stage in found.values():
        assert stage.base in found or stage.base == "${PYTHON_IMAGE}", stage.name


def test_dependencies_from_the_lock_without_dev() -> None:
    syncs = {
        name: [args for args in stages()[name].all("RUN") if args.startswith("uv sync")]
        for name in ("dependencias-controle", "dependencias-executor")
    }
    for name, commands in syncs.items():
        assert commands, name
        for command in commands:
            assert "--frozen" in command and "--no-dev" in command, command
        assert "--no-editable" in commands[-1], name
    assert all("--all-extras" in c for c in syncs["dependencias-controle"])
    # O executor só com o extra runner (nada do plano de controle).
    for command in syncs["dependencias-executor"]:
        assert "--extra runner" in command and "--all-extras" not in command
        assert len(re.findall(r"--extra \w+", command)) == 1
    assert final_env("construcao")["UV_PROJECT_ENVIRONMENT"] == "/opt/rapa"
    assert "COPY --from=dependencias-controle /opt/rapa /opt/rapa" in stages()["controle"].text
    assert "COPY --from=dependencias-executor /opt/rapa /opt/rapa" in stages()["executor"].text


@pytest.mark.parametrize("target", ["controle", "executor"])
def test_runs_as_10001_read_only_with_tini(target: str) -> None:
    assert last(target, "USER") == "10001:10001"
    base = stages()["base"].text
    assert "--uid 10001" in base and "--gid 10001" in base
    assert "--shell /usr/sbin/nologin" in base
    env = final_env(target)
    assert env["HOME"] == "/tmp"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    # python -m nunca põe a pasta de trabalho no sys.path.
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PATH"].startswith("/opt/rapa/bin:")
    assert last(target, "ENTRYPOINT") == '["/usr/bin/tini", "--"]'
    assert "tini" in apt_packages(target)
    # Nenhuma instrução depois do USER volta para root ou escreve no disco.
    instructions = [i for stage in chain(target) for i in stage.instructions]
    after_user = instructions[[k for k, _ in instructions].index("USER") :]
    assert not [args for key, args in after_user if key in {"RUN", "COPY", "ADD"}]


def test_control_image() -> None:
    assert {"chromium", "fonts-liberation", "ca-certificates", "tzdata"} <= apt_packages("controle")
    assert last("controle", "CMD") == (
        '["python", "-m", "rapa.server", "--host", "0.0.0.0", "--port", "8000"]'
    )
    # O NiceGUI escreve só em /tmp.
    assert final_env("controle")["NICEGUI_STORAGE_PATH"] == "/tmp/nicegui"


def test_executor_image() -> None:
    assert {
        "chromium",
        "fonts-liberation",
        "fonts-noto-core",
        "tzdata",
        "ca-certificates",
        "ffmpeg",
    } <= apt_packages("executor")
    env = final_env("executor")
    assert env["RAPA_CHROMIUM_PATH"] == "/usr/bin/chromium"
    assert env["RAPA_ARGS_NAVEGADOR"] == "--no-sandbox"
    assert last("executor", "CMD") == '["python", "-m", "rapa.runner.pod"]'
    # O vídeo da falha: o ffmpeg do Debian onde o Playwright procura, conferido na construção.
    text = stages()["executor"].text
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/rapa-navegadores"
    assert 'ln -s /usr/bin/ffmpeg "/opt/rapa-navegadores/ffmpeg-${revisao}/ffmpeg-linux"' in text
    assert "ffmpeg_installed()" in text


def test_no_sandbox_only_in_the_executor_image() -> None:
    """O --no-sandbox (o isolamento é o gVisor) nunca vale para o plano de controle."""
    for name in ("base", "controle", "construcao"):
        assert "RAPA_ARGS_NAVEGADOR" not in final_env(name), name
        assert "--no-sandbox" not in "\n".join(s.text for s in chain(name)), name
    # E o porquê está escrito ao lado.
    assert "gVisor" in DOCKERFILE.read_text(encoding="utf-8").split("RAPA_ARGS_NAVEGADOR=")[0]


@pytest.mark.parametrize("target", ["controle", "executor"])
def test_oci_labels(target: str) -> None:
    labels = " ".join(args for stage in chain(target) for args in stage.all("LABEL"))
    for key in (
        "org.opencontainers.image.title",
        "org.opencontainers.image.description",
        "org.opencontainers.image.licenses",
        "org.opencontainers.image.source",
        "org.opencontainers.image.revision",
        "org.opencontainers.image.version",
    ):
        assert key in labels, key
    assert f'org.opencontainers.image.title="rapa-{target}"' in labels


_SECRET_NAME = re.compile(r"(PASSWORD|SENHA|SECRET|TOKEN|CHAVE|_KEY\b|API_KEY|CREDENTIAL)", re.I)


def test_no_secret_in_any_layer() -> None:
    for stage in stages().values():
        for key, args in stage.instructions:
            if key in {"ARG", "ENV"}:
                for name in re.findall(r"(\w+)=", args) or [args.split("=")[0]]:
                    assert not _SECRET_NAME.search(name), (stage.name, args)
            if key in {"COPY", "ADD"}:
                assert ".env" not in args and "secrets" not in args.lower(), args
                assert not args.startswith(". ") and " . " not in f" {args} ", args
            if key == "RUN":
                # Nada baixado de fora do apt e do PyPI (e nenhum segredo de construção).
                assert "--mount=type=secret" not in args, (stage.name, args)
                assert not re.search(r"(curl|wget)\s", args), (stage.name, args)


def test_dockerignore_sends_only_what_the_build_needs() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    allowed = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "src"}
    copied = {
        part.rstrip("/")
        for stage in stages().values()
        for args in stage.all("COPY")
        if "--from" not in args
        for part in args.split()[:-1]
    }
    assert copied <= allowed


# ---------------------------------------------------------------------------------------------
# O que roda na imagem do executor importa só o que o extra runner instala
# ---------------------------------------------------------------------------------------------

#: Os módulos que a imagem do executor roda (o worker e o host de sessão; o agente do pod
#: quando existir).
EXECUTOR_ENTRYPOINTS = ("rapa.runner.worker", "rapa.session.host", "rapa.runner.pod")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.\-]+)", requirement)
    assert match, requirement
    return _normalize(match[1])


@functools.cache
def _runner_distributions() -> frozenset[str]:
    """As distribuições que o extra runner instala (com as dependências delas)."""
    requires = importlib.metadata.requires("rapa") or []
    roots = [
        _requirement_name(r)
        for r in requires
        if "extra ==" not in r or re.search(r"extra\s*==\s*[\"']runner[\"']", r)
    ]
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            requirements = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for requirement in requirements:
            if "extra ==" in requirement:
                continue
            if "python_version" in requirement or "sys_platform" in requirement:
                marker = requirement.split(";", 1)[1]
                if "win32" in marker or "darwin" in marker:
                    continue
            pending.append(_requirement_name(requirement))
    return frozenset(seen)


def _module_file(module: str) -> Path | None:
    base = REPO / "src" / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _imports(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
                out.add(target)
                out |= {f"{target}.{alias.name}" for alias in node.names}
            elif node.module:
                out.add(node.module)
                if node.module.startswith("rapa"):
                    out |= {f"{node.module}.{alias.name}" for alias in node.names}
    return out


def _reachable_third_party(entrypoints: tuple[str, ...]) -> dict[str, str]:
    """Os pacotes de fora que os módulos do RaPA alcançáveis dos pontos de entrada importam."""
    seen: set[str] = set()
    third: dict[str, str] = {}
    pending = [e for e in entrypoints if _module_file(e) is not None]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(module)
        if path is None:
            continue
        # O pacote de cada módulo também roda (o __init__ das pastas).
        parent = module.rpartition(".")[0]
        if parent:
            pending.append(parent)
        for name in _imports(path, module):
            top = name.split(".")[0]
            if top == "rapa":
                pending.append(name)
            elif top not in sys.stdlib_module_names:
                third.setdefault(top, module)
    return third


def test_executor_modules_import_only_the_runner_extra() -> None:
    """Sem isto, a imagem do executor (só o extra runner) quebraria ao rodar o worker."""
    distributions = _runner_distributions()
    providers = importlib.metadata.packages_distributions()
    missing = {}
    for top, module in _reachable_third_party(EXECUTOR_ENTRYPOINTS).items():
        owners = {_normalize(d) for d in providers.get(top, [top])}
        if not owners & distributions:
            missing[top] = module
    assert missing == {}, f"importados fora do extra runner: {missing}"


def test_the_import_check_sees_a_control_plane_import() -> None:
    """A conferência acima de fato pega um pacote do plano de controle."""
    third = _reachable_third_party(("rapa.control.launcher",))
    distributions = _runner_distributions()
    providers = importlib.metadata.packages_distributions()
    outside = {
        top
        for top in third
        if not {_normalize(d) for d in providers.get(top, [top])} & distributions
    }
    assert outside  # o lançador importa o banco (sqlalchemy), fora do extra runner
