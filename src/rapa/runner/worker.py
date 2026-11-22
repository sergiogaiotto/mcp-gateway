"""Worker do executor: roda uma execução descrita em arquivos.

Uso::

    python -m rapa.runner.worker --job JOB.json --result RESULT.json \\
        --events EVENTS.jsonl --artifacts DIR [--secrets SECRETS.json] \\
        [--chromium CAMINHO] [--browser-arg=ARG ...] [--files-dir DIR] \\
        [--ack-dir DIR [--ack-timeout SEGUNDOS]] [--video-dir DIR] \\
        [--guarda-de-saida [--rede-interna-permitida CIDR ...] [--proxy-saida URL]]

- ``JOB.json`` é um :class:`rapa.core.contracts.RunnerJob` em JSON.
- ``SECRETS.json`` associa o nome de cada credencial a um objeto com os campos
  opcionais ``username``, ``password``, ``totp_secret`` (base32), ``pfx_path`` e
  ``pfx_password``. O worker apaga o arquivo logo depois de ler.
- ``EVENTS.jsonl`` recebe um :class:`rapa.core.events.RunEvent` por linha.
- ``RESULT.json`` recebe o :class:`rapa.runner.result.RunResult`.
- Com ``--ack-dir``, antes de cada passo que grava (efeito ``write`` ou
  ``irreversible``) o worker espera o arquivo ``<seq>.ack`` nesse diretório,
  criado por quem acompanha a execução depois de gravar o início do passo (ver
  :class:`rapa.runner.ports.AckingEventSink` e
  :func:`rapa.runner.ports.waits_for_ack`). Sem ele no prazo, o passo falha
  com ``sem_confirmacao`` sem ter começado. A validação segura para antes do
  primeiro passo que grava e não espera nada.
- Com ``--video-dir``, a página do robô é filmada nessa pasta temporária; o vídeo
  vira o artefato ``video`` só se a execução falhar ou ficar indeterminada e o
  robô não digitar credencial fora de um campo de senha (ver
  :mod:`rapa.runner.video`). A gravação nunca muda a execução: sem vídeo
  disponível, o robô roda sem ele, e fechar o vídeo tem limite próprio.
- Com ``--guarda-de-saida``, o Chromium da execução sai por uma
  :class:`rapa.session.egress.EgressGuard` local (a mesma do hospedeiro da sessão:
  QUIC desligado, WebRTC sem UDP fora do proxy e fora das páginas, interruptores de
  proxy do operador removidos) que só deixa passar os ``allowed_domains`` do manifesto
  e recusa a própria máquina, o link-local, o metadata da nuvem e toda rede que não é
  da internet, menos as de ``--rede-interna-permitida``
  (``RAPA_REDES_INTERNAS_PERMITIDAS``). Com ``--proxy-saida`` (``RAPA_PROXY_SAIDA``,
  um proxy HTTP CONNECT), a internet sai por ele. O ``route`` do interpretador
  continua: é ele que dá o erro ``dominio_nao_permitido`` claro no passo; a guarda
  cobre o que ele não vê (websockets, os saltos de um redirecionamento, um nome que
  resolve para a rede interna). O lançador sempre passa a guarda nos pods; no
  lançador local, quando há redes internas permitidas ou proxy de saída, e sempre
  fora do modo de desenvolvimento.

O código de saída é 0 sempre que o RESULT.json foi escrito, qualquer que seja o
estado da execução; diferente de 0 só quando o próprio worker quebra. Argumentos
do navegador que começam com ``--`` vão na forma ``--browser-arg=--opcao``.
SIGTERM e SIGINT cancelam a execução entre dois passos.
"""

import argparse
import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from pydantic import ValidationError

