"""Portas do executor: segredos, arquivos de entrada, artefatos e eventos.

O interpretador só conversa com o mundo por estes protocolos. As
implementações simples daqui servem ao worker local e aos testes; em produção,
o plano de controle troca por cofre, armazenamento de objetos e fila de eventos.

Nada aqui depende do Playwright.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Protocol, Self

from rapa.core.events import RunEvent
from rapa.core.robot import CredentialRef
from rapa.runner import totp
from rapa.runner.result import ArtifactRef

__all__ = [
    "SECRET_FIELDS",
    "ArtifactSink",
    "DictSecretProvider",
    "DirectoryArtifactSink",
    "EventSink",
    "FileProvider",
    "FileUnavailable",
    "JsonlEventSink",
    "ListEventSink",
    "LocalFileProvider",
    "SecretProvider",
    "SecretUnavailable",
    "sanitize_filename",
]

#: Campos aceitos para cada credencial no SECRETS.json.
SECRET_FIELDS = frozenset({"username", "password", "totp_secret", "pfx_path", "pfx_password"})
#: Campos cujo valor é sigiloso (mascarados em eventos, resultados e mensagens).
_SENSITIVE_FIELDS = ("username", "password", "totp_secret", "pfx_password")


class SecretUnavailable(LookupError):
    """A credencial ou o campo pedido não foi entregue ao executor.

    A mensagem cita só o nome da credencial e do campo, nunca valores.
    """


class FileUnavailable(LookupError):
    """O arquivo de entrada não existe ou o identificador é inválido."""


# ---------------------------------------------------------------------------
# Protocolos
# ---------------------------------------------------------------------------


class SecretProvider(Protocol):
    """Entrega o valor de um campo de credencial na hora de digitar.

    Para o campo ``totp``, devolve o código atual calculado da semente guardada.
    """

    async def get(self, credential: CredentialRef, field: str) -> str: ...


class FileProvider(Protocol):
    """Traduz o identificador de um arquivo de entrada num caminho local."""

    async def path_for(self, file_id: str) -> Path: ...


class ArtifactSink(Protocol):
    """Guarda os arquivos produzidos pela execução (telas e downloads)."""

    async def save_bytes(self, kind: str, name: str, data: bytes) -> ArtifactRef: ...

    async def save_file(self, kind: str, name: str, path: Path) -> ArtifactRef: ...


class EventSink(Protocol):
    """Recebe os eventos da execução, na ordem do número de sequência."""

    async def emit(self, event: RunEvent) -> None: ...


# ---------------------------------------------------------------------------
# Segredos
# ---------------------------------------------------------------------------


class DictSecretProvider:
    """Segredos em memória, no formato do SECRETS.json.

    ``{"nome_da_credencial": {"username": ..., "password": ..., "totp_secret": ...,
    "pfx_path": ..., "pfx_password": ...}}``, todos os campos opcionais.

    Se o código TOTP atual estiver a menos de ``totp_min_remaining_s`` segundos
    de expirar, espera a janela seguinte, para o portal não recusar um código
    vencido durante o envio.
    """

    def __init__(
        self,
        secrets: Mapping[str, Mapping[str, str]],
        *,
        totp_min_remaining_s: float = 2.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        checked: dict[str, dict[str, str]] = {}
        for name, fields in secrets.items():
            if not isinstance(name, str) or not isinstance(fields, Mapping):
                raise ValueError("cada credencial precisa ser um objeto com os campos do segredo")
            unknown = sorted(str(key) for key in fields if key not in SECRET_FIELDS)
            if unknown:
                raise ValueError(
                    f"campos desconhecidos na credencial '{name}': {', '.join(unknown)}"
                )
            for key, value in fields.items():
                if not isinstance(value, str):
                    raise ValueError(f"o campo '{key}' da credencial '{name}' precisa ser texto")
            checked[name] = dict(fields)
        self._secrets = checked
        self._totp_min_remaining_s = totp_min_remaining_s
        self._clock = clock

    @classmethod
    def from_json(cls, data: str | bytes, *, totp_min_remaining_s: float = 2.0) -> Self:
        try:
            parsed = json.loads(data)
        except ValueError:
            raise ValueError("o arquivo de segredos não é um JSON válido") from None
        if not isinstance(parsed, dict):
            raise ValueError("o arquivo de segredos precisa ser um objeto JSON")
        return cls(parsed, totp_min_remaining_s=totp_min_remaining_s)

    @classmethod
    def from_file(
        cls, path: Path | str, *, delete: bool = True, totp_min_remaining_s: float = 2.0
    ) -> Self:
        """Lê o SECRETS.json e, por padrão, apaga o arquivo logo depois de ler."""
        file = Path(path)
        try:
            data = file.read_bytes()
        finally:
            if delete:
                file.unlink(missing_ok=True)
        return cls.from_json(data, totp_min_remaining_s=totp_min_remaining_s)

    def names(self) -> list[str]:
        return sorted(self._secrets)

    def sensitive_values(self) -> list[str]:
        """Valores a mascarar em qualquer texto que saia do executor."""
        return [
            fields[key]
            for fields in self._secrets.values()
            for key in _SENSITIVE_FIELDS
            if fields.get(key)
        ]

    async def get(self, credential: CredentialRef, field: str) -> str:
        fields = self._secrets.get(credential.name)
        if fields is None:
            raise SecretUnavailable(
                f"a credencial '{credential.name}' não foi entregue ao executor"
            )
        if field == "totp":
            seed = fields.get("totp_secret")
            if not seed:
                raise SecretUnavailable(
                    f"a credencial '{credential.name}' não tem semente TOTP (totp_secret)"
                )
            remaining = totp.seconds_remaining(self._clock())
            if remaining < self._totp_min_remaining_s:
                await asyncio.sleep(remaining + 0.05)
            try:
                return totp.totp_at(seed, self._clock())
            except totp.TotpError as exc:
                raise SecretUnavailable(
                    f"a semente TOTP da credencial '{credential.name}' é inválida: {exc}"
                ) from None
        value = fields.get(field)
        if value is None or field not in SECRET_FIELDS:
            raise SecretUnavailable(f"a credencial '{credential.name}' não tem o campo '{field}'")
        return value

    def __repr__(self) -> str:
        return f"DictSecretProvider(credenciais={self.names()!r})"


# ---------------------------------------------------------------------------
# Arquivos de entrada
# ---------------------------------------------------------------------------

_FILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class LocalFileProvider:
    """Arquivos de entrada num diretório local: o identificador é o nome do arquivo."""

    def __init__(self, directory: Path | str | None) -> None:
        self.directory = Path(directory) if directory is not None else None

    def _resolve(self, file_id: str) -> Path:
        if self.directory is None:
            raise FileUnavailable("nenhum diretório de arquivos de entrada foi configurado")
        if not _FILE_ID.fullmatch(file_id) or ".." in file_id:
            raise FileUnavailable("identificador de arquivo inválido")
        base = self.directory.resolve()
        path = (base / file_id).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise FileUnavailable(f"o arquivo '{file_id}' não foi encontrado")
        return path

    async def path_for(self, file_id: str) -> Path:
        return await asyncio.to_thread(self._resolve, file_id)


# ---------------------------------------------------------------------------
# Artefatos
# ---------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME = 120


def sanitize_filename(name: str, *, default: str = "arquivo") -> str:
    """Nome de arquivo seguro: sem diretórios, sem acentos e só com ``[A-Za-z0-9._-]``."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    last = ascii_only.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE.sub("_", last).strip("._-")
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if len(cleaned) > _MAX_NAME:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            cleaned = f"{stem[: _MAX_NAME - len(ext) - 1]}.{ext}"
        else:
            cleaned = cleaned[:_MAX_NAME]
    return cleaned or default


