"""Lançadores das execuções: o local (um processo) e o do Kubernetes (um pod).

O despachante escolhe pelo ``RAPA_EXECUTOR``: ``local`` (o padrão, para a Fase 0 e
o desenvolvimento) roda cada tentativa num processo ``python -m rapa.runner.worker``
(:class:`LocalProcessLauncher`); ``kubernetes`` roda cada uma num pod de uso único
com gVisor (:class:`KubernetesLauncher`, mais abaixo). Para cada tentativa, o
lançador local:

1. cria uma pasta só da tentativa (permissão 0700) com JOB.json e, se o robô usa
   credenciais, SECRETS.json (0600, apagado pelo worker logo depois de ler);
2. inicia o worker num grupo de processos próprio e com um ambiente mínimo, sem
   as variáveis do plano de controle (URL do banco, chaves etc.);
3. lê EVENTS.jsonl enquanto o worker roda e entrega os eventos ao banco; quando o
   início de um passo que grava (ou o aviso de passo irreversível) foi gravado,
   cria ``<seq>.ack`` para o worker seguir (ver
   :class:`rapa.runner.ports.AckingEventSink`). Gravado esse início, a execução
   nunca mais volta para a fila (ver :mod:`rapa.control.services.queue`);
4. no fim (ou no prazo, com SIGTERM e depois SIGKILL), lê RESULT.json e o
   devolve ao despachante.

Os artefatos ficam em ``<artifacts_dir>/<empresa>/<execução>/<tentativa>``
(:func:`rapa.control.artifacts.attempt_dir`, a disposição do
:class:`rapa.control.artifacts.LocalArtifactStore`); o despachante os registra pelo
store no fim. Com ``video_on_failure``, o worker filma a execução numa pasta da
tentativa (``<trabalho>/video``, apagada no fim) e guarda o vídeo só se ela não der
certo (ver :mod:`rapa.runner.video`); o limite para fechar o vídeo
(``video_close_s``) entra na folga antes de interromper o worker.

Os arquivos de entrada também são separados por empresa: o worker só recebe a
pasta ``<files_dir>/<uuid da empresa>``, com a empresa tirada da tarefa (que vem
do token, nunca das entradas). Assim o identificador de arquivo escolhido por
quem chama nunca alcança os arquivos de outra empresa. A pasta da empresa não é
criada aqui: se não existir, o passo de envio falha com "arquivo não encontrado".

Os segredos vêm de uma :class:`SecretSource`. Em produção ela consulta o OpenBao
(:class:`rapa.control.vault.OpenBaoSecretSource`); aqui há uma versão estática, para
desenvolvimento e testes. Quando a fonte diz que um segredo falta
(:class:`SecretsUnavailable`: a credencial não registrada, o segredo ou um campo fora do
cofre), o worker nem é iniciado e a execução termina ``failed`` com o código
``segredo_indisponivel`` e a mensagem da fonte (só nomes, nunca valores). O certificado
A1 chega da fonte como ``pfx_base64``: o lançador o grava num arquivo 0600 na pasta de
trabalho da tentativa (o ``pfx_path`` que o worker espera) e o apaga no fim, junto com o
SECRETS.json.

Com ``egress`` (``LocalLauncherSettings.egress``), o worker recebe
``--guarda-de-saida`` e o navegador da execução sai pela guarda da saída (só os
domínios do robô, sem a rede interna; ver :mod:`rapa.runner.worker`). O servidor liga
a guarda quando há redes internas permitidas ou proxy de saída e sempre fora do modo
de desenvolvimento.

:class:`KubernetesLauncher` (``RAPA_EXECUTOR=kubernetes``) tem a mesma interface e o
mesmo seguimento: a pasta de trabalho da tentativa continua no despachante (com
EVENTS.jsonl, ``ack/`` e RESULT.json, sem JOB.json nem SECRETS.json), e quem a enche é
o canal do despachante (:mod:`rapa.control.channel`), por onde o pod fala. Para cada
tentativa ele:

1. abre a tentativa no canal (o trabalho, os segredos só em memória e as opções do
   worker; o bilhete aleatório só vai para o pod) e cria um Secret (o endereço do
   canal da tentativa e o bilhete) e um Pod (:func:`rapa.control.kube.pod_manifest`) no
   ``RAPA_K8S_NAMESPACE_EXECUCOES``, com o Secret como dono (apagar um apaga os dois);
   um certificado A1 em arquivo (``pfx_path`` do arquivo de dev) vai como
   ``pfx_base64``, e os arquivos de entrada que as entradas de arquivo citam (os da
   pasta da empresa em ``files_dir``, com as regras do lançador local) ficam
   disponíveis para o pod buscar pelo canal. Se a fonte diz que um segredo falta
   (:class:`SecretsUnavailable`, também um ``pfx_base64`` inválido), a execução termina
   ``failed`` com ``segredo_indisponivel`` sem criar o Secret nem o pod, como no
   lançador local. Se a API recusa o Secret ou o pod (a cota, a RuntimeClass, a
   permissão), a falha é ``executor_nao_iniciou``, com o motivo do Kubernetes;
2. segue os eventos como o lançador local e acompanha a fase do pod: um pod que não
   sobe (``ImagePullBackOff`` e os outros motivos que não passam sozinhos, ou
   ``Pending``/``Unschedulable`` além de ``start_timeout_s``) ou que nunca buscou o
   trabalho vira falha ``executor_nao_iniciou`` (nada rodou: "falhou", nunca
   indeterminado); um pod que termina sem resultado, ``executor_sem_resultado``;
3. no prazo (com a mesma folga do local), apaga o pod (o SIGTERM, e o SIGKILL do
   kubelet depois da tolerância: o SIGTERM/SIGKILL de hoje) e ainda espera o
   resultado que o agente entrega nesse tempo;
4. no fim, fecha a tentativa no canal **antes** de decidir (o pod não busca mais o
   trabalho, e os envios dele que estavam no meio acabam sem deixar arquivo), troca o
   caminho de cada artefato do RESULT.json pelo da pasta local da tentativa (onde o
   canal o gravou), apaga o que o resultado não cita (tudo, sem resultado) e apaga o
   Secret e o Pod.

Na volta do despachante, e a cada minuto, :meth:`KubernetesLauncher.sweep` apaga os
pods e Secrets de execução (pelo rótulo) cuja tentativa não está viva no banco nem
neste processo (os que uma réplica que caiu deixou para trás) e os que têm o rótulo
de execução sem uma tentativa legível.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DataError, IntegrityError

from rapa.control.artifacts import attempt_dir
from rapa.control.channel import Channel, ChannelAttempt
from rapa.control.kube import (
    EXECUTOR_APP,
    LABEL_APP,
    LABEL_ATTEMPT,
    LABEL_COMPANY,
    LABEL_RUN,
    KubeApi,
    KubeError,
    PodSettings,
    PodState,
    pod_manifest,
    pod_state,
    secret_manifest,
)
from rapa.control.services.dispatcher import WorkerNotStarted, failure_result
from rapa.control.services.queue import StaleAttempt
from rapa.core.artifacts import valid_artifact_name
from rapa.core.contracts import InvalidInput, RunnerJob
from rapa.core.events import RunEvent
from rapa.core.robot import FieldType
from rapa.runner.pod import PodOptions
from rapa.runner.ports import (
    SECRET_FIELDS,
    FileUnavailable,
    LocalFileProvider,
    ack_path,
    waits_for_ack,
)
from rapa.runner.result import RunResult
from rapa.runner.video import VIDEO_CLOSE_TIMEOUT_S
from rapa.session.egress import EgressPolicy

__all__ = [
    "EXECUTOR_NOT_STARTED",
    "SOURCE_SECRET_FIELDS",
    "AttemptRegistry",
    "EventIngestor",
    "KubernetesLauncher",
    "KubernetesLauncherSettings",
    "LocalLauncherSettings",
    "LocalProcessLauncher",
    "NoSecrets",
    "SecretSource",
    "SecretsUnavailable",
    "StaticSecretSource",
    "child_env",
    "materialize_certificates",
]

logger = logging.getLogger("rapa.control.launcher")

#: Variáveis de ambiente repassadas ao worker; todo o resto fica de fora.
_ENV_ALLOWED = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT"})
_ENV_PREFIXES = ("PLAYWRIGHT_", "XDG_", "FONTCONFIG_")
#: Tamanho máximo de uma linha de evento lida do worker.
_MAX_EVENT_LINE = 1024 * 1024
#: O que o seguimento lê do EVENTS.jsonl de uma vez (mais que uma linha inteira): o que
#: o executor manda espera no disco, nunca na memória do despachante.
_MAX_TAIL_READ = 2 * _MAX_EVENT_LINE
#: Código da falha de um pod de execução que nunca subiu ou nunca buscou o trabalho.
EXECUTOR_NOT_STARTED = "executor_nao_iniciou"
#: Campos que uma fonte de segredos pode entregar: os do SECRETS.json e o certificado A1
#: em base64 (``pfx_base64``), que o lançador grava num arquivo antes de lançar.
SOURCE_SECRET_FIELDS = SECRET_FIELDS | {"pfx_base64"}
#: Maior certificado A1 aceito (um PFX tem poucos KiB).
_MAX_PFX_BYTES = 1024 * 1024
_SECRETS_CODE = "segredo_indisponivel"
#: Idade mínima de um pod ou Secret com o rótulo de execução e sem tentativa legível antes
#: de a varredura apagá-lo. Nenhum lançador cria isso, mas quem opera cria: o pod de
#: ``deploy/rede/teste-de-saida.yaml`` leva o rótulo (para ganhar a NetworkPolicy dos
#: robôs) e precisa viver o bastante para rodar e ter o log lido.
_UNLABELLED_GRACE_S = 15 * 60.0


class SecretsUnavailable(Exception):
    """Falta um segredo da execução: o executor nem é lançado.

    A mensagem diz o que falta pelo nome da credencial e do campo, nunca com valores.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class EventIngestor(Protocol):
    """Quem grava os eventos (no RaPA, :class:`rapa.control.services.PgRunQueue`)."""

    async def ingest_events(
        self, run_id: UUID, attempt: int, events: Sequence[RunEvent | Mapping[str, Any]]
    ) -> int: ...


