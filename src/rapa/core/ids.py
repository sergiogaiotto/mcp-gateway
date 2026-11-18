"""Identificadores ordenados no tempo (UUIDv7), gerados pela aplicação."""

import uuid


def new_id() -> uuid.UUID:
    """Devolve um UUIDv7 novo.

    Gerado em Python para não depender da versão do PostgreSQL.
    """
    return uuid.uuid7()