from rapa.core.contracts import RunnerJob
from rapa.core.robot import CredentialKind
from rapa.runner.browser import (
    ClientCertificate,
    launch_browser,
    new_run_context,
    open_browser,
    run_context,
)
from rapa.runner.errors import ErrorCode
from rapa.runner.interpreter import Interpreter
from rapa.runner.ports import (
    AckingEventSink,
    ArtifactSink,
    DictSecretProvider,
    DirectoryArtifactSink,
    EventSink,
    JsonlEventSink,
    LocalFileProvider,
    SecretUnavailable,
)
from rapa.runner.result import ArtifactKind, ArtifactRef, RunResult
from rapa.runner.video import (
    KEEP_STATES,
    VIDEO_CLOSE_TIMEOUT_S,
    VIDEO_NAME,
    VIDEO_PROBE_TIMEOUT_S,
    video_allowed,
)
from rapa.session.egress import (
    BROWSER_NETWORK_ARGS,
    EgressGuard,
    EgressPolicy,
    without_proxy_switches,
)

__all__ = ["build_parser", "main", "run_job", "write_result"]

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_JOB = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rapa.runner.worker",
        description="Executa um robô do RaPA a partir de um trabalho em JSON.",
    )
    parser.add_argument("--job", required=True, type=Path, help="RunnerJob em JSON")
    parser.add_argument("--result", required=True, type=Path, help="onde escrever o RunResult")
    parser.add_argument(
        "--events", required=True, type=Path, help="arquivo JSONL que recebe os eventos"
    )
    parser.add_argument("--artifacts", required=True, type=Path, help="diretório dos artefatos")
    parser.add_argument(
        "--secrets", type=Path, default=None, help="SECRETS.json (apagado depois de lido)"
    )
    parser.add_argument("--chromium", default=None, help="caminho do Chromium")
    parser.add_argument(
        "--browser-arg",
        dest="browser_args",
        action="append",
        default=[],
        metavar="ARG",
        help="argumento extra do Chromium (use --browser-arg=--opcao)",
    )
    parser.add_argument(
        "--files-dir", type=Path, default=None, help="diretório dos arquivos de entrada"
    )
    parser.add_argument(
        "--ack-dir",
        type=Path,
        default=None,
        help="diretório das confirmações antes de cada passo que grava",
    )
    parser.add_argument(
        "--ack-timeout",
        type=float,
        default=30.0,
        help="segundos de espera pela confirmação (padrão: 30)",
    )
    parser.add_argument(
        "--screenshot-every-step",
        action="store_true",
        help="guardar a tela depois de todos os passos",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=None,
        help="pasta temporária do vídeo da execução (guardado só se ela não der certo)",
    )
    parser.add_argument(
        "--guarda-de-saida",
        dest="egress_guard",
        action="store_true",
        help="o navegador sai pela guarda da saída (só os domínios do robô, sem rede interna)",
    )
    parser.add_argument(
        "--rede-interna-permitida",
        dest="internal_networks",
        action="append",
        default=[],
        metavar="CIDR",
        help="rede interna que o robô pode acessar (com --guarda-de-saida)",
    )
    parser.add_argument(
        "--proxy-saida",
        dest="egress_proxy",
        default=None,
        metavar="URL",
        help="proxy HTTP CONNECT de saída para a internet (com --guarda-de-saida)",
    )
    return parser


def write_result(path: Path, result: RunResult) -> None:
    """Escreve o resultado de forma atômica (arquivo temporário + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".result-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(result.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


async def _client_certificates(
    job: RunnerJob, secrets: DictSecretProvider
) -> list[ClientCertificate]:
    certificates: list[ClientCertificate] = []
    for credential in job.manifest.credentials:
        if credential.kind is not CredentialKind.PFX_A1:
            continue
        cert: ClientCertificate = {
            "origin": credential.origin,
            "pfxPath": await secrets.get(credential, "pfx_path"),
        }
        with contextlib.suppress(SecretUnavailable):
            cert["passphrase"] = await secrets.get(credential, "pfx_password")
        certificates.append(cert)
    return certificates


def _remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


async def _close_browser(browser: Browser) -> None:
    """Fecha o navegador da execução filmada com limite (o Playwright para depois)."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(browser.close(), timeout=VIDEO_CLOSE_TIMEOUT_S)