class AttemptRegistry(Protocol):
    """Quem sabe que tentativas estão vivas (:meth:`rapa.control.services.PgRunQueue.live_attempts`)."""

    async def live_attempts(
        self, attempts: Collection[tuple[UUID, int]]
    ) -> set[tuple[UUID, int]]: ...


class SecretSource(Protocol):
    """Entrega os segredos das credenciais de uma execução, no formato do SECRETS.json.

    Devolve ``{nome_da_credencial: {campo: valor}}`` só para as credenciais que
    o manifesto declara; uma credencial ausente vira erro do passo que a usar.
    """

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]: ...


class NoSecrets:
    """Nenhum segredo: robôs sem credenciais."""

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        return {}


class _SecretsFormatError(ValueError):
    """Segredos em formato errado. A mensagem só cita nomes e tipos, nunca valores."""


def _check_credentials(company_id: UUID, credentials: object) -> dict[str, dict[str, str]]:
    """Confere as credenciais de uma empresa sem nunca pôr um valor na mensagem de erro."""
    if not isinstance(credentials, Mapping):
        raise _SecretsFormatError(f"as credenciais da empresa {company_id} precisam ser um objeto")
    checked: dict[str, dict[str, str]] = {}
    for name, fields in credentials.items():
        if not isinstance(name, str):
            raise _SecretsFormatError(
                f"as credenciais da empresa {company_id} precisam ter nomes de texto"
            )
        if not isinstance(fields, Mapping):
            # Uma senha escrita no lugar do objeto não pode aparecer na mensagem.
            raise _SecretsFormatError(
                f"a credencial '{name}' precisa ser um objeto com os campos do segredo"
            )
        if not all(isinstance(key, str) for key in fields):
            raise _SecretsFormatError(f"a credencial '{name}' precisa ter nomes de campo de texto")
        unknown = sorted(key for key in fields if key not in SOURCE_SECRET_FIELDS)
        if unknown:
            raise _SecretsFormatError(
                f"campos desconhecidos na credencial '{name}': {', '.join(unknown)}"
            )
        for key, value in fields.items():
            if not isinstance(value, str):
                raise _SecretsFormatError(
                    f"o campo '{key}' da credencial '{name}' precisa ser texto"
                )
        checked[name] = dict(fields)
    return checked


