"""Vídeo da execução, guardado só quando ela não dá certo.

Com o vídeo ligado (``--video-dir`` do worker, ``RAPA_VIDEO_EM_FALHAS`` no
servidor), o contexto do navegador da execução filma a página do robô (no tamanho
da tela, 1280x800). No fim:

- se a execução deu certo (ou foi cancelada), o vídeo é apagado sem virar artefato;
- se falhou ou ficou indeterminada, vira o artefato ``video`` (``video-falha.webm``),
  também quando o worker foi interrompido (o SIGTERM do lançador no fim do prazo) e a
  execução ficou indeterminada.

A gravação é só observação: nada nela muda a execução. Se o vídeo não abre (sem o
``ffmpeg`` do Playwright, por exemplo), a execução roda sem vídeo, num contexto
igual ao de sempre; um erro ao fechar ou guardar o vídeo só o deixa de fora; e o
tempo para fechá-lo tem limite próprio (:data:`VIDEO_CLOSE_TIMEOUT_S`), que entra
na folga do lançador depois do prazo da execução.

O vídeo mostra a tela como ela é. As capturas de tela mascaram os campos onde o
executor digitou uma credencial; o vídeo não consegue: um campo de senha aparece
como pontos, mas o que for digitado num campo comum aparece. Por isso o vídeo só é
guardado quando nenhuma credencial do cofre foi para fora de um campo
``type=password``:

- :func:`video_allowed` recusa antes de começar o robô que digita o usuário ou o
  código TOTP do cofre (eles vão para campos comuns): esse robô nem é filmado;
- durante a execução, o campo de cada senha é conferido antes e depois de digitar,
  e o tipo dele é vigiado enquanto ela é digitada
  (:attr:`rapa.runner.interpreter.Interpreter.video_unsafe`): um campo comum antes
  de digitar (um "mostrar senha" ligado), um tipo que muda no meio ou um campo comum
  depois deixam o vídeo de fora, e na dúvida também (falha fechada).

As entradas da execução digitadas em campos comuns aparecem no vídeo, como nas
capturas de tela; o acesso ao vídeo segue a mesma regra das capturas.

O vídeo precisa do ``ffmpeg`` do Playwright na máquina do executor (``playwright
install ffmpeg``); o Chromium do sistema não traz um. :func:`ffmpeg_installed` diz se
ele está onde o Playwright procura, para o servidor avisar no log quando o vídeo está
ligado e nada vai ser filmado.
"""

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from rapa.core.contracts import RunState
from rapa.core.robot import FillStep, Manifest, SecretValue

__all__ = [
    "KEEP_STATES",
    "VIDEO_CLOSE_TIMEOUT_S",
    "VIDEO_NAME",
    "VIDEO_PROBE_TIMEOUT_S",
    "ffmpeg_installed",
    "video_allowed",
]

#: Estados em que o vídeo vira artefato; em qualquer outro, ele é apagado.
KEEP_STATES = frozenset({RunState.FAILED, RunState.INDETERMINATE})
#: Limite, em segundos, para fechar o contexto (o que termina o vídeo) e guardá-lo.
VIDEO_CLOSE_TIMEOUT_S = 10.0
#: Limite, em segundos, para conferir que o vídeo abre (a página de teste do contexto).
VIDEO_PROBE_TIMEOUT_S = 5.0
#: Nome do artefato do vídeo.
VIDEO_NAME = "video-falha.webm"


def video_allowed(manifest: Manifest) -> bool:
    """Se o vídeo da execução deste robô pode ser guardado, olhando só o manifesto.

    Falso quando algum passo digita uma credencial do cofre que não é a senha (o
    usuário ou o código TOTP): esses valores vão para campos comuns, que o vídeo
    mostra como são. A senha só é aceita se, na hora, o campo for
    ``<input type=password>`` (o executor confere).
    """
    for step, _ in manifest.iter_steps():
        if (
            isinstance(step, FillStep)
            and isinstance(step.value, SecretValue)
            and step.value.field != "password"
        ):
            return False
    return True


#: O nome do ``ffmpeg`` do Playwright em cada sistema.
_FFMPEG_EXECUTABLES = {"linux": "ffmpeg-linux", "darwin": "ffmpeg-mac", "win32": "ffmpeg-win64.exe"}


def ffmpeg_installed(env: Mapping[str, str] | None = None) -> bool | None:
    """Se o ``ffmpeg`` que o Playwright usa para filmar está instalado; ``None`` se não dá
    para saber.

    O Playwright procura ``<pasta dos navegadores>/ffmpeg-<revisão>/<executável>``: a
    pasta é ``PLAYWRIGHT_BROWSERS_PATH`` (``0`` é a do próprio pacote) ou o cache do
    usuário, e a revisão vem do ``browsers.json`` do pacote. Só lê o disco.
    """
    env = os.environ if env is None else env
    executable = _FFMPEG_EXECUTABLES.get(sys.platform)
    try:
        import playwright

        package = Path(playwright.__file__).parent / "driver" / "package"
        browsers = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
        revision = next(
            str(item["revision"]) for item in browsers["browsers"] if item.get("name") == "ffmpeg"
        )
    except ImportError, OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration:
        return None
    if executable is None:
        return None
    configured = env.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured == "0":
        base = package / ".local-browsers"
    elif configured:
        base = Path(configured)
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
    elif sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or Path.home()) / "ms-playwright"
    else:
        base = Path(env.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
    return (base / f"ffmpeg-{revision}" / executable).is_file()
