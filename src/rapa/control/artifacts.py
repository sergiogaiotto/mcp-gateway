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
escolhe que arquivo do disco vira evidência. O ponto de troca é este protocolo: o
store de objetos (:class:`S3ArtifactStore`, abaixo) envia a cópia local cifrada no
``put`` e a apaga.

Nada daqui devolve caminho a quem chama a API; os nomes já vêm saneados pelo
executor e são conferidos de novo aqui (:func:`rapa.core.artifacts.valid_artifact_name`).

A exportação e a exclusão da empresa (:mod:`rapa.control.services.lifecycle`) também
passam pelo store: ``list_company`` percorre os artefatos guardados da empresa (para a
exportação, que os abre com ``open``: decifrados, como a pessoa os vê pela API) e
``delete_company`` apaga todos eles.

:class:`S3ArtifactStore` (``RAPA_ARMAZENAMENTO=s3``, o SeaweedFS) guarda cada arquivo
cifrado em ``empresas/<empresa>/execucoes/<execução>/<tentativa>/<nome>.enc`` no
bucket, com uma chave de dados aleatória por execução (AES-256-GCM em blocos de 1 MiB,
:mod:`rapa.control.cipher`) embrulhada pela chave da empresa e guardada em ``run_keys``
(:mod:`rapa.control.keys`). O texto claro nunca vai para o S3: o executor deixa o
arquivo na pasta da tentativa, que no store de objetos é só uma pasta de passagem
(``RAPA_PASTA_ARTEFATOS``, com as mesmas conferências do store local), ``put`` o envia
cifrado, calculando o tamanho e o SHA-256 do texto claro de passagem, e só depois de
enviado apaga a cópia local. Se a chave ou o envio falham, ``put`` levanta
:class:`ArtifactUnavailable` e a cópia fica, para o despachante tentar de novo; quando
ele termina de registrar a tentativa (``release_attempt``), a pasta de passagem dela
sai inteira (o que o executor deixou sem informar, o que o store recusou e o que não
subiu de jeito nenhum): texto claro não fica esquecido no disco. ``delete_run`` (a
retenção) e ``delete_company`` (a saída da empresa) também apagam a pasta de passagem
da execução e da empresa, e a retenção apaga a de empresa que não existe mais
(``staging_companies`` e ``discard_staged_company``). ``open``
lê em fluxo e decifra bloco a bloco: um bloco trocado, cortado, fora de ordem ou de
outra execução para a leitura com :class:`ArtifactCorrupted`; sem a linha de
``run_keys`` (a chave destruída pela retenção), o artefato não existe mais
(:class:`ArtifactMissing`), mesmo que o objeto tenha sobrado. ``delete_run`` apaga os
objetos; a linha de ``run_keys`` sai pela retenção, depois da marca de expirado
(:mod:`rapa.control.services.retention`). O boto3 roda em threads.

Abaixo da raiz, o store local nunca segue um atalho: cada parte do caminho (a pasta da
empresa, a da execução, a da tentativa e o arquivo) é aberta uma de cada vez, a partir
da anterior, sem seguir atalho (``O_NOFOLLOW``). Assim, uma pasta da tentativa trocada
por um atalho para a pasta de outra execução (ou de outra empresa), mesmo dentro da
raiz, não faz o arquivo de lá virar evidência desta execução, nem ser lido por ela, nem
ser apagado com ela. A própria raiz (``RAPA_PASTA_ARTEFATOS``) pode ser um atalho: é a
configuração do servidor.
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
from collections.abc import AsyncIterator, Callable, Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rapa.control.cipher import (
    DEFAULT_CHUNK_SIZE,
    ArtifactContext,
    CipherError,
    EncryptingReader,
    decrypt_blocks,
    new_data_key,
    plaintext_size,
    read_header,
)
from rapa.core.artifacts import recorded_artifact_name, valid_artifact_name

if TYPE_CHECKING:
    from rapa.control.keys import CompanyKeys, RunKeyStore