class StaticSecretSource:
    """Segredos fixos por empresa, para desenvolvimento e testes.

    Formato: ``{company_id: {nome_da_credencial: {campo: valor}}}``. Cada empresa
    só enxerga as próprias credenciais. As mensagens de erro citam só a empresa,
    o nome da credencial e o do campo, nunca o valor de um segredo.
    """

    def __init__(self, secrets: Mapping[UUID, Mapping[str, Mapping[str, str]]]) -> None:
        if not isinstance(secrets, Mapping):
            raise _SecretsFormatError("os segredos precisam ser um objeto por empresa")
        checked: dict[UUID, dict[str, dict[str, str]]] = {}
        for company_id, credentials in secrets.items():
            if not isinstance(company_id, UUID):
                raise _SecretsFormatError("cada empresa dos segredos precisa ser um UUID")
            checked[company_id] = _check_credentials(company_id, credentials)
        self._secrets = checked

    @classmethod
    def from_file(cls, path: Path | str) -> Self:
        """Lê o JSON ``{"<uuid da empresa>": {"credencial": {...}}}``."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("o arquivo de segredos não é um JSON válido") from None
        if not isinstance(data, dict):
            raise ValueError("o arquivo de segredos precisa ser um objeto JSON")
        companies: dict[UUID, Any] = {}
        for key, value in data.items():
            try:
                companies[UUID(key)] = value
            except ValueError:
                raise ValueError(
                    "arquivo de segredos inválido: cada chave precisa ser o UUID de uma empresa"
                ) from None
        try:
            return cls(companies)
        except _SecretsFormatError as exc:
            raise ValueError(f"arquivo de segredos inválido: {exc}") from None
        except Exception:
            # Texto de um erro inesperado pode trazer um valor do arquivo: mensagem fixa.
            raise ValueError("arquivo de segredos inválido") from None

    async def secrets_for(self, job: RunnerJob) -> Mapping[str, Mapping[str, str]]:
        available = self._secrets.get(job.company_id, {})
        wanted = {credential.name for credential in job.manifest.credentials}
        return {name: fields for name, fields in available.items() if name in wanted}


@dataclass(frozen=True)
class LocalLauncherSettings:
    """Configuração do lançador local."""

    #: Pasta onde ficam as pastas de trabalho de cada tentativa.
    work_dir: Path
    #: Pasta dos artefatos (telas e downloads), separada por empresa e execução.
    artifacts_dir: Path
    chromium: str | None = None
    browser_args: tuple[str, ...] = ()
    #: Pasta dos arquivos de entrada, com uma subpasta por empresa
    #: (``<files_dir>/<uuid da empresa>/<arquivo>``); arquivos soltos na raiz não valem.
    files_dir: Path | None = None
    python: str = field(default_factory=lambda: sys.executable)
    #: Intervalo de leitura dos eventos do worker.
    poll_s: float = 0.1
    #: Espera do worker pela confirmação antes de cada passo que grava.
    ack_timeout_s: float = 30.0
    #: Tempo entre o SIGTERM e o SIGKILL quando o prazo acaba ou a tentativa é cancelada.
    kill_grace_s: float = 10.0
    #: Folga além do prazo da execução antes de interromper o worker.
    deadline_grace_s: float = 15.0
    #: Mantém a pasta de trabalho depois do fim (só para depuração).
    keep_work_dir: bool = False
    #: Filma a execução e guarda o vídeo só se ela não der certo (``RAPA_VIDEO_EM_FALHAS``).
    video_on_failure: bool = False
    #: Limite para o worker fechar e guardar o vídeo, somado à folga depois do prazo.
    video_close_s: float = VIDEO_CLOSE_TIMEOUT_S
    #: A guarda da saída do navegador (``--guarda-de-saida``); ``None``, sem guarda.
    egress: EgressPolicy | None = None


class _EventTail:
    """Lê as linhas completas novas de EVENTS.jsonl, no máximo ``max_read`` bytes por vez."""

    def __init__(self, path: Path, *, max_read: int = _MAX_TAIL_READ) -> None:
        self.path = path
        self.max_read = max_read
        #: A última leitura parou no limite: o arquivo ainda tem o que ler.
        self.behind = False
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(self.max_read)
        except FileNotFoundError:
            self.behind = False
            return []
        self.behind = len(chunk) >= self.max_read
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > _MAX_EVENT_LINE:
            logger.error("linha de evento grande demais em %s; descartada", self.path.name)
            self._partial = b""
        out: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                logger.error("linha de evento inválida em %s; ignorada", self.path.name)
                continue
            out.append(parsed)
        return out


def child_env() -> dict[str, str]:
    """Ambiente mínimo de um processo filho (executor ou sessão de gravação).

    Só o que o Python, o Playwright e as fontes precisam: nada do plano de controle
    (URL do banco, chaves, tokens) passa para o filho.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWED or key.startswith(_ENV_PREFIXES)
    }


