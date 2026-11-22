"""Regras dos artefatos de uma execução que valem em todas as camadas.

Um artefato é um arquivo que a execução guardou: uma tela (``screenshot``), um
arquivo baixado (``download``) ou o vídeo da falha (``video``). O nome dele, já
saneado pelo executor (só ``[A-Za-z0-9._-]``), é o identificador que a API, a
interface e as saídas da execução usam; o caminho no disco nunca sai do plano de
controle.

As telas e o vídeo são capturas da tela (:func:`is_capture`); os arquivos baixados são
o que a execução produziu. Na retenção, as capturas de uma execução que deu certo saem
pelo prazo curto, e os arquivos baixados ficam pelo prazo das evidências.

Ao servir um artefato: só a tela em PNG e o vídeo em WebM abrem na própria página
(``inline``); todo o resto vai como anexo, com o tipo do arquivo tirado de uma lista
curta e ``application/octet-stream`` para o que não estiver nela. As respostas
nunca ficam em cache e o navegador não pode adivinhar outro tipo (``nosniff``).
"""

import re
from pathlib import PurePosixPath

__all__ = [
    "DOWNLOAD",
    "SCREENSHOT",
    "VIDEO",
    "artifact_headers",
    "is_capture",
    "media_type_for",
    "recorded_artifact_name",
    "valid_artifact_name",
]

#: Tipos de artefato (os mesmos de ``rapa.runner.result.ArtifactKind``).
SCREENSHOT = "screenshot"
DOWNLOAD = "download"
VIDEO = "video"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
#: O que abre na própria página: a tela e o vídeo, cada um só no formato do executor.
_INLINE = {(SCREENSHOT, ".png"): "image/png", (VIDEO, ".webm"): "video/webm"}
#: Tipos dos arquivos baixados que vão com o tipo certo (sempre como anexo).
_DOWNLOAD_TYPES = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".json": "application/json",
    ".zip": "application/zip",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_OCTET_STREAM = "application/octet-stream"


def valid_artifact_name(name: str) -> bool:
    """Se ``name`` pode ser o nome de um artefato: sem barras, sem ``..`` e só ``[A-Za-z0-9._-]``."""
    return bool(_NAME.fullmatch(name)) and ".." not in name


def recorded_artifact_name(name: str) -> bool:
    """Se ``name`` pode ser o nome de um artefato já registrado: a regra de
    :func:`valid_artifact_name` sem a proibição de ``..``.

    O saneador do executor de antes da revisão 0008 deixava pontos seguidos
    (``nota..fiscal.pdf``), e esses nomes ficaram em ``runs.artifacts``. Serve só para
    comparar com o que está na pasta da tentativa (a retenção, ao deixar os arquivos
    baixados no prazo curto), nunca para montar um caminho.
    """
    return bool(_NAME.fullmatch(name))


def is_capture(kind: str) -> bool:
    """Se o artefato é uma captura da tela (a tela ou o vídeo): tudo que não é download.

    Na retenção, as capturas de uma execução que deu certo (ou foi cancelada) saem pelo
    prazo curto; os arquivos baixados ficam pelo prazo das evidências. Um tipo
    desconhecido conta como captura (sai antes): só o arquivo baixado é evidência.
    """
    return kind != DOWNLOAD


def media_type_for(kind: str, name: str) -> tuple[str, bool]:
    """O tipo do arquivo e se ele abre na própria página (só a tela PNG e o vídeo WebM)."""
    suffix = PurePosixPath(name).suffix.lower()
    inline = _INLINE.get((kind, suffix))
    if inline is not None:
        return inline, True
    return _DOWNLOAD_TYPES.get(suffix, _OCTET_STREAM), False


def artifact_headers(name: str, *, inline: bool, size: int | None = None) -> dict[str, str]:
    """Cabeçalhos da resposta que entrega um artefato (pela API ou pela interface).

    ``name`` precisa ter passado por :func:`valid_artifact_name` (cabe entre aspas
    sem escapar nada).
    """
    if not valid_artifact_name(name):
        raise ValueError("nome de artefato inválido")
    headers = {
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        # Aberto direto no navegador, o arquivo nunca roda script nem carrega nada.
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if size is not None:
        headers["Content-Length"] = str(size)
    return headers
