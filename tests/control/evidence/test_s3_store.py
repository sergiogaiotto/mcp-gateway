"""O store de objetos (S3) com cifra por execução, sobre o S3 falso (moto).

Ida e volta, nada em claro no bucket, bloco trocado/cortado/reordenado recusado, dados
associados de outra execução recusados, chave destruída não abre, e as mesmas regras de
caminho do store local na pasta de passagem.
"""

import asyncio
import hashlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from rapa.control.artifacts import (
    ArtifactCorrupted,
    ArtifactMissing,
    ArtifactUnavailable,
    S3ArtifactStore,
    S3Readiness,
    StoredArtifact,
    attempt_dir,
    object_key,
)
from rapa.control.cipher import HEADER_SIZE, TAG_SIZE
from rapa.control.keys import KeyUnavailable, LocalCompanyKeys, WrappedKey
from rapa.core.ids import new_id

from .s3support import (
    BUCKET,
    MASTER_KEY,
    MemoryRunKeys,
    Overridden,
    bucket_objects,
    fake_s3,
    s3_store,
    stage,
)

CHUNK = 1024
RECORD = CHUNK + TAG_SIZE
MARK = b"CONTEUDO-EM-CLARO-"


@pytest.fixture
def s3() -> Iterator[Any]:
    with fake_s3() as client:
        yield client


async def _read(store: S3ArtifactStore, company: UUID, run: UUID, attempt: int, name: str) -> bytes:
    reader = await store.open(company, run, attempt, name)
    return b"".join([chunk async for chunk in reader.chunks()])


