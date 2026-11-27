"""Store local dos artefatos: registrar no lugar, ler em pedaços, apagar e nomes ruins."""

import asyncio
import contextlib
import hashlib
import os
import shutil
from pathlib import Path
from uuid import UUID

import pytest

from rapa.control.artifacts import ArtifactMissing, LocalArtifactStore, attempt_dir
from rapa.control.launcher import LocalLauncherSettings, LocalProcessLauncher, NoSecrets
from rapa.core.artifacts import artifact_headers, media_type_for, valid_artifact_name
from rapa.core.contracts import RunnerJob
from rapa.core.ids import new_id
from rapa.fixtures.robots import emitir_protocolo

COMPANY, RUN = new_id(), new_id()
DATA = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 300  # mais de um pedaço de 64 KiB


def _write(store: LocalArtifactStore, name: str, data: bytes = DATA, attempt: int = 1) -> Path:
    folder = attempt_dir(store.root, COMPANY, RUN, attempt)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


async def _read(store: LocalArtifactStore, name: str, attempt: int = 1) -> bytes:
    reader = await store.open(COMPANY, RUN, attempt, name)
    return b"".join([chunk async for chunk in reader.chunks()])


async def test_put_registers_in_place_and_open_streams_the_bytes(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    path = _write(store, "tela-001-entrar-falha.png")
    info = await store.put(COMPANY, RUN, 1, path.name, "screenshot", path)
    assert info.model_dump(exclude={"created_at"}) == {
        "kind": "screenshot",
        "name": "tela-001-entrar-falha.png",
        "size": len(DATA),
        "sha256": hashlib.sha256(DATA).hexdigest(),
        "attempt": 1,
    }
    assert info.created_at is not None
    # O que fica registrado nunca leva o caminho do disco.
    assert "path" not in info.model_dump() and str(tmp_path) not in info.model_dump_json()
    reader = await store.open(COMPANY, RUN, 1, path.name)
    assert reader.size == len(DATA)
    chunks = [chunk async for chunk in reader.chunks()]
    assert len(chunks) > 1 and b"".join(chunks) == DATA


async def test_put_refuses_anything_outside_the_attempt_folder(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    good = _write(store, "nota.pdf")
    elsewhere = tmp_path / "segredo.txt"
    elsewhere.write_text("não é evidência")
    other_attempt = _write(store, "nota.pdf", attempt=2)
    other_run = attempt_dir(store.root, COMPANY, new_id(), 1) / "nota.pdf"
    other_run.parent.mkdir(parents=True)
    other_run.write_bytes(DATA)
    for name, source in (
        ("segredo.txt", elsewhere),  # um arquivo qualquer do disco
        ("nota.pdf", other_attempt),  # de outra tentativa
        ("nota.pdf", other_run),  # de outra execução
        ("outro.pdf", good),  # o nome não é o do arquivo
    ):
        with pytest.raises(ValueError):
            await store.put(COMPANY, RUN, 1, name, "download", source)
    # O mesmo arquivo por outro caminho continua aceito: o que vale é onde ele fica.
    same = good.parent / ".." / "1" / "nota.pdf"
    assert (await store.put(COMPANY, RUN, 1, "nota.pdf", "download", same)).name == "nota.pdf"
    with pytest.raises(ValueError):
        await store.put(COMPANY, RUN, 1, "nota.pdf", "Tipo Ruim", good)
    with pytest.raises(ValueError):
        await store.put(COMPANY, RUN, 0, "nota.pdf", "download", good)
    missing = attempt_dir(store.root, COMPANY, RUN, 1) / "sumiu.pdf"
    with pytest.raises(OSError):
        await store.put(COMPANY, RUN, 1, "sumiu.pdf", "download", missing)


async def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    target = tmp_path / "fora.txt"
    target.write_text("arquivo de fora")
    folder = attempt_dir(store.root, COMPANY, RUN, 1)
    folder.mkdir(parents=True)
    (folder / "atalho.png").symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        await store.put(COMPANY, RUN, 1, "atalho.png", "screenshot", folder / "atalho.png")
    with pytest.raises(ArtifactMissing):
        await store.open(COMPANY, RUN, 1, "atalho.png")
    # A pasta da execução trocada por um atalho para fora também não serve.
    other = new_id()
    (store.root / str(COMPANY) / str(other)).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ArtifactMissing):
        await store.open(COMPANY, other, 1, "fora.txt")
    with pytest.raises(ValueError):
        await store.delete_run(COMPANY, other)
    assert target.exists()


@pytest.mark.parametrize("linked", ["attempt", "run", "company"])
async def test_a_symlinked_folder_inside_the_root_never_reaches_another_company(
    tmp_path: Path, linked: str
) -> None:
    """A pasta da tentativa (ou da execução, ou da empresa) trocada por um atalho para a
    pasta de outra empresa, dentro da raiz: o arquivo de lá não vira evidência desta
    execução, não é lido por ela e não é apagado com ela."""
    store = LocalArtifactStore(tmp_path / "artefatos")
    mine, theirs = new_id(), new_id()
    run, their_run = new_id(), new_id()
    secret = attempt_dir(store.root, theirs, their_run, 1) / "segredo.pdf"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(DATA)
    # O atalho pode estar em qualquer parte do caminho abaixo da raiz.
    if linked == "attempt":
        (store.root / str(mine) / str(run)).mkdir(parents=True)
        attempt_dir(store.root, mine, run, 1).symlink_to(secret.parent, target_is_directory=True)
    elif linked == "run":
        (store.root / str(mine)).mkdir(parents=True)
        (store.root / str(mine) / str(run)).symlink_to(
            secret.parent.parent, target_is_directory=True
        )
    else:
        (store.root / str(theirs) / str(run) / "1").mkdir(parents=True)
        shutil.copy(secret, store.root / str(theirs) / str(run) / "1" / "segredo.pdf")
        (store.root / str(mine)).symlink_to(store.root / str(theirs), target_is_directory=True)
    reported = attempt_dir(store.root, mine, run, 1) / "segredo.pdf"
    assert reported.read_bytes() == DATA  # pelo caminho do executor, o arquivo "está lá"
    with pytest.raises((ValueError, OSError)):
        await store.put(mine, run, 1, "segredo.pdf", "download", reported)
    with pytest.raises(ArtifactMissing):
        await store.open(mine, run, 1, "segredo.pdf")
    # Apagar a execução tira no máximo o atalho (ou recusa): nunca o que está do outro lado.
    for keep in ([(1, "segredo.pdf")], []):
        with contextlib.suppress(ValueError, OSError):
            await store.delete_run(mine, run, keep=keep)
    # Nada da outra empresa sumiu, e ela continua lendo o que é dela.
    assert secret.read_bytes() == DATA
    assert await _read_from(store, theirs, their_run, "segredo.pdf") == DATA
    if linked == "company":
        assert await _read_from(store, theirs, run, "segredo.pdf") == DATA


async def _read_from(store: LocalArtifactStore, company: UUID, run: UUID, name: str) -> bytes:
    reader = await store.open(company, run, 1, name)
    return b"".join([chunk async for chunk in reader.chunks()])


async def test_a_fifo_in_place_of_the_file_does_not_hang(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    folder = attempt_dir(store.root, COMPANY, RUN, 1)
    folder.mkdir(parents=True)
    fifo = folder / "cano.pdf"
    os.mkfifo(fifo)
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ValueError):
                await store.put(COMPANY, RUN, 1, "cano.pdf", "download", fifo)
            with pytest.raises(ArtifactMissing):
                await store.open(COMPANY, RUN, 1, "cano.pdf")
    finally:
        # Se o store ficasse esperando o FIFO, este escritor o destrava: o teste falha
        # em vez de travar a suíte.
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))