class DirectoryArtifactSink:
    """Guarda artefatos num diretório local, com nomes saneados e sem sobrescrever."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _reserve(self, name: str) -> tuple[Path, IO[bytes]]:
        safe = sanitize_filename(name)
        stem, dot, ext = safe.rpartition(".")
        if not dot or not stem:
            stem, ext = safe, ""
        suffix = f".{ext}" if ext else ""
        for n in range(1, 10_000):
            candidate = safe if n == 1 else f"{stem}-{n}{suffix}"
            path = self.directory / candidate
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            return path, os.fdopen(fd, "wb")
        raise FileExistsError(f"nomes esgotados para o artefato '{safe}'")

    def _write_bytes(self, kind: str, name: str, data: bytes) -> ArtifactRef:
        path, handle = self._reserve(name)
        with handle:
            handle.write(data)
        return ArtifactRef(
            kind=kind,
            name=path.name,
            path=str(path.resolve()),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def _copy_file(self, kind: str, name: str, source: Path) -> ArtifactRef:
        digest = hashlib.sha256()
        size = 0
        path, handle = self._reserve(name)
        with handle, source.open("rb") as src:
            while chunk := src.read(1 << 16):
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
        return ArtifactRef(
            kind=kind,
            name=path.name,
            path=str(path.resolve()),
            size=size,
            sha256=digest.hexdigest(),
        )

    async def save_bytes(self, kind: str, name: str, data: bytes) -> ArtifactRef:
        return await asyncio.to_thread(self._write_bytes, kind, name, data)

    async def save_file(self, kind: str, name: str, path: Path) -> ArtifactRef:
        return await asyncio.to_thread(self._copy_file, kind, name, Path(path))


# ---------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------


class ListEventSink:
    """Guarda os eventos numa lista (testes e execuções em memória)."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class JsonlEventSink:
    """Acrescenta um evento JSON por linha, gravado no disco antes de seguir.

    O ``fsync`` a cada linha garante que o aviso de passo irreversível chegue
    ao disco antes de o executor clicar.
    """

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self._file: IO[str] | None = self.path.open("a", encoding="utf-8")
        self._lock = asyncio.Lock()

    def _write(self, line: str) -> None:
        if self._file is None:
            raise RuntimeError("o arquivo de eventos já foi fechado")
        self._file.write(line)
        self._file.flush()
        if self._fsync:
            os.fsync(self._file.fileno())

    async def emit(self, event: RunEvent) -> None:
        line = event.model_dump_json() + "\n"
        async with self._lock:
            await asyncio.to_thread(self._write, line)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