def _plain(size: int) -> bytes:
    """Texto claro fácil de procurar no bucket: a marca repetida, com bytes aleatórios."""
    out = bytearray()
    while len(out) < size:
        out += MARK + os.urandom(13)
    return bytes(out[:size])


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, 2 * CHUNK, 5 * CHUNK + 7])
async def test_round_trip_in_blocks_and_nothing_in_clear(
    s3: Any, tmp_path: Path, size: int
) -> None:
    store = s3_store(s3, tmp_path / "passagem")
    company, run = new_id(), new_id()
    data = _plain(size)
    source = stage(store.staging_root, company, run, 1, "nota-001.pdf", data)
    info = await store.put(company, run, 1, "nota-001.pdf", "download", source)
    assert (info.size, info.sha256, info.attempt) == (size, hashlib.sha256(data).hexdigest(), 1)
    reader = await store.open(company, run, 1, "nota-001.pdf")
    assert reader.size == size
    assert b"".join([chunk async for chunk in reader.chunks()]) == data
    # No bucket: só o objeto cifrado, no lugar da empresa, sem nada do texto claro.
    objects = bucket_objects(s3)
    assert list(objects) == [f"empresas/{company}/execucoes/{run}/1/nota-001.pdf.enc"]
    stored = objects[object_key(company, run, 1, "nota-001.pdf")]
    assert MARK not in stored and (size < 16 or data[:16] not in stored)
    assert len(stored) == HEADER_SIZE + size + TAG_SIZE * max(1, -(-size // CHUNK))
    # A cópia de passagem sumiu (e as pastas vazias da tentativa e da execução também).
    assert not source.exists()
    assert not attempt_dir(store.staging_root, company, run, 1).parent.exists()


@pytest.mark.parametrize("broken", ["upload", "key"])
async def test_a_failed_upload_keeps_the_only_copy_for_another_try(
    s3: Any, tmp_path: Path, broken: str
) -> None:
    """O S3 (ou o cofre das chaves) fora do ar: a cópia de passagem é a única, e fica."""
    company, run = new_id(), new_id()
    keys = LocalCompanyKeys(MASTER_KEY)

    class DownKeys:
        async def wrap(self, *args: Any) -> WrappedKey:
            raise KeyUnavailable("o cofre não respondeu")

        async def unwrap(self, *args: Any) -> bytes:  # pragma: no cover
            raise AssertionError

    def upload(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("S3 fora do ar")

    store = s3_store(
        s3,
        tmp_path / "passagem",
        keys=DownKeys() if broken == "key" else keys,
    )
    if broken == "upload":
        store.client = Overridden(s3, upload_fileobj=upload)
    source = stage(store.staging_root, company, run, 1, "tela-001.png", MARK * 10)
    with pytest.raises(ArtifactUnavailable):
        await store.put(company, run, 1, "tela-001.png", "screenshot", source)
    assert source.read_bytes() == MARK * 10 and bucket_objects(s3) == {}
    # De volta: a nova tentativa sobe e só então a cópia em claro sai.
    store.client, store.keys = s3, keys
    info = await store.put(company, run, 1, "tela-001.png", "screenshot", source)
    assert info.size == len(MARK * 10) and not source.exists()
    assert await _read(store, company, run, 1, "tela-001.png") == MARK * 10


async def test_what_is_left_in_the_staging_folder_goes_away(s3: Any, tmp_path: Path) -> None:
    """O que o executor deixou sem informar, o recusado e o que não subiu: sai ao soltar a
    tentativa, com a retenção da execução e com a saída da empresa (só a passagem dela)."""
    store = s3_store(s3, tmp_path / "passagem")
    root = store.staging_root
    company, other, run, run2 = new_id(), new_id(), new_id(), new_id()
    unreported = stage(root, company, run, 1, "baixado-sem-aviso.pdf", MARK)
    refused = stage(root, company, run, 1, "nota.pdf", MARK)
    with pytest.raises(ValueError):
        await store.put(company, run, 1, "nota.pdf", "Tipo Errado", refused)
    (attempt_dir(root, company, run, 1) / "sub").mkdir()
    os.mkfifo(attempt_dir(root, company, run, 1) / "fila")
    stage(root, company, run, 2, "tela-001.png", MARK)  # outra tentativa: fica
    await store.release_attempt(company, run, 1)
    assert not unreported.exists() and not refused.exists()
    assert not attempt_dir(root, company, run, 1).exists()
    assert attempt_dir(root, company, run, 2).exists()
    await store.release_attempt(company, run, 2)
    assert not attempt_dir(root, company, run, 2).parent.exists()  # a da execução, vazia
    await store.release_attempt(company, run, 3)  # nada ali: nada a fazer
    # O despachante caiu antes de registrar: a retenção da execução leva a passagem dela.
    left = stage(root, company, run2, 1, "tela-001.png", MARK)
    kept = stage(root, other, run2, 1, "tela-001.png", MARK)
    await store.delete_run(company, run2, keep=[(1, "tela-001.png")])
    assert not left.exists() and kept.exists()
    # A saída da empresa leva a passagem inteira dela; a da outra fica.
    stage(root, company, run, 1, "tela-001.png", MARK)
    assert await store.staging_companies() == sorted([company, other])
    await store.delete_company(company)
    assert not (root / str(company)).exists() and kept.exists()
    await store.discard_staged_company(other)
    assert await store.staging_companies() == []
    # Um atalho no lugar da pasta da empresa: nunca seguido (só o atalho sai).
    target = tmp_path / "fora"
    target.mkdir()
    (target / "x.pdf").write_bytes(b"fora")
    (root / str(company)).symlink_to(target)
    await store.release_attempt(company, run, 1)
    await store.discard_staged_company(company)
    assert (target / "x.pdf").read_bytes() == b"fora"
    assert not (root / str(company)).is_symlink()


async def test_an_object_store_that_fails_to_answer_is_unavailable(s3: Any, tmp_path: Path) -> None:
    store, company, run, _key, _raw, _ = await _stored(s3, tmp_path)

    def refused(*args: Any, **kwargs: Any) -> Any:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    store.client = Overridden(s3, get_object=refused)
    with pytest.raises(ArtifactUnavailable):
        await store.open(company, run, 1, "video.webm")

    class Cut:
        def read(self, size: int = -1) -> bytes:
            raise ConnectionResetError("caiu")

        def close(self) -> None:
            pass

    store.client = Overridden(s3, get_object=lambda **_: {"Body": Cut(), "ContentLength": 99})
    with pytest.raises(ArtifactUnavailable):
        await store.open(company, run, 1, "video.webm")


async def _stored(s3: Any, tmp_path: Path, size: int = 3 * CHUNK + 10) -> tuple[Any, ...]:
    store = s3_store(s3, tmp_path / "passagem")
    company, run = new_id(), new_id()
    data = _plain(size)
    source = stage(store.staging_root, company, run, 1, "video.webm", data)
    await store.put(company, run, 1, "video.webm", "video", source)
    key = object_key(company, run, 1, "video.webm")
    raw = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return store, company, run, key, raw, data


def _blocks(raw: bytes) -> tuple[bytes, list[bytes]]:
    body = raw[HEADER_SIZE:]
    return raw[:HEADER_SIZE], [body[i : i + RECORD] for i in range(0, len(body), RECORD)]


def _tampered(raw: bytes, how: str) -> bytes:
    header, blocks = _blocks(raw)
    if how == "bloco_trocado":
        changed = bytearray(blocks[1])
        changed[5] ^= 0x01
        blocks[1] = bytes(changed)
    elif how == "cortado_no_meio":
        blocks[-1] = blocks[-1][:-3]
    elif how == "cortado_entre_blocos":
        blocks = blocks[:-1]
    elif how == "fora_de_ordem":
        blocks[0], blocks[1] = blocks[1], blocks[0]
    elif how == "repetido":
        blocks[1] = blocks[0]
    elif how == "algo_a_mais":
        blocks.append(blocks[0])
    elif how == "cabecalho":
        header = header[:-1] + bytes([header[-1] ^ 0x01])
    return header + b"".join(blocks)


@pytest.mark.parametrize(
    "how",
    [
        "bloco_trocado",
        "cortado_no_meio",
        "cortado_entre_blocos",
        "fora_de_ordem",
        "repetido",
        "algo_a_mais",
        "cabecalho",
    ],
)
async def test_tampered_objects_are_refused_block_by_block(
    s3: Any, tmp_path: Path, how: str
) -> None:
    store, company, run, key, raw, data = await _stored(s3, tmp_path)
    s3.put_object(Bucket=BUCKET, Key=key, Body=_tampered(raw, how))
    reader = await store.open(company, run, 1, "video.webm")
    delivered = bytearray()
    with pytest.raises(ArtifactCorrupted):
        async for chunk in reader.chunks():
            delivered += chunk
    # O que saiu antes da recusa é exatamente o começo do original (bloco a bloco).
    assert data.startswith(bytes(delivered)) and len(delivered) < len(data)


async def test_an_object_of_another_run_name_or_attempt_does_not_open(
    s3: Any, tmp_path: Path
) -> None:
    """Os dados associados prendem cada bloco à empresa, à execução, à tentativa e ao nome."""
    store, company, run, _key, raw, _ = await _stored(s3, tmp_path)
    # Na mesma execução (a mesma chave de dados), noutro nome e noutra tentativa.
    for attempt, name in ((1, "outro.webm"), (2, "video.webm")):
        s3.put_object(Bucket=BUCKET, Key=object_key(company, run, attempt, name), Body=raw)
        with pytest.raises(ArtifactCorrupted):
            await _read(store, company, run, attempt, name)
    # Noutra execução, com a chave embrulhada copiada: a chave da empresa prende a chave
    # embrulhada à execução, e ela não abre.
    other = new_id()
    keys: MemoryRunKeys = store.run_keys
    keys.rows[(company, other)] = keys.rows[(company, run)]
    s3.put_object(Bucket=BUCKET, Key=object_key(company, other, 1, "video.webm"), Body=raw)
    with pytest.raises(ArtifactUnavailable):
        await _read(store, company, other, 1, "video.webm")


async def test_the_blocks_are_bound_to_the_run_and_the_company_even_with_the_same_key(
    s3: Any, tmp_path: Path
) -> None:
    """Mesmo com a mesma chave de dados (uma chave da empresa que não prendesse nada), os
    dados associados de cada bloco recusam o objeto de outra execução ou empresa."""
    store = s3_store(s3, tmp_path / "passagem", keys=_NakedKeys())
    company, run = new_id(), new_id()
    data = _plain(2 * CHUNK + 1)
    source = stage(store.staging_root, company, run, 1, "video.webm", data)
    await store.put(company, run, 1, "video.webm", "video", source)
    raw = s3.get_object(Bucket=BUCKET, Key=object_key(company, run, 1, "video.webm"))["Body"]
    raw = raw.read()
    keys: MemoryRunKeys = store.run_keys  # type: ignore[assignment]
    for who, which in ((company, new_id()), (new_id(), run)):
        keys.rows[(who, which)] = keys.rows[(company, run)]
        s3.put_object(Bucket=BUCKET, Key=object_key(who, which, 1, "video.webm"), Body=raw)
        with pytest.raises(ArtifactCorrupted):
            await _read(store, who, which, 1, "video.webm")
    assert await _read(store, company, run, 1, "video.webm") == data


class _NakedKeys:
    """Uma "chave da empresa" que não embrulha nada (só para isolar a cifra dos blocos)."""

    async def wrap(self, company_id: UUID, run_id: UUID, data_key: bytes) -> WrappedKey:
        return WrappedKey(data_key, 1)

    async def unwrap(self, company_id: UUID, run_id: UUID, key: WrappedKey) -> bytes:
        return key.wrapped


async def test_a_destroyed_run_key_does_not_open_what_is_left(s3: Any, tmp_path: Path) -> None:
    store, company, run, key, _raw, data = await _stored(s3, tmp_path)
    assert await _read(store, company, run, 1, "video.webm") == data
    keys: MemoryRunKeys = store.run_keys
    keys.destroy(company, run)
    # O objeto ficou (num backup, por exemplo), mas sem a chave ele não existe mais.
    assert key in bucket_objects(s3)
    with pytest.raises(ArtifactMissing):
        await store.open(company, run, 1, "video.webm")


async def test_the_company_key_wraps_each_run_key_and_other_companies_do_not_unwrap() -> None:
    keys = LocalCompanyKeys(MASTER_KEY)
    company, other, run = new_id(), new_id(), new_id()
    data_key = os.urandom(32)
    wrapped = await keys.wrap(company, run, data_key)
    assert data_key not in wrapped.wrapped
    assert await keys.unwrap(company, run, wrapped) == data_key
    for who, which in ((other, run), (company, new_id())):
        with pytest.raises(KeyUnavailable):
            await keys.unwrap(who, which, wrapped)
    with pytest.raises(KeyUnavailable):
        await LocalCompanyKeys("outra-chave-mestra-com-mais-de-32-caracteres").unwrap(
            company, run, wrapped
        )
    with pytest.raises(ValueError, match="32"):
        LocalCompanyKeys("curta")
    assert MASTER_KEY not in repr(keys)


async def test_each_run_gets_its_own_data_key_and_attempts_share_it(
    s3: Any, tmp_path: Path
) -> None:
    store = s3_store(s3, tmp_path / "passagem")
    company, first, second = new_id(), new_id(), new_id()
    for run, attempt in ((first, 1), (first, 2), (second, 1)):
        source = stage(store.staging_root, company, run, attempt, "tela.png", b"x" * 10)
        await store.put(company, run, attempt, "tela.png", "screenshot", source)
    keys: MemoryRunKeys = store.run_keys  # type: ignore[assignment]
    assert set(keys.rows) == {(company, first), (company, second)}
    assert keys.rows[(company, first)] != keys.rows[(company, second)]
    for run, attempt in ((first, 1), (first, 2), (second, 1)):
        assert await _read(store, company, run, attempt, "tela.png") == b"x" * 10


async def test_delete_run_keeps_what_is_asked_and_the_company_listing(
    s3: Any, tmp_path: Path
) -> None:
    store = s3_store(s3, tmp_path / "passagem")
    company, other, run, run2 = new_id(), new_id(), new_id(), new_id()
    for who, which, attempt, name in (
        (company, run, 1, "tela-001.png"),
        (company, run, 1, "nota-001.pdf"),
        (company, run, 2, "video.webm"),
        (company, run2, 1, "tela-001.png"),
        (other, run, 1, "tela-001.png"),
    ):
        source = stage(store.staging_root, who, which, attempt, name, MARK)
        await store.put(who, which, attempt, name, "download", source)
    # Um objeto fora da disposição, no prefixo da empresa: não é artefato, mas sai com ela.
    s3.put_object(Bucket=BUCKET, Key=f"empresas/{company}/solto.bin", Body=b"?")
    listed = [item async for item in store.list_company(company)]
    assert listed == sorted(
        [
            StoredArtifact(run, 1, "nota-001.pdf"),
            StoredArtifact(run, 1, "tela-001.png"),
            StoredArtifact(run, 2, "video.webm"),
            StoredArtifact(run2, 1, "tela-001.png"),
        ]
    )
    await store.delete_run(company, run, keep=[(1, "nota-001.pdf")])
    assert [item async for item in store.list_company(company)] == sorted(
        [StoredArtifact(run, 1, "nota-001.pdf"), StoredArtifact(run2, 1, "tela-001.png")]
    )
    await store.delete_run(company, run)
    assert [item async for item in store.list_company(company)] == [
        StoredArtifact(run2, 1, "tela-001.png")
    ]
    assert await store.delete_company(company) == 2
    assert list(bucket_objects(s3)) == [object_key(other, run, 1, "tela-001.png")]
    assert await store.delete_company(company) == 0


async def test_delete_run_tolerates_a_legacy_download_name(s3: Any, tmp_path: Path) -> None:
    """A retenção do prazo curto pede para guardar os downloads pelo nome registrado, e um
    de antes da regra do ``..`` (``nota..fiscal.pdf``) não trava a execução: só é
    comparado (como no store local); um nome que nem é de artefato continua recusado."""
    store = s3_store(s3, tmp_path / "passagem")
    company, run = new_id(), new_id()
    for name in ("tela-001.png", "nota-001.pdf"):
        source = stage(store.staging_root, company, run, 1, name, MARK)
        await store.put(company, run, 1, name, "download", source)
    await store.delete_run(company, run, keep=[(1, "nota..fiscal.pdf"), (1, "nota-001.pdf")])
    assert [item async for item in store.list_company(company)] == [
        StoredArtifact(run, 1, "nota-001.pdf")
    ]
    for bad in ("..", "../nota..fiscal.pdf", ".nota..pdf"):
        with pytest.raises(ValueError):
            await store.delete_run(company, run, keep=[(1, bad)])
    assert [item async for item in store.list_company(company)] == [
        StoredArtifact(run, 1, "nota-001.pdf")
    ]


async def test_the_staging_rules_are_those_of_the_local_store(s3: Any, tmp_path: Path) -> None:
    store = s3_store(s3, tmp_path / "passagem")
    company, run = new_id(), new_id()
    outside = tmp_path / "fora.pdf"
    outside.write_bytes(b"de fora")
    with pytest.raises(ValueError):
        await store.put(company, run, 1, "fora.pdf", "download", outside)
    # Um atalho na pasta da tentativa, para um arquivo de fora: nunca seguido.
    folder = attempt_dir(store.staging_root, company, run, 1)
    folder.mkdir(parents=True)
    (folder / "nota.pdf").symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        await store.put(company, run, 1, "nota.pdf", "download", folder / "nota.pdf")
    for name in ("../x.pdf", "a/b.pdf", ".oculto"):
        with pytest.raises(ValueError):
            await store.put(company, run, 1, name, "download", folder / name)
        with pytest.raises(ArtifactMissing):
            await store.open(company, run, 1, name)
    with pytest.raises(ValueError):
        await store.put(company, run, 1, "nota.pdf", "Tipo Errado", folder / "nota.pdf")
    assert bucket_objects(s3) == {}
    assert outside.read_bytes() == b"de fora"


async def test_a_missing_object_is_missing(s3: Any, tmp_path: Path) -> None:
    store, company, run, key, _raw, _ = await _stored(s3, tmp_path)
    s3.delete_object(Bucket=BUCKET, Key=key)
    with pytest.raises(ArtifactMissing):
        await store.open(company, run, 1, "video.webm")


async def test_readiness_checks_the_bucket(s3: Any) -> None:
    assert await S3Readiness(s3, BUCKET).ready() is True
    assert await S3Readiness(s3, "outro-bucket").ready() is False
    assert S3Readiness(s3, BUCKET).name == "s3"


async def test_a_hanging_bucket_holds_one_thread_however_often_readiness_asks() -> None:
    """O prazo da prontidão cancela só quem espera: a consulta do boto segue na thread. Com
    um S3 que aceita e não responde, cada sondagem não deixa mais uma thread presa."""
    release = threading.Event()
    calls: list[str] = []

    class Hanging:
        def head_bucket(self, *, Bucket: str) -> None:
            calls.append(Bucket)
            release.wait(10)

    check = S3Readiness(Hanging(), BUCKET)
    try:
        for _ in range(5):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await check.ready()
        assert calls == [BUCKET]
    finally:
        release.set()
    # Terminada a consulta, a próxima sondagem faz outra.
    assert await check.ready() is True
    before = len(calls)
    assert await check.ready() is True
    assert len(calls) == before + 1
