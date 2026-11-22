"""Cifra dos artefatos no store de objetos: AES-256-GCM em blocos (construção STREAM).

Cada execução tem uma chave de dados aleatória de 256 bits (:func:`new_data_key`),
guardada só embrulhada pela chave da empresa (:mod:`rapa.control.keys`). Cada arquivo
vira um objeto assim:

- um cabeçalho de 16 bytes: a marca ``RAPA`` com a versão do formato (5 bytes), o
  tamanho do bloco em claro (4 bytes) e um prefixo aleatório de 7 bytes, próprio do
  arquivo;
- os blocos: cada pedaço de até ``chunk_size`` bytes em claro (1 MiB por padrão), cifrado
  com AES-256-GCM (16 bytes de etiqueta a mais). O nonce de cada bloco é o prefixo, o
  contador do bloco (4 bytes) e um byte que diz se é o último; os dados associados
  (AAD) são o cabeçalho, a empresa, a execução, a tentativa, o nome e o índice do bloco.

Assim, na leitura (:func:`decrypt_blocks`), um bloco trocado (outro conteúdo, outro
arquivo, outra execução ou outra empresa), fora de ordem, repetido, cortado no meio ou
no fim (inclusive um corte exato entre dois blocos: o último que sobra não tem a marca
de último) ou com algo a mais depois do último falha a etiqueta: nada dele é entregue,
e a leitura para com :class:`CipherError`. Cada bloco é conferido antes de sair, então
o que sai é sempre o que foi cifrado, na ordem; um corte só é notado no fim.

Nada daqui grava em disco nem em log, e as mensagens de erro nunca trazem os bytes.
"""

import os
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "HEADER_SIZE",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "TAG_SIZE",
    "ArtifactContext",
    "CipherError",
    "EncryptingReader",
    "decrypt_blocks",
    "new_data_key",
    "plaintext_size",
    "read_header",
]

#: Tamanho de um bloco em claro (1 MiB).
DEFAULT_CHUNK_SIZE = 1024 * 1024
#: Limites do tamanho de bloco aceito num cabeçalho (a memória da leitura é de dois blocos).
MIN_CHUNK_SIZE = 1024
MAX_CHUNK_SIZE = 8 * 1024 * 1024
TAG_SIZE = 16
_MAGIC = b"RAPA\x01"
_PREFIX_SIZE = 7
HEADER_SIZE = len(_MAGIC) + 4 + _PREFIX_SIZE
_KEY_SIZE = 32
_MAX_BLOCKS = 2**32
_AAD_LABEL = b"rapa-artefato-v1\x00"


class CipherError(ValueError):
    """O objeto cifrado não confere (bloco trocado, cortado, fora de ordem ou de outro lugar)."""


def new_data_key() -> bytes:
    """Uma chave de dados aleatória de 256 bits (uma por execução)."""
    return os.urandom(_KEY_SIZE)


@dataclass(frozen=True, slots=True)
class ArtifactContext:
    """De onde é o arquivo: entra nos dados associados de cada bloco."""

    company_id: UUID
    run_id: UUID
    attempt: int
    name: str

    def aad(self, header: bytes, index: int) -> bytes:
        name = self.name.encode()
        return b"".join(
            (
                _AAD_LABEL,
                header,
                self.company_id.bytes,
                self.run_id.bytes,
                struct.pack(">IH", self.attempt, len(name)),
                name,
                struct.pack(">I", index),
            )
        )


def _check_key(key: bytes) -> AESGCM:
    if not isinstance(key, bytes) or len(key) != _KEY_SIZE:
        raise CipherError("a chave de dados precisa ter 256 bits")
    return AESGCM(key)


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    if index >= _MAX_BLOCKS:
        raise CipherError("arquivo grande demais para a cifra em blocos")
    return prefix + struct.pack(">I", index) + (b"\x01" if last else b"\x00")


def _header(chunk_size: int, prefix: bytes) -> bytes:
    return _MAGIC + struct.pack(">I", chunk_size) + prefix


