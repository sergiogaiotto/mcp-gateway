"""Armazenamento dos artefatos das execuções no plano de controle.

O executor guarda as telas, os arquivos baixados e o vídeo da falha numa pasta da
tentativa (:func:`attempt_dir`). Quando a execução termina, o despachante registra
cada um pelo :class:`ArtifactStore` (``put``): o que vale dali em diante é o que o
store devolve (:class:`ArtifactInfo`, sem caminho), que vai para ``runs.artifacts``.
A API lê pelo mesmo store (``open``) e a retenção apaga por ele (``delete_run``).

:class:`LocalArtifactStore` é a disposição de pastas de sempre:
``<RAPA_PASTA_ARTEFATOS>/<empresa>/<execução>/<tentativa>/<nome>``. Ali o executor
já escreve no lugar final, então ``put`` só confere o arquivo (no lugar certo, um
arquivo comum, nunca um atalho) e calcula o tamanho e o SHA-256. Um caminho
informado fora da pasta da tentativa é recusado: o resultado do executor nunca
escolhe que arquivo do disco vira evidência. O ponto de troca é este protocolo: a
parte E liga um store de objetos (S3) com cifra por execução, que envia a cópia
local no ``put`` e a apaga.

Nada daqui devolve caminho a quem chama a API; os nomes já vêm saneados pelo
executor e são conferidos de novo aqui (:func:`rapa.core.artifacts.valid_artifact_name`).

Abaixo da raiz, o store local nunca segue um atalho: cada parte do caminho (a pasta da
empresa, a da execução, a da tentativa e o arquivo) é aberta uma de cada vez, a partir
da anterior, sem seguir atalho (``O_NOFOLLOW``). Assim, uma pasta da tentativa trocada
por um atalho para a pasta de outra execução (ou de outra empresa), mesmo dentro da
raiz, não faz o arquivo de lá virar evidência desta execução, nem ser lido por ela, nem
ser apagado com ela. A própria raiz (``RAPA_PASTA_ARTEFATOS``) pode ser um atalho: é a
configuração do servidor.
"""

import asyncio
import hashlib
import os
import re
import shutil
import stat
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rapa.core.artifacts import recorded_artifact_name, valid_artifact_name

__all__ = [
    "ArtifactInfo",
    "ArtifactMissing",
    "ArtifactReader",
    "ArtifactStore",
    "LocalArtifactStore",
    "attempt_dir",
]

_KIND = re.compile(r"^[a-z][a-z_]{0,39}$")
_CHUNK = 64 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
#: Uma pasta abaixo da raiz: nunca um atalho.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
#: Um arquivo: nunca um atalho, e sem esperar (um FIFO no lugar não trava a leitura).
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _CLOEXEC