def _worker_env() -> dict[str, str]:
    return child_env()


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _write_private_bytes(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _pfx_bytes(name: str, encoded: str) -> bytes:
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        raise SecretsUnavailable(
            f"o certificado da credencial '{name}' não é um PFX em base64 válido"
        ) from None
    if not data or len(data) > _MAX_PFX_BYTES:
        raise SecretsUnavailable(f"o certificado da credencial '{name}' tem tamanho inválido")
    return data


def materialize_certificates(
    secrets: Mapping[str, Mapping[str, str]], directory: Path
) -> dict[str, dict[str, str]]:
    """Troca cada ``pfx_base64`` por um arquivo 0600 em ``directory`` (o ``pfx_path``).

    Devolve os segredos no formato do SECRETS.json. Um ``pfx_base64`` inválido levanta
    :class:`SecretsUnavailable` (sem o valor na mensagem). Os arquivos se chamam
    ``certificado-<n>.pfx``: o nome da credencial não vai para o disco.
    """
    out: dict[str, dict[str, str]] = {}
    for index, (name, fields) in enumerate(sorted(secrets.items()), start=1):
        entry = {key: value for key, value in fields.items() if key != "pfx_base64"}
        encoded = fields.get("pfx_base64")
        if encoded is not None:
            path = directory / f"certificado-{index}.pfx"
            _write_private_bytes(path, _pfx_bytes(name, encoded))
            entry["pfx_path"] = str(path)
        out[name] = entry
    return out


def _write_ack(directory: Path, seq: int) -> None:
    target = ack_path(directory, seq)
    temp = target.with_suffix(".tmp")
    temp.write_text("ok", encoding="utf-8")
    os.replace(temp, target)


class _Follower:
    """O que os dois lançadores fazem igual: gravar os eventos, confirmar e ler o resultado."""

    events: EventIngestor

    async def __call__(self, job: RunnerJob) -> dict[str, Any]:
        return await self.launch(job)

    async def launch(self, job: RunnerJob) -> dict[str, Any]:  # pragma: no cover - abstrato
        raise NotImplementedError

    async def _forward(
        self,
        job: RunnerJob,
        tail: _EventTail,
        work: Path,
        pending: list[dict[str, Any]],
        *,
        final: bool = False,
    ) -> None:
        """Grava os eventos novos e confirma o início dos passos que gravam.

        Gravar é idempotente (``seq``), então uma falha passageira do banco só
        adia a entrega para a próxima leitura; no fim, tenta mais algumas vezes.
        Só o que foi gravado é confirmado: o worker não começa um passo que grava
        antes de o banco saber dele. Uma tentativa antiga (:class:`StaleAttempt`)
        interrompe o lançamento. Um lote que o banco recusa pelo conteúdo fica de fora,
        sem confirmação (repetir não muda nada). Enquanto ``pending`` não é gravado,
        nada mais é lido: a memória fica em uma leitura (:attr:`_EventTail.max_read`), e
        o resto espera no disco. No fim (``final``), lê até o fim do arquivo.
        """
        while True:
            if not pending:
                pending.extend(await asyncio.to_thread(tail.read_new))
            if pending and not await self._ingest(job, work, pending, final=final):
                return
            if not (final and tail.behind):
                return

    async def _ingest(
        self, job: RunnerJob, work: Path, pending: list[dict[str, Any]], *, final: bool
    ) -> bool:
        """Grava ``pending`` e confirma; ``False`` se o banco não respondeu (fica para depois)."""
        tries = 4 if final else 1
        for attempt in range(1, tries + 1):
            try:
                await self.events.ingest_events(job.run_id, job.attempt, list(pending))
                break
            except StaleAttempt:
                raise
            except (InvalidInput, DataError, IntegrityError) as exc:
                # Recusado pelo conteúdo (um número fora da coluna, um caractere que o
                # banco não guarda, um evento malformado): repetir não muda nada. O lote
                # fica de fora sem nenhuma confirmação (o passo que esperava uma não
                # começa), em vez de ser repetido a cada leitura e crescer na memória.
                logger.error(
                    "%d evento(s) de %s recusados pelo banco: %s",
                    len(pending),
                    job.run_id,
                    type(exc).__name__,
                )
                pending.clear()
                return True
            except Exception as exc:
                logger.warning(
                    "falha ao gravar eventos de %s (tentativa %d): %s",
                    job.run_id,
                    attempt,
                    type(exc).__name__,
                )
                if attempt == tries:
                    return False
                await asyncio.sleep(0.25 * attempt)
        delivered, pending[:] = list(pending), []
        for raw in delivered:
            try:
                event = RunEvent.model_validate(raw)
            except ValidationError:
                continue
            if waits_for_ack(event):
                await asyncio.to_thread(_write_ack, work / "ack", event.seq)
                self._acknowledged(job)
        return True

    def _acknowledged(self, job: RunnerJob) -> None:
        """Uma confirmação acabou de ser escrita (o lançador de pods acorda o canal)."""

    @staticmethod
    def _read_result(job: RunnerJob, work: Path, returncode: int | None) -> dict[str, Any]:
        path = work / "result.json"
        try:
            result = RunResult.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            logger.error("o worker de %s saiu (código %s) sem resultado", job.run_id, returncode)
            return failure_result(
                job, "executor_sem_resultado", "O executor terminou sem entregar o resultado."
            )
        except ValidationError:
            logger.error("o worker de %s entregou um resultado inválido", job.run_id)
            return failure_result(
                job, "resultado_invalido", "O executor devolveu um resultado inválido."
            )
        if result.run_id != job.run_id or result.attempt != job.attempt:
            logger.error("o resultado do worker não é desta tentativa de %s", job.run_id)
            return failure_result(
                job, "resultado_invalido", "O executor devolveu o resultado de outra tentativa."
            )
        return result.model_dump(mode="json")


class LocalProcessLauncher(_Follower):
    """Lançador do despachante (``launch(job) -> dict``) com processos locais."""

    def __init__(
        self,
        events: EventIngestor,
        settings: LocalLauncherSettings,
        secrets: SecretSource | None = None,
    ) -> None:
        self.events = events
        self.settings = settings
        self.secrets: SecretSource = secrets or NoSecrets()

    @property
    def grace_s(self) -> float:
        """Folga além do prazo da execução antes de interromper o worker.

        Com o vídeo ligado, o limite para fechá-lo depois do fim da execução entra nela.
        """
        grace = self.settings.deadline_grace_s
        if self.settings.video_on_failure:
            grace += self.settings.video_close_s
        return grace

    def _paths(self, job: RunnerJob) -> tuple[Path, Path]:
        # Caminhos absolutos: o worker roda com a pasta da tentativa como diretório atual,
        # e uma pasta relativa da configuração seria lida a partir dela (os artefatos
        # iriam para dentro da pasta de trabalho, que some no fim).
        work = self.settings.work_dir.absolute() / f"{job.run_id}-{job.attempt}"
        artifacts = attempt_dir(
            self.settings.artifacts_dir.absolute(), job.company_id, job.run_id, job.attempt
        )
        return work, artifacts

    def _command(
        self, job: RunnerJob, work: Path, artifacts: Path, *, with_secrets: bool
    ) -> list[str]:
        settings = self.settings
        command = [
            settings.python,
            "-m",
            "rapa.runner.worker",
            "--job",
            str(work / "job.json"),
            "--result",
            str(work / "result.json"),
            "--events",
            str(work / "events.jsonl"),
            "--artifacts",
            str(artifacts),
            "--ack-dir",
            str(work / "ack"),
            f"--ack-timeout={settings.ack_timeout_s}",
        ]
        if with_secrets:
            command += ["--secrets", str(work / "secrets.json")]
        if settings.video_on_failure:
            # Dentro da pasta da tentativa: some com ela no fim, guardado ou não.
            command += ["--video-dir", str(work / "video")]
        if settings.chromium:
            command += ["--chromium", settings.chromium]
        if settings.files_dir is not None:
            # Só a pasta da empresa da tarefa: a execução de uma empresa nunca vê os
            # arquivos de outra, seja qual for o identificador pedido nas entradas.
            command += ["--files-dir", str(settings.files_dir.absolute() / str(job.company_id))]
        if settings.egress is not None:
            command += ["--guarda-de-saida", *settings.egress.worker_args()]
        command += [f"--browser-arg={arg}" for arg in settings.browser_args]
        return command

    def _env(self) -> dict[str, str]:
        """O ambiente do worker: o mínimo, mais usuário e senha do proxy de saída (nunca na
        linha de comando, que qualquer processo da máquina lê)."""
        egress = self.settings.egress
        return {**_worker_env(), **(egress.worker_env() if egress is not None else {})}

    async def launch(self, job: RunnerJob) -> dict[str, Any]:
        """Roda a tentativa num worker e devolve o RESULT.json.

        Falhas antes de o processo do worker existir (pasta, segredos, programa)
        levantam :class:`WorkerNotStarted`: nada pode ter rodado.
        """
        work, artifacts = self._paths(job)
        try:
            try:
                await asyncio.to_thread(self._prepare, job, work, artifacts)
                try:
                    secrets = await self.secrets.secrets_for(job)
                    prepared = await asyncio.to_thread(materialize_certificates, secrets, work)
                except SecretsUnavailable as exc:
                    # Sem o segredo, nada abre: o worker nem é iniciado.
                    logger.warning("execução %s sem os segredos; não lançada", job.run_id)
                    return failure_result(
                        job,
                        _SECRETS_CODE,
                        f"Os segredos da execução não estão disponíveis: {exc.message}.",
                        started=False,
                    )
                if prepared:
                    payload = json.dumps(prepared)
                    await asyncio.to_thread(_write_private, work / "secrets.json", payload)
                secrets = prepared
                process = await asyncio.create_subprocess_exec(
                    *self._command(job, work, artifacts, with_secrets=bool(secrets)),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env(),
                    cwd=work,
                    start_new_session=True,
                )
            except Exception as exc:
                raise WorkerNotStarted("o executor não pôde ser iniciado") from exc
            try:
                await self._follow(job, process, work)
            except BaseException:
                await self._stop(process)
                raise
            return await asyncio.to_thread(self._read_result, job, work, process.returncode)
        finally:
            await asyncio.to_thread(self._cleanup, work)

    def _prepare(self, job: RunnerJob, work: Path, artifacts: Path) -> None:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(mode=0o700)
        (work / "ack").mkdir(mode=0o700)
        artifacts.mkdir(parents=True, exist_ok=True)
        _write_private(work / "job.json", job.model_dump_json())

    async def _follow(
        self, job: RunnerJob, process: asyncio.subprocess.Process, work: Path
    ) -> None:
        tail = _EventTail(work / "events.jsonl")
        pending: list[dict[str, Any]] = []
        stderr = asyncio.create_task(self._drain_stderr(process))
        limit = (job.deadline - datetime.now(UTC)).total_seconds() + self.grace_s
        loop = asyncio.get_running_loop()
        give_up = loop.time() + max(limit, 1.0)
        try:
            while True:
                try:
                    await asyncio.wait_for(process.wait(), timeout=self.settings.poll_s)
                    finished = True
                except TimeoutError:
                    finished = False
                if finished:
                    await self._forward(job, tail, work, pending, final=True)
                    break
                await self._forward(job, tail, work, pending)
                if loop.time() >= give_up:
                    logger.warning("execução %s passou do prazo; interrompendo", job.run_id)
                    await self._stop(process)
                    await self._forward(job, tail, work, pending, final=True)
                    break
        except BaseException:
            # Cancelamento (tentativa antiga, encerramento) ou erro ao gravar eventos: o
            # worker para antes de qualquer espera. A saída de erro só fecha quando ele
            # sai; esperar por ela antes deixaria um worker antigo gravando em paralelo
            # com a nova tentativa e seguraria o encerramento sem limite.
            await asyncio.shield(self._stop(process))
            raise
        finally:
            if process.returncode is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr
            else:  # pragma: no cover - _stop sempre espera o processo sair
                stderr.cancel()

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        """Lê a saída de erro só para não travar o worker; registra apenas o tamanho."""
        if process.stderr is None:
            return
        total = 0
        while chunk := await process.stderr.read(65536):
            total += len(chunk)
        if total:
            logger.info("o worker escreveu %d bytes na saída de erro", total)

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.settings.kill_grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    def _cleanup(self, work: Path) -> None:
        secrets = work / "secrets.json"
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            secrets.unlink()
        # Os certificados A1 saem sempre, mesmo quando a pasta fica para depuração.
        with contextlib.suppress(OSError):
            for certificate in work.glob("certificado-*.pfx"):
                certificate.unlink(missing_ok=True)
        if not self.settings.keep_work_dir:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pods de uso único no Kubernetes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KubernetesLauncherSettings:
    """Configuração do lançador de pods (``RAPA_EXECUTOR=kubernetes``)."""

    #: Pasta local (no despachante) das pastas de trabalho das tentativas.
    work_dir: Path
    #: Pasta local dos artefatos, a mesma do store (``RAPA_PASTA_ARTEFATOS``).
    artifacts_dir: Path
    #: Como os pods rodam (``RAPA_K8S_*``).
    pod: PodSettings
    #: O endereço do canal que os pods usam, já com o IP deste despachante
    #: (:func:`rapa.control.kube.channel_address`).
    channel_address: str
    browser_args: tuple[str, ...] = ()
    #: Pasta dos arquivos de entrada, com uma subpasta por empresa (como no lançador
    #: local): os que as entradas de arquivo da tentativa citam vão para o pod pelo canal.
    files_dir: Path | None = None
    #: A guarda da saída, que no pod sempre vale (as redes internas e o proxy de saída).
    egress: EgressPolicy = field(default_factory=EgressPolicy)
    video_on_failure: bool = False
    video_close_s: float = VIDEO_CLOSE_TIMEOUT_S
    #: Intervalo de leitura dos eventos que o canal grava.
    poll_s: float = 0.1
    #: Intervalo entre duas consultas da fase do pod.
    status_interval_s: float = 2.0
    ack_timeout_s: float = 30.0
    #: Tempo entre o SIGTERM e o SIGKILL do worker (dentro do pod).
    kill_grace_s: float = 10.0
    #: Folga além do prazo da execução antes de apagar o pod.
    deadline_grace_s: float = 15.0
    #: Tempo para o agente entregar o resultado depois que o worker para.
    upload_grace_s: float = 20.0
    #: Quanto um pod pode ficar sem subir (agendamento, imagem) ou sem buscar o trabalho.
    start_timeout_s: float = 120.0
    #: Intervalo da varredura dos pods e Secrets órfãos.
    sweep_interval_s: float = 60.0
    keep_work_dir: bool = False


def pod_name(job: RunnerJob) -> str:
    """O nome do Secret e do Pod de uma tentativa (o mesmo para os dois)."""
    return f"rapa-execucao-{job.run_id}-{job.attempt}"


def executor_labels(job: RunnerJob) -> dict[str, str]:
    return {
        LABEL_APP: EXECUTOR_APP,
        "app.kubernetes.io/part-of": "rapa",
        LABEL_RUN: str(job.run_id),
        LABEL_COMPANY: str(job.company_id),
        LABEL_ATTEMPT: str(job.attempt),
    }


def _labelled_attempt(item: Mapping[str, Any]) -> tuple[str, tuple[UUID, int]] | None:
    """O nome e a tentativa de um pod ou Secret de execução, pelos rótulos."""
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = metadata.get("name")
    try:
        run_id = UUID(str(labels.get(LABEL_RUN)))
        attempt = int(str(labels.get(LABEL_ATTEMPT)))
    except ValueError:
        return None
    if not isinstance(name, str) or attempt < 1:
        return None
    return name, (run_id, attempt)


def _age_s(item: Mapping[str, Any], now: datetime) -> float | None:
    """A idade do objeto pelo ``creationTimestamp`` (``None`` se não der para ler)."""
    raw = (item.get("metadata") or {}).get("creationTimestamp")
    if not isinstance(raw, str):
        return None
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).total_seconds()