def plaintext_size(object_size: int, chunk_size: int) -> int:
    """O tamanho em claro de um objeto de ``object_size`` bytes (cabeçalho incluído)."""
    body = object_size - HEADER_SIZE
    record = chunk_size + TAG_SIZE
    if body < TAG_SIZE:
        raise CipherError("o objeto cifrado está cortado")
    blocks = -(-body // record)
    if body - (blocks - 1) * record < TAG_SIZE:
        raise CipherError("o objeto cifrado está cortado")
    return body - blocks * TAG_SIZE


class EncryptingReader:
    """Lê um arquivo em claro e entrega o objeto cifrado, em pedaços (para o envio ao S3).

    Calcula, de passagem, o tamanho e o SHA-256 do texto claro (``size`` e ``digest``),
    que valem depois de lido até o fim. ``read_plain`` lê o arquivo em claro.
    """

    def __init__(
        self,
        read_plain: Callable[[int], bytes],
        key: bytes,
        context: ArtifactContext,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        on_plain: Callable[[bytes], None] | None = None,
    ) -> None:
        if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError("tamanho de bloco fora dos limites")
        self._aead = _check_key(key)
        self._read_plain = read_plain
        self._context = context
        self._chunk = chunk_size
        self._on_plain = on_plain
        self._prefix = os.urandom(_PREFIX_SIZE)
        self._header = _header(chunk_size, self._prefix)
        self._buffer = bytearray(self._header)
        self._index = 0
        self._ahead: bytes | None = None
        self._done = False
        self.size = 0

    def _read_full(self) -> bytes:
        parts: list[bytes] = []
        wanted = self._chunk
        while wanted:
            data = self._read_plain(wanted)
            if not data:
                break
            parts.append(data)
            wanted -= len(data)
        block = b"".join(parts)
        if block and self._on_plain is not None:
            self._on_plain(block)
        self.size += len(block)
        return block

    def _next_block(self) -> None:
        current = self._ahead if self._ahead is not None else self._read_full()
        # Um bloco de olho no próximo: só assim se sabe se este é o último.
        following = self._read_full() if len(current) == self._chunk else b""
        last = not following
        nonce = _nonce(self._prefix, self._index, last)
        aad = self._context.aad(self._header, self._index)
        self._buffer += self._aead.encrypt(nonce, current, aad)
        self._index += 1
        self._ahead = following
        self._done = last

    def read(self, size: int = -1) -> bytes:
        while not self._done and (size < 0 or len(self._buffer) < size):
            self._next_block()
        if size < 0 or size >= len(self._buffer):
            out = bytes(self._buffer)
            self._buffer.clear()
            return out
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def readable(self) -> bool:
        return True


def _read_exact(read: Callable[[int], bytes], size: int) -> bytes:
    parts: list[bytes] = []
    wanted = size
    while wanted:
        data = read(wanted)
        if not data:
            break
        parts.append(data)
        wanted -= len(data)
    return b"".join(parts)


def read_header(read: Callable[[int], bytes]) -> tuple[bytes, int]:
    """Lê e confere o cabeçalho; devolve ele e o tamanho do bloco em claro."""
    header = _read_exact(read, HEADER_SIZE)
    if len(header) != HEADER_SIZE or not header.startswith(_MAGIC):
        raise CipherError("o objeto não é um artefato cifrado do RaPA")
    (chunk_size,) = struct.unpack(">I", header[len(_MAGIC) : len(_MAGIC) + 4])
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise CipherError("o cabeçalho do artefato cifrado é inválido")
    return header, chunk_size


def decrypt_blocks(
    read: Callable[[int], bytes], key: bytes, context: ArtifactContext
) -> Iterator[bytes]:
    """Decifra o objeto que ``read`` entrega, bloco a bloco, conferindo cada um.

    Levanta :class:`CipherError` no primeiro bloco que não confere (antes de entregá-lo).
    """
    aead = _check_key(key)
    header, chunk_size = read_header(read)
    prefix = header[-_PREFIX_SIZE:]
    record = chunk_size + TAG_SIZE
    index = 0
    current = _read_exact(read, record)
    while True:
        if len(current) < TAG_SIZE:
            raise CipherError("o artefato cifrado está cortado")
        following = _read_exact(read, record) if len(current) == record else b""
        last = not following
        try:
            plain = aead.decrypt(_nonce(prefix, index, last), current, context.aad(header, index))
        except InvalidTag:
            raise CipherError(
                "o artefato cifrado não confere: bloco trocado, cortado, fora de ordem ou de "
                "outra execução"
            ) from None
        yield plain
        if last:
            return
        current = following
        index += 1