class _Video:
    """O vídeo de uma execução: a pasta temporária e a página do robô que ele filma."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.pages: list[Page] = []

    def watch(self, context: BrowserContext) -> None:
        """Anota as páginas abertas daqui em diante (a primeira é a do robô).

        Um erro aqui só deixa o vídeo de fora (sem página anotada, nada é guardado).
        """
        with contextlib.suppress(Exception):
            context.on("page", self._opened)

    def _opened(self, page: Page) -> None:
        self.pages.append(page)

    async def settle(
        self,
        result: RunResult,
        context: BrowserContext,
        interpreter: Interpreter,
        sink: ArtifactSink,
    ) -> RunResult:
        """Fecha o contexto (o que termina o vídeo) e guarda o vídeo, se for o caso.

        Fica de fora quando a execução deu certo ou foi cancelada, quando uma credencial
        pode aparecer nele, e em qualquer erro ou demora: o resultado da execução nunca
        muda por causa do vídeo. Uma execução interrompida pelo worker (o SIGTERM do
        lançador no fim do prazo) que fica indeterminada ou falha guarda o vídeo como
        qualquer outra: é o caso em que ele mais ajuda.
        """
        keep = result.state in KEEP_STATES and not interpreter.video_unsafe
        ref: ArtifactRef | None = None
        try:
            ref = await asyncio.wait_for(
                self._close_and_save(context, sink, keep=keep), timeout=VIDEO_CLOSE_TIMEOUT_S
            )
        except Exception:  # prazo, navegador que caiu, disco cheio: só o vídeo fica de fora
            ref = None
        finally:
            await asyncio.to_thread(_remove, self.directory)
        if ref is None:
            return result
        return result.model_copy(update={"artifacts": [*result.artifacts, ref]})

    async def _close_and_save(
        self, context: BrowserContext, sink: ArtifactSink, *, keep: bool
    ) -> ArtifactRef | None:
        video = self.pages[0].video if self.pages else None
        source = Path(await video.path()) if keep and video is not None else None
        await context.close()
        if source is None or not source.is_file() or source.stat().st_size == 0:
            return None
        return await sink.save_file(ArtifactKind.VIDEO, VIDEO_NAME, source)


async def _video_context(
    browser: Browser,
    certificates: Sequence[ClientCertificate],
    directory: Path,
    *,
    webrtc_off: bool = False,
) -> BrowserContext | None:
    """Contexto filmado, ou ``None`` se o vídeo não abre (sem o ``ffmpeg``, por exemplo).

    Uma página de teste confere que o vídeo abre antes de o robô abrir a dele; ela é
    fechada e o vídeo dela, apagado. Sem vídeo, quem chama abre o contexto de sempre.
    """
    try:
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True, mode=0o700)
        context = await new_run_context(
            browser,
            client_certificates=certificates,
            record_video_dir=directory,
            webrtc_off=webrtc_off,
        )
    except Exception:
        return None
    try:
        probe = await asyncio.wait_for(context.new_page(), timeout=VIDEO_PROBE_TIMEOUT_S)
        await probe.close()
        if probe.video is not None:
            await asyncio.wait_for(probe.video.delete(), timeout=VIDEO_PROBE_TIMEOUT_S)
    except Exception:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(context.close(), timeout=VIDEO_CLOSE_TIMEOUT_S)
        return None
    return context


async def run_job(
    job: RunnerJob,
    *,
    secrets: DictSecretProvider | None,
    events: EventSink,
    artifacts_dir: Path,
    files_dir: Path | None = None,
    chromium: str | None = None,
    browser_args: Sequence[str] = (),
    screenshot_every_step: bool = False,
    cancel: asyncio.Event | None = None,
    secrets_error: str | None = None,
    video_dir: Path | None = None,
    egress: EgressPolicy | None = None,
) -> RunResult:
    """Roda o trabalho num Chromium novo e devolve o resultado.

    Não levanta exceção por falhas da execução: navegador que não abre ou
    segredos inválidos também viram um resultado "falhou". Com ``video_dir``, a
    página do robô é filmada (ver :mod:`rapa.runner.video`). Com ``egress``, o
    navegador sai pela guarda da saída (:func:`_guarded_network`).
    """
    provider = secrets if secrets is not None else DictSecretProvider({})
    sink = DirectoryArtifactSink(artifacts_dir)

    def interpreter_for(record_video: bool = False) -> Interpreter:
        return Interpreter.from_job(
            job,
            secrets=provider,
            files=LocalFileProvider(files_dir),
            artifacts=sink,
            events=events,
            cancel=cancel,
            screenshot_every_step=screenshot_every_step,
            redact_values=provider.sensitive_values(),
            record_video=record_video,
        )

    interpreter = interpreter_for()
    if secrets_error is not None:
        return await interpreter.fail_without_browser(
            ErrorCode.SEGREDO_INDISPONIVEL,
            f"Os segredos da execução são inválidos: {secrets_error}",
        )
    try:
        certificates = await _client_certificates(job, provider)
    except SecretUnavailable as exc:
        return await interpreter.fail_without_browser(
            ErrorCode.SEGREDO_INDISPONIVEL, f"O certificado A1 não está disponível: {exc}"
        )
    async with contextlib.AsyncExitStack() as outer:
        try:
            network = await outer.enter_async_context(_guarded_network(job, browser_args, egress))
        except Exception as exc:
            return await interpreter.fail_without_browser(
                ErrorCode.NAVEGADOR_INDISPONIVEL,
                f"Não consegui abrir a guarda da saída do navegador ({type(exc).__name__}).",
            )
        if video_dir is not None and video_allowed(job.manifest):
            return await _run_filmed(
                interpreter,
                interpreter_for,
                sink,
                certificates,
                video_dir,
                chromium=chromium,
                network=network,
            )
        async with contextlib.AsyncExitStack() as stack:
            try:
                browser = await stack.enter_async_context(
                    open_browser(executable_path=chromium, args=network.args, proxy=network.proxy)
                )
                context = await stack.enter_async_context(
                    run_context(
                        browser,
                        client_certificates=certificates,
                        webrtc_off=network.webrtc_off,
                    )
                )
            except Exception as exc:
                return await interpreter.fail_without_browser(
                    ErrorCode.NAVEGADOR_INDISPONIVEL,
                    f"Não consegui abrir o navegador ({type(exc).__name__}).",
                )
            return await interpreter.run(context)


@dataclass(frozen=True, slots=True)
class _Network:
    """Como o navegador da execução sai: os argumentos, o proxy da guarda e o WebRTC."""

    args: tuple[str, ...]
    proxy: str | None = None
    webrtc_off: bool = False


@contextlib.asynccontextmanager
async def _guarded_network(
    job: RunnerJob, browser_args: Sequence[str], egress: EgressPolicy | None
) -> AsyncIterator[_Network]:
    """A guarda da saída da execução, aberta até o fim do bloco (o navegador fecha antes).

    Sem ``egress``, o navegador sai direto, como sempre (só o ``route`` do
    interpretador). Com ele, só os ``allowed_domains`` do manifesto passam, e a rede
    interna fica fechada, menos as redes permitidas da política (ver
    :mod:`rapa.session.egress`). Os interruptores de proxy do operador saem dos
    argumentos; os que tiram o UDP do caminho vêm por último, para não serem desfeitos.
    """
    if egress is None:
        yield _Network(args=tuple(browser_args))
        return
    guard = EgressGuard.from_browser_args(
        browser_args, allowed_hosts=job.manifest.allowed_domains, policy=egress
    )
    port = await guard.start()
    try:
        yield _Network(
            args=(*without_proxy_switches(browser_args), *BROWSER_NETWORK_ARGS),
            proxy=f"socks5://127.0.0.1:{port}",
            webrtc_off=True,
        )
    finally:
        await guard.close()


async def _run_filmed(
    interpreter: Interpreter,
    interpreter_for: Callable[[bool], Interpreter],
    sink: ArtifactSink,
    certificates: Sequence[ClientCertificate],
    video_dir: Path,
    *,
    chromium: str | None,
    network: _Network,
) -> RunResult:
    """Como o caminho de sempre de :func:`run_job`, com o contexto filmado.

    Se o vídeo não abre, o contexto é o de sempre e o robô roda sem vídeo. O contexto
    filmado fecha dentro do limite do vídeo (:class:`_Video`), e o navegador também
    fecha com limite: um vídeo preso nunca segura o resultado da execução.
    """
    async with contextlib.AsyncExitStack() as stack:
        try:
            playwright = await stack.enter_async_context(async_playwright())
            browser = await launch_browser(
                playwright, executable_path=chromium, args=network.args, proxy=network.proxy
            )
        except Exception as exc:
            return await interpreter.fail_without_browser(
                ErrorCode.NAVEGADOR_INDISPONIVEL,
                f"Não consegui abrir o navegador ({type(exc).__name__}).",
            )
        stack.push_async_callback(_close_browser, browser)
        filmed = await _video_context(
            browser, certificates, video_dir, webrtc_off=network.webrtc_off
        )
        if filmed is None:
            await asyncio.to_thread(_remove, video_dir)
            try:
                context = await stack.enter_async_context(
                    run_context(
                        browser, client_certificates=certificates, webrtc_off=network.webrtc_off
                    )
                )
            except Exception as exc:
                return await interpreter.fail_without_browser(
                    ErrorCode.NAVEGADOR_INDISPONIVEL,
                    f"Não consegui abrir o navegador ({type(exc).__name__}).",
                )
            return await interpreter.run(context)
        video = _Video(video_dir)
        video.watch(filmed)
        recorded = interpreter_for(True)
        try:
            result = await recorded.run(filmed)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(filmed.close(), timeout=VIDEO_CLOSE_TIMEOUT_S)
            await asyncio.to_thread(_remove, video_dir)
            raise
        return await video.settle(result, filmed, recorded, sink)


def _load_secrets(path: Path | None) -> tuple[DictSecretProvider | None, str | None]:
    if path is None:
        return None, None
    try:
        return DictSecretProvider.from_file(path, delete=True), None
    except FileNotFoundError:
        return None, "o arquivo de segredos não existe"
    except ValueError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, type(exc).__name__


async def _main_async(
    args: argparse.Namespace,
    job: RunnerJob,
    secrets: DictSecretProvider | None,
    secrets_error: str | None,
    egress: EgressPolicy | None = None,
) -> RunResult:
    # SIGTERM e SIGINT cancelam a execução entre dois passos; o resultado sai mesmo assim.
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, cancel.set)
    with JsonlEventSink(args.events) as jsonl:
        events: EventSink = jsonl
        if args.ack_dir is not None:
            events = AckingEventSink(jsonl, args.ack_dir, timeout_s=args.ack_timeout)
        return await run_job(
            job,
            secrets=secrets,
            secrets_error=secrets_error,
            events=events,
            artifacts_dir=args.artifacts,
            files_dir=args.files_dir,
            chromium=args.chromium,
            browser_args=args.browser_args,
            screenshot_every_step=args.screenshot_every_step,
            cancel=cancel,
            video_dir=args.video_dir,
            egress=egress,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.ack_timeout > 0:
        parser.error("--ack-timeout precisa ser maior que zero")
    egress: EgressPolicy | None = None
    if args.egress_guard:
        try:
            egress = EgressPolicy.parse(args.internal_networks, args.egress_proxy)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.internal_networks or args.egress_proxy:
        parser.error("--rede-interna-permitida e --proxy-saida só valem com --guarda-de-saida")
    # Os segredos saem do disco antes de qualquer outra coisa.
    secrets, secrets_error = _load_secrets(args.secrets)
    try:
        job = RunnerJob.model_validate_json(args.job.read_bytes())
    except (OSError, ValidationError) as exc:
        detail = type(exc).__name__ if isinstance(exc, OSError) else f"{exc.error_count()} erro(s)"
        print(f"rapa.runner.worker: trabalho inválido em {args.job} ({detail})", file=sys.stderr)
        return EXIT_BAD_JOB
    result = asyncio.run(_main_async(args, job, secrets, secrets_error, egress))
    write_result(args.result, result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
