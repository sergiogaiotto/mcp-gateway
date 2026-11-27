"""Um trabalho de teste para o worker de verdade dentro da imagem do executor (job imagens do CI).

Uso::

    python deploy/ci/trabalho_de_teste.py preparar --portal http://portal-v1.rapa.test:8080 --pasta DIR
    # ... roda python -m rapa.runner.worker na imagem, com DIR montada em /trabalho ...
    python deploy/ci/trabalho_de_teste.py conferir --pasta DIR

``preparar`` escreve ``job.json`` (o robô de exemplo que entra no portal de teste e baixa as
notas), ``secrets.json`` (o login e o TOTP do portal de teste, que não são segredos de
ninguém) e a pasta dos artefatos. ``conferir`` lê o ``result.json`` e confere que a execução
deu certo, com os arquivos baixados na pasta dos artefatos e os eventos escritos.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from rapa.core.contracts import RunnerJob, RunState
from rapa.core.ids import new_id
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.fixtures.robots import baixar_notas
from rapa.runner.result import RunResult

__all__ = ["check", "main", "prepare"]

ARTIFACTS = "artefatos"


def prepare(portal: str, folder: Path) -> RunnerJob:
    """Escreve o trabalho, os segredos do portal de teste e a pasta dos artefatos."""
    host = urlsplit(portal).hostname
    if not host:
        raise ValueError(f"endereço do portal inválido: {portal!r}")
    folder.mkdir(parents=True, exist_ok=True)
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=baixar_notas(portal, host),
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )
    (folder / "job.json").write_text(job.model_dump_json(), encoding="utf-8")
    secrets = {
        "portal": {"username": DEFAULT_USER, "password": DEFAULT_PASSWORD},
        "portal_otp": {"totp_secret": DEFAULT_TOTP_SECRET},
    }
    (folder / "secrets.json").write_text(json.dumps(secrets), encoding="utf-8")
    (folder / ARTIFACTS).mkdir(exist_ok=True)
    return job


def check(folder: Path) -> RunResult:
    """Confere o resultado do worker; levanta ``AssertionError`` com o motivo se falhou."""
    job = RunnerJob.model_validate_json((folder / "job.json").read_text(encoding="utf-8"))
    result = RunResult.model_validate_json((folder / "result.json").read_text(encoding="utf-8"))
    if result.run_id != job.run_id:
        raise AssertionError("o resultado é de outra execução")
    if result.state is not RunState.SUCCEEDED:
        raise AssertionError(
            f"a execução terminou {result.state.value}: {result.error_code} {result.error_message}"
        )
    downloads = sorted(p.name for p in (folder / ARTIFACTS).rglob("*") if p.is_file())
    if not result.artifacts or not downloads:
        raise AssertionError("a execução deu certo, mas sem os arquivos baixados")
    events = (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
    if not events:
        raise AssertionError("o worker não escreveu nenhum evento")
    if (folder / "secrets.json").exists():
        raise AssertionError("o worker não apagou o arquivo de segredos depois de ler")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trabalho_de_teste", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("preparar")
    prep.add_argument("--portal", required=True, help="endereço do portal de teste")
    prep.add_argument("--pasta", required=True, type=Path)
    verify = commands.add_parser("conferir")
    verify.add_argument("--pasta", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "preparar":
        job = prepare(args.portal, args.pasta)
        print(f"trabalho {job.run_id} em {args.pasta}")
        return 0
    try:
        result = check(args.pasta)
    except (AssertionError, OSError, ValueError) as exc:
        print(f"trabalho_de_teste: {exc}", file=sys.stderr)
        return 1
    print(f"execução {result.run_id}: {result.state.value}, {len(result.artifacts)} artefatos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