__all__ = [
    "ArtifactCorrupted",
    "ArtifactInfo",
    "ArtifactMissing",
    "ArtifactReader",
    "ArtifactStore",
    "ArtifactUnavailable",
    "LocalArtifactStore",
    "S3ArtifactStore",
    "S3Readiness",
    "StoredArtifact",
    "attempt_dir",
    "object_key",
]

logger = logging.getLogger("rapa.control.artifacts")

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


class ArtifactCorrupted(ValueError):
    """O artefato guardado não confere (cifra que falha, objeto cortado ou trocado)."""


class ArtifactUnavailable(RuntimeError):
    """O artefato existe, mas não abre agora: a chave dele não pôde ser desembrulhada (o
    cofre fora do ar, ou a chave da empresa não confere). A mensagem não traz valores."""


@dataclass(frozen=True, slots=True, order=True)
class StoredArtifact:
    """Um artefato guardado no store (o que ``list_company`` encontra)."""

    run_id: UUID
    attempt: int
    name: str


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

    def list_company(self, company_id: UUID) -> AsyncIterator[StoredArtifact]:
        """Os artefatos guardados da empresa, em ordem (execução, tentativa, nome)."""
        ...

    async def delete_company(self, company_id: UUID) -> int:
        """Apaga todos os artefatos da empresa; devolve quantos arquivos (ou objetos) saíram."""
        ...

    async def release_attempt(self, company_id: UUID, run_id: UUID, attempt: int) -> None:
        """Os artefatos da tentativa já foram registrados: o que sobrou dela pode sair.

        No store de objetos, apaga a pasta de passagem da tentativa (texto claro que
        ficou: o que o executor não informou, o que foi recusado, o que não subiu). No
        local, nada: a pasta da tentativa é o lugar final dos artefatos.
        """
        ...

    async def staging_companies(self) -> list[UUID]:
        """As empresas com alguma coisa na pasta de passagem (no local, nenhuma)."""
        ...

    async def discard_staged_company(self, company_id: UUID) -> None:
        """Apaga a pasta de passagem da empresa (no local, nada: não há passagem)."""
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

    def _list_company(self, company_id: UUID) -> list[StoredArtifact]:
        company = _company_part(company_id)
        try:
            # Sem seguir atalho: um atalho no lugar da pasta da empresa não lista nada.
            folder = _open_dir(self.root, [company])
        except OSError:
            return []
        found: list[StoredArtifact] = []
        try:
            for run_name in sorted(os.listdir(folder)):
                run_id = _canonical_uuid(run_name)
                if run_id is None:
                    continue
                for attempt, names in _attempt_files(folder, run_name):
                    found += [StoredArtifact(run_id, attempt, name) for name in names]
        finally:
            os.close(folder)
        return sorted(found)

    async def list_company(self, company_id: UUID) -> AsyncIterator[StoredArtifact]:
        for item in await asyncio.to_thread(self._list_company, company_id):
            yield item

    def _delete_company(self, company_id: UUID) -> int:
        company = _company_part(company_id)
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC)
        except FileNotFoundError:
            return 0  # sem a raiz, nada foi guardado
        try:
            try:
                mode = os.lstat(company, dir_fd=root).st_mode
            except FileNotFoundError:
                return 0
            if not stat.S_ISDIR(mode):
                # Um atalho (ou arquivo) no lugar da pasta: some só ele, nunca o destino.
                os.unlink(company, dir_fd=root)
                return 0
            removed = sum(
                1
                for _, _, files, directory in os.fwalk(company, dir_fd=root, follow_symlinks=False)
                for name in files
                if stat.S_ISREG(os.lstat(name, dir_fd=directory).st_mode)
            )
            shutil.rmtree(company, dir_fd=root)
            return removed
        finally:
            os.close(root)

    async def delete_company(self, company_id: UUID) -> int:
        return await asyncio.to_thread(self._delete_company, company_id)

    async def release_attempt(self, company_id: UUID, run_id: UUID, attempt: int) -> None:
        _parts(company_id, run_id, attempt)  # a pasta da tentativa é o lugar final

    async def staging_companies(self) -> list[UUID]:
        return []

    async def discard_staged_company(self, company_id: UUID) -> None:
        _company_part(company_id)