async def test_delete_run_can_keep_the_downloads(tmp_path: Path) -> None:
    """O prazo curto: as telas, o vídeo e o que sobrou de outra tentativa saem; os arquivos
    baixados que ficam na lista continuam, e depois saem com o resto."""
    store = LocalArtifactStore(tmp_path / "artefatos")
    _write(store, "tela-001.png")
    _write(store, "video-falha.webm")
    _write(store, "nota-001.pdf")
    _write(store, "nota-002.pdf", attempt=2)
    _write(store, "sobra.pdf", attempt=3)
    (attempt_dir(store.root, COMPANY, RUN, 1) / "pasta").mkdir()
    await store.delete_run(COMPANY, RUN, keep=[(1, "nota-001.pdf"), (2, "nota-002.pdf")])
    left = sorted(
        str(path.relative_to(store.root / str(COMPANY) / str(RUN)))
        for path in (store.root / str(COMPANY) / str(RUN)).rglob("*")
        if path.is_file()
    )
    assert left == ["1/nota-001.pdf", "2/nota-002.pdf"]
    assert await _read(store, "nota-001.pdf") == DATA
    with pytest.raises(ValueError):
        await store.delete_run(COMPANY, RUN, keep=[(1, "../fora.pdf")])
    await store.delete_run(COMPANY, RUN)
    assert not (store.root / str(COMPANY) / str(RUN)).exists()
    await store.delete_run(COMPANY, RUN, keep=[(1, "nota-001.pdf")])  # sem nada, não faz nada


