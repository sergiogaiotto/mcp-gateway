"""Códigos TOTP (RFC 6238) só com a biblioteca padrão.

Compatível com o padrão do pyotp: HMAC-SHA1, 6 dígitos, janelas de 30 segundos
e semente em base32 (sem diferenciar maiúsculas e com preenchimento opcional).
"""

import base64
import hashlib
import hmac
import struct
import time

__all__ = ["TotpError", "decode_secret", "seconds_remaining", "totp_at", "totp_now"]

DIGITS = 6
INTERVAL_S = 30


class TotpError(ValueError):
    """Semente TOTP inválida. A mensagem nunca contém a semente."""


def decode_secret(secret: str) -> bytes:
    """Decodifica a semente base32 (aceita espaços, minúsculas e falta de '=')."""
    cleaned = "".join(secret.split()).upper().rstrip("=")
    if not cleaned:
        raise TotpError("semente TOTP vazia")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except ValueError:  # binascii.Error é subclasse de ValueError
        raise TotpError("semente TOTP não está em base32") from None
    if len(key) < 10:
        raise TotpError("semente TOTP curta demais")
    return key


def totp_at(
    secret: str, for_time: float, *, digits: int = DIGITS, interval: int = INTERVAL_S
) -> str:
    """Código TOTP para o instante ``for_time`` (segundos desde a época Unix)."""
    key = decode_secret(secret)
    counter = int(for_time // interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**digits).zfill(digits)


def totp_now(secret: str, *, digits: int = DIGITS, interval: int = INTERVAL_S) -> str:
    """Código TOTP atual."""
    return totp_at(secret, time.time(), digits=digits, interval=interval)


def seconds_remaining(for_time: float | None = None, *, interval: int = INTERVAL_S) -> float:
    """Quantos segundos faltam para o código atual expirar."""
    now = time.time() if for_time is None else for_time
    return interval - (now % interval)