def _company_part(company_id: UUID) -> str:
    if not isinstance(company_id, UUID):
        raise TypeError("a empresa precisa ser um UUID")
    return str(company_id)


def _canonical_uuid(value: str) -> UUID | None:
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _attempt_number(value: str) -> int | None:
    if not value.isascii() or not value.isdigit() or value != str(int(value)) or int(value) < 1:
        return None
    return int(value)


def _attempt_files(folder: int, run_name: str) -> Iterator[tuple[int, list[str]]]:
    """As pastas das tentativas de uma execução (sem seguir atalho) e os arquivos de cada uma."""
    try:
        if not stat.S_ISDIR(os.lstat(run_name, dir_fd=folder).st_mode):
            return
        run_fd = os.open(run_name, _DIR_FLAGS, dir_fd=folder)
    except OSError:
        return
    try:
        for entry in sorted(os.listdir(run_fd)):
            attempt = _attempt_number(entry)
            if attempt is None:
                continue
            try:
                if not stat.S_ISDIR(os.lstat(entry, dir_fd=run_fd).st_mode):
                    continue
                attempt_fd = os.open(entry, _DIR_FLAGS, dir_fd=run_fd)
            except OSError:
                continue
            try:
                names = [
                    name
                    for name in sorted(os.listdir(attempt_fd))
                    if valid_artifact_name(name)
                    and stat.S_ISREG(os.lstat(name, dir_fd=attempt_fd).st_mode)
                ]
            finally:
                os.close(attempt_fd)
            yield attempt, names
    finally:
        os.close(run_fd)


# ---------------------------------------------------------------------------
# Store de objetos (S3), com cifra por execução
# ---------------------------------------------------------------------------

#: Objetos apagados por pedido ao S3 (o limite da API).
_DELETE_BATCH = 1000
_SUFFIX = ".enc"
_MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


def _company_prefix(company_id: UUID) -> str:
    return f"empresas/{_company_part(company_id)}/"


def _run_prefix(company_id: UUID, run_id: UUID) -> str:
    company, run = _parts(company_id, run_id)
    return f"empresas/{company}/execucoes/{run}/"


def object_key(company_id: UUID, run_id: UUID, attempt: int, name: str) -> str:
    """A chave do objeto de um artefato: ``empresas/<e>/execucoes/<x>/<t>/<nome>.enc``."""
    company, run, number = _parts(company_id, run_id, attempt)
    return f"empresas/{company}/execucoes/{run}/{number}/{_checked_name(name)}{_SUFFIX}"


def _parse_key(company_id: UUID, key: str) -> StoredArtifact | None:
    prefix = f"{_company_prefix(company_id)}execucoes/"
    if not key.startswith(prefix) or not key.endswith(_SUFFIX):
        return None
    parts = key[len(prefix) : -len(_SUFFIX)].split("/")
    if len(parts) != 3:
        return None
    run_id = _canonical_uuid(parts[0])
    attempt = _attempt_number(parts[1])
    if run_id is None or attempt is None or not valid_artifact_name(parts[2]):
        return None
    return StoredArtifact(run_id, attempt, parts[2])


def _client_error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return str(code) if code is not None else None
    return None


class _Prefixed:
    """Lê primeiro ``prefix`` e depois ``read`` (o cabeçalho já lido volta para a frente)."""

    def __init__(self, prefix: bytes, read: Callable[[int], bytes]) -> None:
        self._prefix = prefix
        self._read = read

    def __call__(self, size: int) -> bytes:
        if self._prefix:
            out, self._prefix = self._prefix[:size], self._prefix[size:]
            return out
        return self._read(size)