def _read_pfx(path: str) -> bytes | None:
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(_MAX_PFX_BYTES + 1)
    except OSError:
        return None
    return data if 0 < len(data) <= _MAX_PFX_BYTES else None


def _pod_secrets(secrets: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """Os segredos no formato do canal: o certificado A1 vai em ``pfx_base64``.

    Um ``pfx_path`` (o arquivo de desenvolvimento) é um caminho desta máquina: o arquivo
    é lido aqui e vai inteiro. Se não puder ser lido, o certificado fica de fora e o
    worker falha com ``segredo_indisponivel`` claro, sem abrir o navegador. Um
    ``pfx_base64`` da fonte (o OpenBao) é conferido aqui, como no lançador local: um que
    não é base64 ou tem tamanho inválido levanta :class:`SecretsUnavailable` (nenhum pod
    é criado).
    """
    out: dict[str, dict[str, str]] = {}
    for name, fields in secrets.items():
        converted = {key: value for key, value in fields.items() if key != "pfx_path"}
        if (encoded := converted.get("pfx_base64")) is not None:
            _pfx_bytes(name, encoded)
        path = fields.get("pfx_path")
        if path and "pfx_base64" not in converted:
            data = _read_pfx(path)
            if data is None:
                logger.warning("o certificado A1 de uma credencial não pôde ser lido")
            else:
                converted["pfx_base64"] = base64.b64encode(data).decode("ascii")
        out[name] = converted
    return out


@dataclass
class _Outcome:
    """Como terminou o seguimento do pod: com um problema na subida, ou não."""

    problem: tuple[str, str] | None = None


class KubernetesLauncher(_Follower):
    """Lançador do despachante com um pod de uso único por tentativa (ver o módulo)."""

    def __init__(
        self,
        events: EventIngestor,
        settings: KubernetesLauncherSettings,
        *,
        api: KubeApi,
        channel: Channel,
        secrets: SecretSource | None = None,
        attempts: AttemptRegistry | None = None,
    ) -> None:
        self.events = events
        self.settings = settings
        self.api = api
        self.channel = channel
        self.secrets: SecretSource = secrets or NoSecrets()
        self.attempts = attempts
        #: Tentativas que este processo lançou e ainda acompanha (a varredura não as toca).
        self._live: set[tuple[UUID, int]] = set()

    def __repr__(self) -> str:
        return f"KubernetesLauncher(namespace={self.settings.pod.namespace!r})"

    @property
    def grace_s(self) -> float:
        """Folga além do prazo da execução antes de apagar o pod (com o vídeo, mais)."""
        grace = self.settings.deadline_grace_s
        if self.settings.video_on_failure:
            grace += self.settings.video_close_s
        return grace

    def _paths(self, job: RunnerJob) -> tuple[Path, Path]:
        # Caminhos absolutos, como no lançador local: os caminhos dos artefatos do
        # RESULT.json são trocados pelos desta pasta e conferidos pelo store.
        work = self.settings.work_dir.absolute() / f"{job.run_id}-{job.attempt}"
        artifacts = attempt_dir(
            self.settings.artifacts_dir.absolute(), job.company_id, job.run_id, job.attempt
        )
        return work, artifacts

    def _options(self, files: Mapping[str, Path] | None = None) -> PodOptions:
        settings = self.settings
        proxy = settings.egress.upstream_proxy
        return PodOptions(
            egress_guard=True,
            internal_networks=tuple(str(net) for net in settings.egress.allowed_networks),
            # Pelo canal, como os segredos do robô; o agente do pod os passa ao worker pelo
            # ambiente, nunca pela linha de comando.
            egress_proxy=proxy.url_with_credentials if proxy is not None else None,
            video=settings.video_on_failure,
            ack_timeout_s=settings.ack_timeout_s,
            browser_args=settings.browser_args,
            grace_s=self.grace_s,
            files=tuple(files) if files is not None else None,
        )

    def _input_files(self, job: RunnerJob) -> dict[str, Path] | None:
        """Os arquivos de entrada que o pod pode buscar: os das entradas de arquivo.

        Só os da pasta da empresa da tarefa, com as regras do lançador local
        (:class:`rapa.runner.ports.LocalFileProvider`): um identificador inválido, de fora
        da pasta ou que não existe fica de fora, e o passo de envio falha com "arquivo
        não encontrado", como no local. Sem ``files_dir``, ``None`` (o worker fica sem
        pasta de arquivos, como no local).
        """
        if self.settings.files_dir is None:
            return None
        provider = LocalFileProvider(self.settings.files_dir / str(job.company_id))
        found: dict[str, Path] = {}
        for spec in job.manifest.inputs:
            value = job.inputs.get(spec.name)
            if spec.type is not FieldType.FILE or not isinstance(value, str) or value in found:
                continue
            try:
                found[value] = provider.resolve(value)
            except FileUnavailable:
                continue
        return found

    def _manifests(
        self, job: RunnerJob, ticket: str
    ) -> tuple[dict[str, Any], Callable[[Mapping[str, Any]], dict[str, Any]]]:
        settings = self.settings
        name = pod_name(job)
        labels = executor_labels(job)
        address = f"{settings.channel_address}/canal/{job.run_id}/{job.attempt}"
        secret = secret_manifest(
            settings.pod,
            name=name,
            labels=labels,
            data={"endereco": address.encode(), "bilhete": ticket.encode()},
        )
        remaining = (job.deadline - datetime.now(UTC)).total_seconds()
        stop_grace = settings.kill_grace_s + settings.upload_grace_s

        def pod(owner: Mapping[str, Any]) -> dict[str, Any]:
            return pod_manifest(
                settings.pod,
                name=name,
                labels=labels,
                owner=owner,
                args=["python", "-m", "rapa.runner.pod"],
                mount_path="/var/run/rapa/canal",
                # Mesmo sem o despachante, o pod não passa do prazo da execução.
                active_deadline_s=max(remaining, 1.0) + self.grace_s + stop_grace,
                termination_grace_s=stop_grace,
            )

        return secret, pod

    async def launch(self, job: RunnerJob) -> dict[str, Any]:
        """Roda a tentativa num pod e devolve o RESULT.json (ver o módulo).

        Falhas antes de o pod existir (pasta, segredos, a API do Kubernetes) levantam
        :class:`WorkerNotStarted` com o código ``executor_nao_iniciou``: nada pode ter
        rodado. Antes de decidir o resultado, a tentativa fecha no canal e os pedidos do
        pod que estavam no meio terminam: nada do pod chega depois da decisão.
        """
        work, artifacts = self._paths(job)
        name = pod_name(job)
        namespace = self.settings.pod.namespace
        key = (job.run_id, job.attempt)
        self._live.add(key)
        attempt: ChannelAttempt | None = None
        created = False
        try:
            try:
                await asyncio.to_thread(self._prepare, work, artifacts)
                try:
                    secrets = await asyncio.to_thread(
                        _pod_secrets, await self.secrets.secrets_for(job)
                    )
                except SecretsUnavailable as exc:
                    # Como no lançador local: sem o segredo, nada abre e nenhum pod é criado.
                    logger.warning("execução %s sem os segredos; nenhum pod criado", job.run_id)
                    return failure_result(
                        job,
                        _SECRETS_CODE,
                        f"Os segredos da execução não estão disponíveis: {exc.message}.",
                        started=False,
                    )
                files = await asyncio.to_thread(self._input_files, job)
                attempt, ticket = self.channel.open(
                    job,
                    work_dir=work,
                    artifacts_dir=artifacts,
                    secrets=secrets,
                    options=self._options(files).wire(),
                    files=files,
                )
                del secrets
                secret_body, pod_body = self._manifests(job, ticket)
                del ticket
                secret = await self.api.create(namespace, "secrets", secret_body)
                created = True
                await self.api.create(namespace, "pods", pod_body(secret))
            except KubeError as exc:
                raise WorkerNotStarted(
                    "o Kubernetes recusou o pod do executor",
                    code=EXECUTOR_NOT_STARTED,
                    public_message=(
                        f"O Kubernetes recusou o pod do executor ({exc.reason}, {exc.status}); "
                        "nenhum passo foi executado."
                    ),
                ) from exc
            except Exception as exc:
                raise WorkerNotStarted(
                    "o pod do executor não pôde ser criado",
                    code=EXECUTOR_NOT_STARTED,
                    public_message="O pod do executor não pôde ser criado; nenhum passo foi executado.",
                ) from exc
            outcome = await self._follow(job, attempt, work, name)
            # Fecha antes de decidir (sem espera no meio desde o seguimento): o pod não
            # busca mais o trabalho, e o que ele ainda enviava acaba antes de o resultado
            # ser lido e de o que ele não cita ser apagado.
            await self._close(attempt)
            return await asyncio.to_thread(self._result, job, work, artifacts, attempt, outcome)
        finally:
            if attempt is not None and not attempt.closed:
                await asyncio.shield(self._close(attempt))
            if created:
                await asyncio.shield(self._remove(name))
            await asyncio.to_thread(self._cleanup, work)
            self._live.discard(key)

    async def _close(self, attempt: ChannelAttempt) -> None:
        self.channel.close(attempt.run_id, attempt.attempt)
        if not await self.channel.drain(attempt):
            logger.warning("um envio do pod de %s não terminou a tempo", attempt.run_id)

    def _acknowledged(self, job: RunnerJob) -> None:
        attempt = self.channel.get(job.run_id, job.attempt)
        if attempt is not None:
            attempt.wake_acks()

    def _prepare(self, work: Path, artifacts: Path) -> None:
        self.settings.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(mode=0o700)
        (work / "ack").mkdir(mode=0o700)
        artifacts.mkdir(parents=True, exist_ok=True)

    async def _pod(self, name: str) -> PodState | None:
        """A situação do pod; ``None`` se a API não respondeu agora (tenta na próxima)."""
        try:
            return pod_state(await self.api.get(self.settings.pod.namespace, "pods", name))
        except Exception as exc:
            logger.warning("a API do Kubernetes não respondeu: %s", type(exc).__name__)
            return None

    async def _delete_pod(self, name: str, grace_s: float) -> None:
        try:
            await self.api.delete(
                self.settings.pod.namespace, "pods", name, grace_s=int(grace_s + 0.999)
            )
        except Exception as exc:
            logger.warning("o pod de uma execução não foi apagado: %s", type(exc).__name__)

    async def _remove(self, name: str) -> None:
        """Apaga o Secret (o coletor apaga o pod com ele) e o pod, sem esperar."""
        namespace = self.settings.pod.namespace
        for plural in ("secrets", "pods"):
            try:
                await self.api.delete(namespace, plural, name)
            except Exception as exc:
                # A varredura apaga depois o que ficar para trás.
                logger.warning("falha ao apagar %s de uma execução: %s", plural, type(exc).__name__)

    def _not_started(
        self, state: PodState, attempt: ChannelAttempt, overdue: bool
    ) -> tuple[str, str] | None:
        """O problema de um pod que não subiu (ou nunca buscou o trabalho), se houver."""
        if attempt.fetched:
            return None
        if state.stuck:
            return (
                EXECUTOR_NOT_STARTED,
                f"O pod do executor não iniciou ({state.reason}); nenhum passo foi executado.",
            )
        if not overdue:
            return None
        if state.phase in ("Pending", "Unknown"):
            reason = state.reason or state.phase
            return (
                EXECUTOR_NOT_STARTED,
                f"O pod do executor não iniciou a tempo ({reason}); nenhum passo foi executado.",
            )
        return (
            EXECUTOR_NOT_STARTED,
            "O pod do executor não falou com o despachante a tempo; nenhum passo foi executado.",
        )

    async def _follow(
        self, job: RunnerJob, attempt: ChannelAttempt, work: Path, name: str
    ) -> _Outcome:
        settings = self.settings
        tail = _EventTail(work / "events.jsonl")
        pending: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        limit = (job.deadline - datetime.now(UTC)).total_seconds() + self.grace_s
        give_up = loop.time() + max(limit, 1.0)
        start_by = loop.time() + settings.start_timeout_s
        next_status = loop.time() + settings.status_interval_s
        stop_grace = settings.kill_grace_s + settings.upload_grace_s
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(attempt.result_received.wait(), settings.poll_s)
                await self._forward(job, tail, work, pending)
                if attempt.result_received.is_set():
                    await self._forward(job, tail, work, pending, final=True)
                    return _Outcome()
                now = loop.time()
                if now >= next_status:
                    next_status = now + settings.status_interval_s
                    state = await self._pod(name)
                    if state is not None:
                        if state.finished:
                            # Sem resultado: o agente entrega antes de sair. O que chegou fica.
                            await self._forward(job, tail, work, pending, final=True)
                            return _Outcome()
                        problem = self._not_started(state, attempt, now >= start_by)
                        if problem is not None:
                            logger.error("o pod da execução %s não iniciou", job.run_id)
                            return _Outcome(problem)
                if now >= give_up:
                    logger.warning("execução %s passou do prazo; apagando o pod", job.run_id)
                    await self._delete_pod(name, stop_grace)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(attempt.result_received.wait(), stop_grace + 5.0)
                    await self._forward(job, tail, work, pending, final=True)
                    return _Outcome()
        except BaseException:
            # Cancelamento (tentativa antiga, encerramento) ou erro ao gravar eventos: o pod
            # sai já (o canal fecha logo em seguida, e nada mais dele é aceito).
            await asyncio.shield(self._delete_pod(name, settings.kill_grace_s))
            raise

    def _result(
        self,
        job: RunnerJob,
        work: Path,
        artifacts: Path,
        attempt: ChannelAttempt,
        outcome: _Outcome,
    ) -> dict[str, Any]:
        # A tentativa já fechou no canal: nada disto muda mais.
        received = attempt.result_received.is_set()
        if not received and not attempt.fetched:
            if outcome.problem is not None:
                code, message = outcome.problem
                return failure_result(job, code, message, started=False)
            return failure_result(
                job,
                EXECUTOR_NOT_STARTED,
                "O pod do executor terminou sem buscar o trabalho; nenhum passo foi executado.",
                started=False,
            )
        if not received:
            # Só vale o resultado que o canal recebeu inteiro antes de fechar.
            logger.error("o pod de %s terminou sem resultado", job.run_id)
            self._discard_unlisted(artifacts, ())
            return failure_result(
                job, "executor_sem_resultado", "O executor terminou sem entregar o resultado."
            )
        result = self._read_result(job, work, None)
        listed: set[str] = set()
        for item in result.get("artifacts") or []:
            name = item.get("name") if isinstance(item, dict) else None
            if isinstance(name, str) and valid_artifact_name(name):
                # O caminho do pod não vale aqui: o arquivo é o que o canal gravou.
                item["path"] = str(artifacts / name)
                listed.add(name)
        self._discard_unlisted(artifacts, listed)
        return result

    @staticmethod
    def _discard_unlisted(artifacts: Path, listed: Collection[str]) -> None:
        """Apaga o que o pod mandou e o resultado não cita (nunca vira evidência)."""
        with contextlib.suppress(FileNotFoundError):
            for entry in artifacts.iterdir():
                if entry.name not in listed:
                    with contextlib.suppress(OSError):
                        entry.unlink()

    def _cleanup(self, work: Path) -> None:
        if not self.settings.keep_work_dir:
            shutil.rmtree(work, ignore_errors=True)

    # -- órfãos --------------------------------------------------------------------

    async def sweep(self) -> int:
        """Apaga os pods e Secrets de execução sem tentativa viva; devolve quantos nomes.

        Viva é a tentativa que este processo acompanha ou que o banco tem como a atual
        de uma execução rodando (de qualquer réplica). Sem o banco (``attempts``),
        nada que não seja deste processo é apagado.
        """
        namespace = self.settings.pod.namespace
        selector = f"{LABEL_APP}={EXECUTOR_APP}"
        found: dict[str, tuple[UUID, int]] = {}
        unlabelled: set[str] = set()
        young: set[str] = set()
        now = datetime.now(UTC)
        for plural in ("pods", "secrets"):
            for item in await self.api.list(namespace, plural, label_selector=selector):
                name = (item.get("metadata") or {}).get("name")
                if not isinstance(name, str) or not name:
                    continue
                labelled = _labelled_attempt(item)
                if labelled is None:
                    # Rótulo de execução sem uma tentativa legível: nenhum lançador cria
                    # isso, então nunca é de uma tentativa viva (e não fica para sempre).
                    # Um recente fica por enquanto (o pod de teste da saída, de quem opera).
                    age = _age_s(item, now)
                    if age is not None and age < _UNLABELLED_GRACE_S:
                        young.add(name)
                    else:
                        unlabelled.add(name)
                elif labelled[1] not in self._live:
                    found[labelled[0]] = labelled[1]
        if (not found and not unlabelled) or self.attempts is None:
            return 0
        live = await self.attempts.live_attempts(set(found.values())) if found else set()
        removed = 0
        for name in sorted(unlabelled - set(found) - young):
            await self._remove(name)
            removed += 1
        for name, key in found.items():
            if key in live or key in self._live:
                continue
            await self._remove(name)
            removed += 1
        if removed:
            logger.warning("%d pod(s) de execução órfão(s) apagado(s)", removed)
        return removed

    async def run_sweeper(self, stop: asyncio.Event) -> None:
        """Varre na subida e a cada ``sweep_interval_s`` até ``stop``."""
        while not stop.is_set():
            try:
                await self.sweep()
            except Exception as exc:
                logger.warning("falha na varredura dos pods de execução: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.settings.sweep_interval_s)
