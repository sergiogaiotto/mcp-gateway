"""Auxiliares dos testes do store de objetos: o S3 falso (moto), as chaves em memória e a
pasta de passagem onde o executor deixa os arquivos."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import boto3
from moto import mock_aws

from rapa.control.artifacts import S3ArtifactStore, attempt_dir
from rapa.control.keys import CompanyKeys, LocalCompanyKeys, WrappedKey

BUCKET = "rapa-artefatos"
MASTER_KEY = "chave-mestra-de-teste-com-mais-de-32-caracteres"


class MemoryRunKeys:
    """``run_keys`` em memória (o protocolo :class:`rapa.control.keys.RunKeyStore`)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID], WrappedKey] = {}

    async def get(self, company_id: UUID, run_id: UUID) -> WrappedKey | None:
        return self.rows.get((company_id, run_id))

    async def add(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> WrappedKey:
        return self.rows.setdefault((company_id, run_id), key)

    def destroy(self, company_id: UUID, run_id: UUID) -> None:
        del self.rows[(company_id, run_id)]


class Overridden:
    """O cliente do moto com métodos trocados (para simular o S3 fora do ar)."""

    def __init__(self, client: Any, **overrides: Any) -> None:
        self._client = client
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        return self._overrides.get(name) or getattr(self._client, name)


@contextmanager
def fake_s3() -> Iterator[Any]:
    """Um cliente S3 do moto, com o bucket dos artefatos criado."""
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="chave-de-teste",
            aws_secret_access_key="segredo-de-teste",
        )
        client.create_bucket(Bucket=BUCKET)
        yield client


def s3_store(
    client: Any,
    staging: Path,
    *,
    run_keys: Any = None,
    keys: CompanyKeys | None = None,
    chunk_size: int = 1024,
) -> S3ArtifactStore:
    return S3ArtifactStore(
        client=client,
        bucket=BUCKET,
        keys=keys or LocalCompanyKeys(MASTER_KEY),
        run_keys=run_keys if run_keys is not None else MemoryRunKeys(),
        staging_root=staging,
        chunk_size=chunk_size,
    )


def stage(root: Path, company: UUID, run: UUID, attempt: int, name: str, data: bytes) -> Path:
    """O arquivo como o executor o deixa na pasta da tentativa."""
    folder = attempt_dir(root, company, run, attempt)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


def bucket_objects(client: Any) -> dict[str, bytes]:
    """Todos os objetos do bucket, com os bytes guardados."""
    found: dict[str, bytes] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for item in page.get("Contents", []) or []:
            body = client.get_object(Bucket=BUCKET, Key=item["Key"])["Body"]
            found[item["Key"]] = body.read()
    return found