@dataclass
class _S3Reader:
    body: Any
    size: int
    blocks: Iterator[bytes]

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            while True:
                try:
                    block = await asyncio.to_thread(next, self.blocks, None)
                except CipherError as exc:
                    raise ArtifactCorrupted(str(exc)) from None
                if block is None:
                    break
                if block:
                    yield block
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.body.close)


def _discard_staged(root: Path, parts: Sequence[str], name: str) -> None:
    """Apaga a cópia local de passagem (e as pastas da tentativa e da execução, se vazias)."""
    try:
        directory = _open_dir(root, parts)
    except OSError:
        return
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory)
    finally:
        os.close(directory)
    # A pasta da tentativa e a da execução, de baixo para cima; a da empresa fica.
    for depth in (len(parts), len(parts) - 1):
        try:
            parent = _open_dir(root, parts[: depth - 1])
        except OSError:
            return
        try:
            os.rmdir(parts[depth - 1], dir_fd=parent)
        except OSError:
            return  # ainda tem coisa (outra tentativa, outro arquivo): fica
        finally:
            os.close(parent)


def _discard_entry(root: Path, parts: Sequence[str]) -> None:
    """Apaga ``root/parts...`` inteiro (uma pasta ou um atalho, que nunca é seguido).

    Sem nada ali, não faz nada. Um atalho no meio do caminho não é seguido (fica, com um
    aviso no log), e um erro do disco também só vai para o log: a limpeza da passagem
    nunca impede o que veio antes (o registro, a retenção, a saída da empresa).
    """
    try:
        directory = _open_dir(root, parts[:-1])
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("a pasta de passagem não pôde ser aberta: %s", type(exc).__name__)
        return
    try:
        _remove_entry(parts[-1], directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("a pasta de passagem não pôde ser apagada: %s", type(exc).__name__)
    finally:
        os.close(directory)


def _discard_empty(root: Path, parts: Sequence[str]) -> None:
    """Apaga ``root/parts...`` se for uma pasta vazia (senão, fica)."""
    try:
        directory = _open_dir(root, parts[:-1])
    except OSError:
        return
    try:
        os.rmdir(parts[-1], dir_fd=directory)
    except OSError:
        return
    finally:
        os.close(directory)


class S3ArtifactStore:
    """Artefatos cifrados num bucket S3 (veja o início do módulo).

    ``client`` é um cliente S3 do boto3 (seguro entre threads); ``staging_root`` é a
    pasta de passagem onde o executor deixa os arquivos (``RAPA_PASTA_ARTEFATOS``).
    """

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        keys: CompanyKeys,
        run_keys: RunKeyStore,
        staging_root: Path | str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        data_key: Callable[[], bytes] = new_data_key,
    ) -> None:
        if not bucket:
            raise ValueError("o bucket dos artefatos é obrigatório")
        self.client = client
        self.bucket = bucket
        self.keys = keys
        self.run_keys = run_keys
        self.staging_root = Path(staging_root)
        self.chunk_size = chunk_size
        self._data_key = data_key

    def __repr__(self) -> str:
        return f"S3ArtifactStore(bucket={self.bucket!r})"

    async def _run_key(self, company_id: UUID, run_id: UUID, *, create: bool) -> bytes:
        stored = await self.run_keys.get(company_id, run_id)
        if stored is None:
            if not create:
                raise ArtifactMissing("a chave desta execução não existe mais")
            fresh = self._data_key()
            wrapped = await self.keys.wrap(company_id, run_id, fresh)
            kept = await self.run_keys.add(company_id, run_id, wrapped)
            if kept == wrapped:
                return fresh
            stored = kept  # outra gravação chegou antes: vale a chave dela
        return await self.keys.unwrap(company_id, run_id, stored)

    def _staged_here(self, parts: Sequence[str], name: str, source: Path) -> bool:
        """O caminho informado é o da pasta de passagem da tentativa (como no store local)."""
        expected = self.staging_root.resolve().joinpath(*parts, name)
        return source.name == name and source.resolve() == expected

    def _upload(self, handle: IO[bytes], key: bytes, context: ArtifactContext) -> tuple[int, str]:
        from boto3.s3.transfer import TransferConfig

        digest = hashlib.sha256()
        reader = EncryptingReader(
            handle.read, key, context, chunk_size=self.chunk_size, on_plain=digest.update
        )
        self.client.upload_fileobj(
            reader,
            self.bucket,
            object_key(context.company_id, context.run_id, context.attempt, context.name),
            ExtraArgs={"ContentType": "application/octet-stream"},
            Config=TransferConfig(use_threads=False),
        )
        return reader.size, digest.hexdigest()

    async def put(
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
        name = _checked_name(name)
        if not await asyncio.to_thread(self._staged_here, parts, name, Path(source_path)):
            raise ValueError("o artefato precisa estar na pasta da tentativa")
        handle = await asyncio.to_thread(_open_regular, self.staging_root, parts, name)
        try:
            key = await self._run_key(company_id, run_id, create=True)
            context = ArtifactContext(company_id, run_id, attempt, name)
            size, sha256 = await asyncio.to_thread(self._upload, handle, key, context)
        except Exception as exc:  # o cofre, o banco das chaves ou o S3 fora do ar
            logger.warning("um artefato não subiu para o S3: %s", type(exc).__name__)
            # A cópia de passagem fica: é a única, e o despachante tenta de novo. Ela sai
            # com o resto da tentativa (release_attempt) quando ele desistir.
            raise ArtifactUnavailable("o artefato não pôde ser guardado agora") from None
        finally:
            await asyncio.to_thread(handle.close)
        # Só depois de enviado (cifrado), a cópia em claro sai.
        await asyncio.to_thread(_discard_staged, self.staging_root, parts, name)
        return ArtifactInfo(
            kind=kind,
            name=name,
            size=size,
            sha256=sha256,
            attempt=attempt,
            created_at=datetime.now(UTC),
        )

    async def open(self, company_id: UUID, run_id: UUID, attempt: int, name: str) -> ArtifactReader:
        try:
            key_name = object_key(company_id, run_id, attempt, name)
        except ValueError:
            raise ArtifactMissing("o arquivo do artefato não está guardado") from None
        try:
            data_key = await self._run_key(company_id, run_id, create=False)
        except ArtifactMissing:
            raise
        except Exception as exc:  # o cofre fora do ar, a chave da empresa que não confere
            logger.warning("a chave de uma execução não abriu: %s", type(exc).__name__)
            raise ArtifactUnavailable("a chave do artefato não pôde ser aberta agora") from None
        try:
            response = await asyncio.to_thread(
                self.client.get_object, Bucket=self.bucket, Key=key_name
            )
        except Exception as exc:
            if _client_error_code(exc) in _MISSING_CODES:
                raise ArtifactMissing("o arquivo do artefato não está guardado") from None
            # O S3 fora do ar, recusando (403) ou com erro (5xx): agora não dá.
            logger.warning("o S3 não entregou um artefato: %s", type(exc).__name__)
            raise ArtifactUnavailable("o arquivo do artefato não pôde ser lido agora") from None
        body = response["Body"]
        try:
            header, chunk_size = await asyncio.to_thread(read_header, body.read)
            size = plaintext_size(int(response["ContentLength"]), chunk_size)
        except CipherError as exc:
            await asyncio.to_thread(body.close)
            raise ArtifactCorrupted(str(exc)) from None
        except Exception as exc:  # a leitura do cabeçalho caiu no meio
            await asyncio.to_thread(body.close)
            logger.warning("o S3 não entregou um artefato: %s", type(exc).__name__)
            raise ArtifactUnavailable("o arquivo do artefato não pôde ser lido agora") from None
        blocks = decrypt_blocks(
            _Prefixed(header, body.read),
            data_key,
            ArtifactContext(company_id, run_id, attempt, name),
        )
        return _S3Reader(body, size, blocks)

    def _keys(self, prefix: str) -> list[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        found: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            found += [item["Key"] for item in page.get("Contents", []) or []]
        return found

    def _delete_keys(self, keys: Sequence[str]) -> int:
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            if response.get("Errors"):
                raise RuntimeError("o S3 recusou apagar alguns objetos")
        return len(keys)

    def _delete_run(
        self, company_id: UUID, run_id: UUID, keep: Collection[tuple[int, str]]
    ) -> None:
        kept = set()
        for attempt, name in keep:
            # O nome de keep é só comparado (a regra da retenção, como no store local): um
            # download antigo com ".." no nome nunca foi para o bucket (o put recusa), então
            # não há o que guardar dele aqui, mas também não trava a retenção da execução.
            if valid_artifact_name(_kept_name(name)):
                kept.add(object_key(company_id, run_id, attempt, name))
            else:
                _attempt(attempt)
        self._delete_keys(
            [key for key in self._keys(_run_prefix(company_id, run_id)) if key not in kept]
        )
        # O que tenha sobrado em claro na passagem (o despachante caiu antes de registrar
        # a tentativa) também sai: nunca é o artefato guardado, que está no bucket.
        _discard_entry(self.staging_root, _parts(company_id, run_id))

    async def delete_run(
        self, company_id: UUID, run_id: UUID, *, keep: Collection[tuple[int, str]] = ()
    ) -> None:
        await asyncio.to_thread(self._delete_run, company_id, run_id, tuple(keep))

    async def list_company(self, company_id: UUID) -> AsyncIterator[StoredArtifact]:
        keys = await asyncio.to_thread(self._keys, f"{_company_prefix(company_id)}execucoes/")
        found = sorted(item for key in keys if (item := _parse_key(company_id, key)) is not None)
        for item in found:
            yield item

    async def delete_company(self, company_id: UUID) -> int:
        prefix = _company_prefix(company_id)

        def run() -> int:
            removed = self._delete_keys(self._keys(prefix))
            # A passagem da empresa, se estiver neste disco (o comando da operação roda
            # noutro pod: lá, a retenção do despachante apaga a de empresa que não existe).
            _discard_entry(self.staging_root, [_company_part(company_id)])
            return removed

        return await asyncio.to_thread(run)

    async def release_attempt(self, company_id: UUID, run_id: UUID, attempt: int) -> None:
        parts = _parts(company_id, run_id, attempt)

        def run() -> None:
            _discard_entry(self.staging_root, parts)
            _discard_empty(self.staging_root, parts[:2])  # a da execução, se vazia

        await asyncio.to_thread(run)

    async def staging_companies(self) -> list[UUID]:
        def run() -> list[UUID]:
            try:
                names = os.listdir(self.staging_root)
            except FileNotFoundError:
                return []
            return sorted(found for name in names if (found := _canonical_uuid(name)) is not None)

        return await asyncio.to_thread(run)

    async def discard_staged_company(self, company_id: UUID) -> None:
        part = _company_part(company_id)
        await asyncio.to_thread(_discard_entry, self.staging_root, [part])


@dataclass(frozen=True, slots=True)
class S3Readiness:
    """Pronto só se o bucket dos artefatos existe e responde."""

    client: Any
    bucket: str
    name: str = "s3"

    async def ready(self) -> bool:
        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except Exception as exc:
            logger.warning("o bucket dos artefatos não respondeu: %s", type(exc).__name__)
            return False
        return True


if TYPE_CHECKING:
    # Conferência estática: o mypy acusa se um store deixar de seguir o protocolo.
    def _conforms(root: Path) -> ArtifactStore:
        return LocalArtifactStore(root)

    def _s3_conforms(store: S3ArtifactStore) -> ArtifactStore:
        return store

    def _reader_conforms(reader: _LocalReader) -> ArtifactReader:
        return reader

    def _s3_reader_conforms(reader: _S3Reader) -> ArtifactReader:
        return reader