class ArtifactInfo(BaseModel):
    """Um artefato registrado: o que ``runs.artifacts`` guarda (nunca o caminho).

    Lê também a forma do RESULT.json do executor (com ``path``, que fica de fora).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: A tentativa que guardou o arquivo (a pasta dele no store).
    attempt: int | None = Field(default=None, ge=1)
    created_at: datetime | None = None


class ArtifactMissing(LookupError):
    """O arquivo do artefato não está no store (apagado, ou nunca guardado)."""


class ArtifactReader(Protocol):
    """Um artefato aberto: o tamanho e os bytes em pedaços."""

    size: int

    def chunks(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class ArtifactStore(Protocol):
    """Onde os artefatos das execuções ficam guardados."""

    async def put(
        self,
        company_id: UUID,
        run_id: UUID,
        attempt: int,
        name: str,
        kind: str,
        source_path: Path,
    ) -> ArtifactInfo:
        """Registra o arquivo que o executor deixou na pasta da tentativa.

        Levanta ``ValueError`` para nome, tipo ou caminho fora da regra e
        ``OSError`` se o arquivo não pode ser lido.
        """
        ...

    async def open(self, company_id: UUID, run_id: UUID, attempt: int, name: str) -> ArtifactReader:
        """Abre um artefato para leitura; :class:`ArtifactMissing` se não existe."""
        ...

    async def delete_run(
        self, company_id: UUID, run_id: UUID, *, keep: Collection[tuple[int, str]] = ()
    ) -> None:
        """Apaga os arquivos da execução (de todas as tentativas); sem nada, não faz nada.

        ``keep`` são os artefatos que ficam, como ``(tentativa, nome)``: a retenção
        apaga primeiro as telas e o vídeo de uma execução que deu certo e deixa os
        arquivos baixados (as evidências) para o prazo longo. Sem ``keep``, tudo sai.
        """
        ...


def attempt_dir(root: Path, company_id: UUID, run_id: UUID, attempt: int) -> Path:
    """A pasta dos artefatos de uma tentativa no disco local."""
    return _run_dir(root, company_id, run_id) / str(_attempt(attempt))


def _run_dir(root: Path, company_id: UUID, run_id: UUID) -> Path:
    return root.joinpath(*_parts(company_id, run_id))


def _parts(company_id: UUID, run_id: UUID, attempt: int | None = None) -> list[str]:
    """As pastas abaixo da raiz: a da empresa, a da execução e, se dada, a da tentativa."""
    if not isinstance(company_id, UUID) or not isinstance(run_id, UUID):
        raise TypeError("a empresa e a execução precisam ser UUIDs")
    parts = [str(company_id), str(run_id)]
    if attempt is not None:
        parts.append(str(_attempt(attempt)))
    return parts


def _attempt(attempt: int) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("a tentativa começa em 1")
    return attempt


def _checked_name(name: str) -> str:
    if not isinstance(name, str) or not valid_artifact_name(name):
        raise ValueError("nome de artefato inválido")
    return name


def _kept_name(name: str) -> str:
    """Um nome de ``keep``: só comparado com o que está na pasta, nunca um caminho.

    Aceita o nome de um artefato de antes da regra do ``..`` (um download de uma
    execução antiga fica no prazo curto como os outros).
    """
    if not isinstance(name, str) or not recorded_artifact_name(name):
        raise ValueError("nome de artefato inválido")
    return name


def _digest(handle: IO[bytes]) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _open_dir(root: Path, parts: Sequence[str]) -> int:
    """Abre a pasta ``root/parts...`` sem seguir atalho nenhum abaixo da raiz.

    Cada parte é aberta a partir da anterior com ``O_NOFOLLOW``: um atalho no caminho
    (mesmo para outro lugar dentro da raiz) levanta ``OSError``.
    """
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
    try:
        for part in parts:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_regular(root: Path, parts: Sequence[str], name: str) -> IO[bytes]:
    """Abre ``root/parts.../name`` só se for um arquivo comum, sem seguir atalho nenhum."""
    directory = _open_dir(root, parts)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("o artefato não é um arquivo comum")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _remove_entry(name: str, directory: int) -> None:
    """Apaga ``name`` dentro da pasta aberta ``directory`` (uma pasta inteira, sem seguir atalho)."""
    if stat.S_ISDIR(os.lstat(name, dir_fd=directory).st_mode):
        shutil.rmtree(name, dir_fd=directory)
    else:
        os.unlink(name, dir_fd=directory)


def _remove_except(directory: int, keep: Collection[str]) -> None:
    """Apaga tudo dentro da pasta aberta ``directory``, menos os nomes de ``keep``."""
    for entry in os.listdir(directory):
        if entry not in keep:
            _remove_entry(entry, directory)


@dataclass
class _LocalReader:
    handle: IO[bytes]
    size: int
    chunk_size: int = _CHUNK

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(self.handle.read, self.chunk_size):
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if not self.handle.closed:
            await asyncio.to_thread(self.handle.close)


class LocalArtifactStore:
    """Artefatos no disco local, em ``<raiz>/<empresa>/<execução>/<tentativa>/<nome>``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def __repr__(self) -> str:
        return f"LocalArtifactStore(root={str(self.root)!r})"

    def _put(
        self,
        company_id: UUID,
        run_id: UUID,
        attempt: int,
        name: str,
        kind: str,
        source_path: Path,
    ) -> ArtifactInfo:
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ValueError("tipo de artefato inválido")
        parts = _parts(company_id, run_id, attempt)
        expected = self.root.resolve().joinpath(*parts, _checked_name(name))
        source = Path(source_path)
        if source.name != name or source.resolve() != expected:
            # O executor escreve no lugar final; outro caminho (ou um atalho no meio dele,
            # mesmo para outra pasta dentro da raiz) não vira evidência.
            raise ValueError("o artefato precisa estar na pasta da tentativa")
        # Sem seguir atalho em nenhuma parte: só um arquivo comum criado ali pelo executor.
        with _open_regular(self.root, parts, name) as handle:
            size, sha256 = _digest(handle)
        return ArtifactInfo(
            kind=kind,
            name=name,
            size=size,
            sha256=sha256,
            attempt=attempt,
            created_at=datetime.now(UTC),
        )

    async def put(
        self,
        company_id: UUID,
        run_id: UUID,
        attempt: int,
        name: str,
        kind: str,
        source_path: Path,
    ) -> ArtifactInfo:
        return await asyncio.to_thread(
            self._put, company_id, run_id, attempt, name, kind, Path(source_path)
        )

    def _open(self, company_id: UUID, run_id: UUID, attempt: int, name: str) -> _LocalReader:
        try:
            handle = _open_regular(
                self.root, _parts(company_id, run_id, attempt), _checked_name(name)
            )
        except OSError, ValueError:
            # Sem o arquivo, um atalho em qualquer parte do caminho (O_NOFOLLOW) ou um
            # nome fora da regra: para quem lê, o arquivo não existe.
            raise ArtifactMissing("o arquivo do artefato não está guardado") from None
        return _LocalReader(handle, os.fstat(handle.fileno()).st_size)

    async def open(self, company_id: UUID, run_id: UUID, attempt: int, name: str) -> ArtifactReader:
        return await asyncio.to_thread(self._open, company_id, run_id, attempt, name)

    def _delete_run(
        self, company_id: UUID, run_id: UUID, keep: Collection[tuple[int, str]]
    ) -> None:
        company, run = _parts(company_id, run_id)
        kept: dict[str, set[str]] = {}
        for attempt, name in keep:
            kept.setdefault(str(_attempt(attempt)), set()).add(_kept_name(name))
        try:
            # A pasta da empresa é aberta sem seguir atalho: um atalho para a pasta de
            # outra empresa levanta OSError, e nada é apagado (a retenção tenta depois).
            folder = _open_dir(self.root, [company])
        except FileNotFoundError:
            return
        try:
            try:
                mode = os.lstat(run, dir_fd=folder).st_mode
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(mode):
                raise ValueError("a pasta da execução é um atalho (ou não é uma pasta)")
            if not kept:
                shutil.rmtree(run, dir_fd=folder)
                return
            run_fd = os.open(run, _DIR_FLAGS, dir_fd=folder)
            try:
                for entry in os.listdir(run_fd):
                    names = kept.get(entry)
                    if names is None or not stat.S_ISDIR(os.lstat(entry, dir_fd=run_fd).st_mode):
                        _remove_entry(entry, run_fd)
                        continue
                    attempt_fd = os.open(entry, _DIR_FLAGS, dir_fd=run_fd)
                    try:
                        _remove_except(attempt_fd, names)
                    finally:
                        os.close(attempt_fd)
            finally:
                os.close(run_fd)
        finally:
            os.close(folder)

    async def delete_run(
        self, company_id: UUID, run_id: UUID, *, keep: Collection[tuple[int, str]] = ()
    ) -> None:
        await asyncio.to_thread(self._delete_run, company_id, run_id, tuple(keep))


if TYPE_CHECKING:
    # Conferência estática: o mypy acusa se o store local deixar de seguir o protocolo.
    def _conforms(root: Path) -> ArtifactStore:
        return LocalArtifactStore(root)

    def _reader_conforms(reader: _LocalReader) -> ArtifactReader:
        return reader