@pytest.mark.parametrize(
    "name",
    ["", "..", "../x.png", "a/b.png", "a\\b.png", ".oculto", "x..png", "tela .png", "á.png"],
)
async def test_bad_names_are_refused(tmp_path: Path, name: str) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    assert not valid_artifact_name(name)
    with pytest.raises(ArtifactMissing):
        await store.open(COMPANY, RUN, 1, name)
    with pytest.raises(ValueError):
        await store.put(COMPANY, RUN, 1, name, "download", tmp_path / "qualquer")
    with pytest.raises(ValueError):
        artifact_headers(name, inline=False)


async def test_missing_file_and_delete_run(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artefatos")
    _write(store, "tela.png")
    _write(store, "tela.png", attempt=2)
    neighbour = attempt_dir(store.root, COMPANY, new_id(), 1)
    neighbour.mkdir(parents=True)
    (neighbour / "tela.png").write_bytes(DATA)
    assert await _read(store, "tela.png", attempt=2) == DATA
    with pytest.raises(ArtifactMissing):
        await store.open(COMPANY, RUN, 1, "outra.png")
    await store.delete_run(COMPANY, RUN)
    # Todas as tentativas da execução somem; a vizinha fica.
    assert not (store.root / str(COMPANY) / str(RUN)).exists()
    assert (neighbour / "tela.png").read_bytes() == DATA
    with pytest.raises(ArtifactMissing):
        await store.open(COMPANY, RUN, 1, "tela.png")
    await store.delete_run(COMPANY, RUN)  # sem nada, não faz nada


async def test_list_and_delete_company_follow_the_layout_and_never_a_link(
    tmp_path: Path,
) -> None:
    """A exportação lista e a exclusão apaga pelo store: só a disposição dele, sem atalhos."""
    store = LocalArtifactStore(tmp_path / "artefatos")
    company, other, second = new_id(), new_id(), new_id()
    for who, run, attempt, name in (
        (company, RUN, 1, "tela.png"),
        (company, RUN, 2, "nota.pdf"),
        (company, second, 1, "video.webm"),
        (other, RUN, 1, "tela.png"),
    ):
        folder = attempt_dir(store.root, who, run, attempt)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(DATA)
    base = store.root / str(company)
    # Fora da disposição (nome de pasta, tentativa e arquivo fora da regra) ou um atalho
    # (para a pasta da outra empresa): nada disso é listado.
    (base / "solto.png").write_bytes(DATA)
    (base / "nao-e-uuid" / "1").mkdir(parents=True)
    (base / "nao-e-uuid" / "1" / "x.png").write_bytes(DATA)
    (base / str(RUN) / "01").mkdir()
    (base / str(RUN) / "1" / ".oculto").write_bytes(DATA)
    (base / str(new_id())).symlink_to(store.root / str(other) / str(RUN))
    (base / str(RUN) / "1" / "atalho.png").symlink_to(
        store.root / str(other) / str(RUN) / "1" / "tela.png"
    )
    listed = [item async for item in store.list_company(company)]
    assert [(item.run_id, item.attempt, item.name) for item in listed] == sorted(
        [(RUN, 1, "tela.png"), (RUN, 2, "nota.pdf"), (second, 1, "video.webm")]
    )
    assert [item async for item in store.list_company(new_id())] == []
    # Apagar a empresa: os arquivos dela (sem contar os atalhos) e nada da outra.
    assert await store.delete_company(company) == 6
    assert not base.exists()
    assert (store.root / str(other) / str(RUN) / "1" / "tela.png").read_bytes() == DATA
    assert await store.delete_company(company) == 0
    # Um atalho no lugar da pasta da empresa: some só ele, nunca o destino.
    (store.root / str(company)).symlink_to(store.root / str(other))
    assert [item async for item in store.list_company(company)] == []
    assert await store.delete_company(company) == 0
    assert not (store.root / str(company)).is_symlink()
    assert (store.root / str(other) / str(RUN) / "1" / "tela.png").exists()
    assert await LocalArtifactStore(tmp_path / "nao-existe").delete_company(company) == 0


def test_launcher_writes_where_the_store_reads(tmp_path: Path) -> None:
    """O lançador e o store local usam a mesma pasta da tentativa."""
    launcher = LocalProcessLauncher(
        _NoEvents(),
        LocalLauncherSettings(work_dir=tmp_path / "trabalho", artifacts_dir=tmp_path / "art"),
        NoSecrets(),
    )
    job = RunnerJob(
        run_id=RUN,
        company_id=COMPANY,
        attempt=3,
        manifest=emitir_protocolo("http://portal-v1.rapa.test:8080", "portal-v1.rapa.test"),
        inputs={},
        deadline="2030-01-01T00:00:00Z",
    )
    _work, artifacts = launcher._paths(job)
    assert artifacts == attempt_dir(LocalArtifactStore(tmp_path / "art").root, COMPANY, RUN, 3)


class _NoEvents:
    async def ingest_events(self, *_: object) -> int:  # pragma: no cover - não é chamado
        return 0


@pytest.mark.parametrize(
    ("kind", "name", "expected"),
    [
        ("screenshot", "tela.png", ("image/png", True)),
        ("video", "video-falha.webm", ("video/webm", True)),
        ("download", "nota.pdf", ("application/pdf", False)),
        # Só a tela e o vídeo abrem na página; um download PNG vai como anexo.
        ("download", "imagem.png", ("image/png", False)),
        ("screenshot", "tela.svg", ("application/octet-stream", False)),
        ("download", "pagina.html", ("application/octet-stream", False)),
        ("download", "sem-extensao", ("application/octet-stream", False)),
    ],
)
def test_media_types(kind: str, name: str, expected: tuple[str, bool]) -> None:
    assert media_type_for(kind, name) == expected


def test_headers_never_allow_sniffing_or_caching() -> None:
    headers = artifact_headers("tela.png", inline=True, size=10)
    assert headers == {
        "Content-Disposition": 'inline; filename="tela.png"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Length": "10",
    }
    assert artifact_headers("nota.pdf", inline=False)["Content-Disposition"] == (
        'attachment; filename="nota.pdf"'
    )


async def test_delete_run_keeps_a_legacy_download_name(tmp_path: Path) -> None:
    """Um download de antes da regra do ``..`` (``nota..fiscal.pdf``) também fica no
    prazo curto: o nome de ``keep`` só é comparado com o que está na pasta."""
    store = LocalArtifactStore(tmp_path / "artefatos")
    _write(store, "tela-001.png")
    _write(store, "nota..fiscal.pdf")
    await store.delete_run(COMPANY, RUN, keep=[(1, "nota..fiscal.pdf")])
    folder = attempt_dir(store.root, COMPANY, RUN, 1)
    assert sorted(path.name for path in folder.iterdir()) == ["nota..fiscal.pdf"]
    for bad in ("..", "../nota..fiscal.pdf", ".nota..pdf"):
        with pytest.raises(ValueError):
            await store.delete_run(COMPANY, RUN, keep=[(1, bad)])
    assert sorted(path.name for path in folder.iterdir()) == ["nota..fiscal.pdf"]


async def test_company_listing_and_reading_include_a_legacy_download_name(
    tmp_path: Path,
) -> None:
    """A saída da empresa leva também o download de antes da regra do ``..``.

    ``nota..fiscal.pdf`` continua em ``runs.artifacts`` e na pasta (a retenção o deixa no
    prazo das evidências): a exportação o lista e o lê pelo store, como fazia ao percorrer
    a pasta da empresa. Registrar um nome assim continua recusado.
    """
    store = LocalArtifactStore(tmp_path / "artefatos")
    _write(store, "tela-001.png")
    legacy = _write(store, "nota..fiscal.pdf", b"%PDF legado")
    listed = [(item.run_id, item.attempt, item.name) async for item in store.list_company(COMPANY)]
    assert listed == [(RUN, 1, "nota..fiscal.pdf"), (RUN, 1, "tela-001.png")]
    assert await _read(store, "nota..fiscal.pdf") == b"%PDF legado"
    with pytest.raises(ValueError):
        await store.put(COMPANY, RUN, 1, legacy.name, "download", legacy)
    # Fora da regra de sempre (começa com ponto, tem barra): nem listado, nem lido.
    for bad in ("..", ".nota..pdf", "../nota..fiscal.pdf"):
        with pytest.raises(ArtifactMissing):
            await store.open(COMPANY, RUN, 1, bad)
