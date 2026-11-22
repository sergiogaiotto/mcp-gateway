"""Testes sem navegador: TOTP, segredos, arquivos, artefatos, eventos e resultado."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pyotp
import pytest

from rapa.core.artifacts import valid_artifact_name
from rapa.core.contracts import RunState
from rapa.core.events import RunEvent, RunEventKind
from rapa.core.ids import new_id
from rapa.core.robot import CredentialRef
from rapa.fixtures.portal import DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET, DEFAULT_USER
from rapa.runner import ports, totp
from rapa.runner.ports import (
    DictSecretProvider,
    DirectoryArtifactSink,
    FileUnavailable,
    JsonlEventSink,
    ListEventSink,
    LocalFileProvider,
    SecretUnavailable,
    sanitize_filename,
)
from rapa.runner.result import ArtifactRef, RunResult
from tests.runner.support import SECRETS, file_bytes

PORTAL = CredentialRef(name="portal", kind="password", origin="http://portal.rapa.test:8000")
PORTAL_OTP = CredentialRef(name="portal_otp", kind="totp", origin="http://portal.rapa.test:8000")


# -- TOTP ----------------------------------------------------------------------


@pytest.mark.parametrize("instant", [0, 59, 1_111_111_109, 1_234_567_890, 2_000_000_000, 1.7e9])
def test_totp_matches_pyotp(instant: float) -> None:
    assert totp.totp_at(DEFAULT_TOTP_SECRET, instant) == pyotp.TOTP(DEFAULT_TOTP_SECRET).at(
        int(instant)
    )


def test_totp_rfc6238_vector() -> None:
    # RFC 6238, apêndice B (SHA-1, 8 dígitos), semente "12345678901234567890".
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp.totp_at(seed, 59, digits=8) == "94287082"
    assert totp.totp_at(seed, 1_111_111_109, digits=8) == "07081804"


def test_totp_accepts_lowercase_spaces_and_missing_padding() -> None:
    spaced = " ".join(DEFAULT_TOTP_SECRET.lower()[i : i + 4] for i in range(0, 32, 4))
    assert totp.totp_at(spaced, 1000) == totp.totp_at(DEFAULT_TOTP_SECRET, 1000)


@pytest.mark.parametrize("seed", ["", "!!!!", "AB"])
def test_totp_rejects_bad_seed_without_echoing_it(seed: str) -> None:
    with pytest.raises(totp.TotpError) as info:
        totp.totp_at(seed, 0)
    if seed:
        assert seed not in str(info.value)


# -- segredos --------------------------------------------------------------------


async def test_dict_secret_provider_returns_fields_and_current_totp() -> None:
    provider = DictSecretProvider(SECRETS, totp_min_remaining_s=0)
    assert await provider.get(PORTAL, "username") == DEFAULT_USER
    assert await provider.get(PORTAL, "password") == DEFAULT_PASSWORD
    code = await provider.get(PORTAL_OTP, "totp")
    assert pyotp.TOTP(DEFAULT_TOTP_SECRET).verify(code, valid_window=1)


async def test_totp_waits_for_next_window_when_code_is_about_to_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000_000_019.5]  # faltam 0,5 s para a janela acabar
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(ports.asyncio, "sleep", fake_sleep)
    provider = DictSecretProvider(SECRETS, totp_min_remaining_s=2, clock=lambda: now[0])
    code = await provider.get(PORTAL_OTP, "totp")
    assert slept and slept[0] == pytest.approx(0.55)
    assert code == pyotp.TOTP(DEFAULT_TOTP_SECRET).at(1_000_000_020)


async def test_missing_secret_names_only_the_credential() -> None:
    provider = DictSecretProvider({"portal": {"username": DEFAULT_USER}})
    with pytest.raises(SecretUnavailable, match="password"):
        await provider.get(PORTAL, "password")
    with pytest.raises(SecretUnavailable, match="portal_otp"):
        await provider.get(PORTAL_OTP, "totp")


def test_secret_provider_repr_hides_values() -> None:
    provider = DictSecretProvider(SECRETS)
    text = repr(provider) + str(provider)
    assert "portal" in text
    for value in (DEFAULT_USER, DEFAULT_PASSWORD, DEFAULT_TOTP_SECRET):
        assert value not in text


def test_secret_provider_rejects_unknown_fields_without_values() -> None:
    with pytest.raises(ValueError, match="token") as info:
        DictSecretProvider({"portal": {"token": "valor-secreto"}})
    assert "valor-secreto" not in str(info.value)


def test_secrets_file_is_deleted_after_reading(tmp_path: Path) -> None:
    path = tmp_path / "segredos.json"
    path.write_text(json.dumps(SECRETS))
    provider = DictSecretProvider.from_file(path)
    assert not path.exists()
    assert provider.names() == ["portal", "portal_otp"]
    assert DEFAULT_PASSWORD in provider.sensitive_values()


def test_invalid_secrets_file_is_deleted_too(tmp_path: Path) -> None:
    path = tmp_path / "segredos.json"
    path.write_text("{não é json")
    with pytest.raises(ValueError, match="JSON"):
        DictSecretProvider.from_file(path)
    assert not path.exists()


# -- arquivos de entrada -----------------------------------------------------------


async def test_local_file_provider(tmp_path: Path) -> None:
    (tmp_path / "planilha.csv").write_text("a;b\n")
    provider = LocalFileProvider(tmp_path)
    assert await provider.path_for("planilha.csv") == (tmp_path / "planilha.csv").resolve()
    for bad in ("../etc/passwd", "/etc/passwd", "..", ".oculto", "sub/arquivo", "nao-existe.txt"):
        with pytest.raises(FileUnavailable):
            await provider.path_for(bad)
    with pytest.raises(FileUnavailable):
        await LocalFileProvider(None).path_for("planilha.csv")


# -- artefatos ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("nota-001000.pdf", "nota-001000.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\ana\\relatório março.pdf", "relatorio_marco.pdf"),
        ("  .bashrc  ", "bashrc"),
        ("...", "arquivo"),
        ("a" * 300 + ".pdf", "a" * 116 + ".pdf"),
        # Pontos seguidos viram um só: o plano de controle recusa ".." num nome.
        ("nota..pdf", "nota.pdf"),
        ("Relatório 2026...xlsx", "Relatorio_2026.xlsx"),
        ("a._..b", "a._.b"),
        # O corte do nome comprido não deixa ".." antes da extensão.
        ("a" * 115 + "." + "b" * 20 + ".pdf", "a" * 115 + ".pdf"),
    ],
)
def test_sanitize_filename(name: str, expected: str) -> None:
    assert sanitize_filename(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "nota..pdf",
        "..nota..",
        "Relatório 2026...xlsx",
        "a" * 115 + "." + "b" * 20 + ".pdf",
        "x" * 300,
        "._-._-",
        "C:\\pasta\\..\\arquivo....txt",
        "ação  –  nota fiscal (cópia).PDF",
        "\u0000\u202e.png",
    ],
)
async def test_sanitized_names_are_artifact_names(tmp_path: Path, name: str) -> None:
    """O nome que o executor dá a um artefato é o identificador dele no plano de controle
    e na API: precisa passar pela mesma regra (também com o sufixo de nome repetido)."""
    assert valid_artifact_name(sanitize_filename(name)), sanitize_filename(name)
    sink = DirectoryArtifactSink(tmp_path / "artefatos")
    first = await sink.save_bytes("download", name, b"um")
    second = await sink.save_bytes("download", name, b"dois")
    for ref in (first, second):
        assert valid_artifact_name(ref.name), ref.name


async def test_directory_sink_saves_with_hash_and_unique_names(tmp_path: Path) -> None:
    sink = DirectoryArtifactSink(tmp_path / "artefatos")
    first = await sink.save_bytes("screenshot", "tela.png", b"um")
    second = await sink.save_bytes("screenshot", "tela.png", b"dois")
    assert (first.name, second.name) == ("tela.png", "tela-2.png")
    assert second.size == 4
    assert second.sha256 == hashlib.sha256(b"dois").hexdigest()
    assert file_bytes(first.path) == b"um"

    source = tmp_path / "baixado.bin"
    source.write_bytes(b"x" * 200_000)
    copied = await sink.save_file("download", "../fora.bin", source)
    assert copied.name == "fora.bin"
    assert Path(copied.path).parent == (tmp_path / "artefatos").resolve()
    assert copied.size == 200_000
    assert copied.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()


# -- eventos e resultado --------------------------------------------------------------


def _event(seq: int) -> RunEvent:
    return RunEvent(
        run_id=new_id(), attempt=1, seq=seq, at=datetime.now(UTC), kind=RunEventKind.RUN_STARTED
    )


async def test_event_sinks(tmp_path: Path) -> None:
    memory = ListEventSink()
    path = tmp_path / "eventos.jsonl"
    with JsonlEventSink(path) as jsonl:
        for seq in range(3):
            await memory.emit(_event(seq))
            await jsonl.emit(_event(seq))
    assert [e.seq for e in memory.events] == [0, 1, 2]
    lines = path.read_text().splitlines()
    assert [RunEvent.model_validate_json(line).seq for line in lines] == [0, 1, 2]


def test_run_result_json_shape_matches_interface() -> None:
    result = RunResult(
        run_id=new_id(),
        attempt=1,
        state=RunState.SUCCEEDED,
        outputs={"protocolo": "PRT-1"},
        artifacts=[
            ArtifactRef(kind="download", name="a.pdf", path="/tmp/a.pdf", size=1, sha256="0" * 64)
        ],
    )
    data = json.loads(result.model_dump_json())
    assert set(data) == {
        "run_id",
        "attempt",
        "state",
        "outputs",
        "error_code",
        "error_message",
        "failed_step_id",
        "irreversible_started",
        "steps_done",
        "artifacts",
        # O retrato da página no passo que falhou (o conserto); só numa falha do alvo.
        "failure_snapshot",
    }
    assert data["failure_snapshot"] is None
    assert data["state"] == "succeeded"
    assert set(data["artifacts"][0]) == {"kind", "name", "path", "size", "sha256"}
    assert RunResult.model_validate_json(result.model_dump_json()) == result


# -- máscara de segredos -------------------------------------------------------------


def test_redactor_masks_values_but_keeps_hashes() -> None:
    from rapa.runner.interpreter import MASK, _Redactor

    redactor = _Redactor([DEFAULT_PASSWORD, "abc"])  # "abc" é curto demais para mascarar
    data = {
        "mensagem": f"falhou com {DEFAULT_PASSWORD}",
        "lista": [DEFAULT_PASSWORD, 3, None],
        "sha256": "ab" * 32,
        "outro": "abc",
    }
    assert redactor.value(data) == {
        "mensagem": f"falhou com {MASK}",
        "lista": [MASK, 3, None],
        "sha256": "ab" * 32,
        "outro": "abc",
    }


def test_redactor_masks_keys_without_collisions_and_no_key_is_exempt() -> None:
    from rapa.runner.interpreter import MASK, _Redactor

    redactor = _Redactor([DEFAULT_PASSWORD, DEFAULT_USER])
    # Chaves vindas da página (cabeçalhos de tabela), em qualquer profundidade.
    data = [
        {
            f"Total {DEFAULT_PASSWORD}": "1",
            f"Total {DEFAULT_USER}": "2",
            f"Total {MASK}_2": "3",
            "sha256": f"x{DEFAULT_PASSWORD}",
            "fingerprint": {DEFAULT_USER: DEFAULT_USER},
            7: DEFAULT_USER,
        }
    ]
    assert redactor.value(data) == [
        {
            f"Total {MASK}": "1",
            f"Total {MASK}_2": "2",
            f"Total {MASK}_2_2": "3",
            "sha256": f"x{MASK}",
            "fingerprint": {MASK: MASK},
            7: MASK,
        }
    ]


# -- confirmação antes do passo irreversível -------------------------------------


def _ack_event(seq: int, kind: RunEventKind, **data: object) -> RunEvent:
    return RunEvent(
        run_id=new_id(),
        attempt=1,
        seq=seq,
        at=datetime.now(UTC),
        kind=kind,
        step_id="emitir",
        data=dict(data),
    )


async def test_acking_sink_passes_ordinary_events_without_waiting(tmp_path: Path) -> None:
    inner = ListEventSink()
    sink = ports.AckingEventSink(inner, tmp_path, timeout_s=0.05)
    await sink.emit(_ack_event(0, RunEventKind.STEP_STARTED, efeito="write"))
    await sink.emit(_ack_event(1, RunEventKind.STEP_SUCCEEDED))
    assert [e.seq for e in inner.events] == [0, 1]


async def test_acking_sink_waits_for_the_ack_file(tmp_path: Path) -> None:
    import asyncio

    inner = ListEventSink()
    sink = ports.AckingEventSink(inner, tmp_path, timeout_s=5, poll_s=0.01)
    event = _ack_event(3, RunEventKind.STEP_STARTED, efeito="irreversible", irreversivel=True)
    task = asyncio.create_task(sink.emit(event))
    await asyncio.sleep(0.05)
    assert not task.done()
    assert inner.events == [event]  # o aviso sai antes da espera
    ports.ack_path(tmp_path, 3).write_text("ok")
    await asyncio.wait_for(task, 2)


async def test_acking_sink_fails_the_step_without_ack(tmp_path: Path) -> None:
    from rapa.runner.errors import ErrorCode, StepError

    sink = ports.AckingEventSink(ListEventSink(), tmp_path, timeout_s=0.05, poll_s=0.01)
    event = _ack_event(0, RunEventKind.STEP_STARTED, efeito="irreversible", irreversivel=True)
    with pytest.raises(StepError) as caught:
        await sink.emit(event)
    assert caught.value.code is ErrorCode.SEM_CONFIRMACAO


def test_acking_sink_rejects_bad_timing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ports.AckingEventSink(ListEventSink(), tmp_path, timeout_s=0)


# -- confirmação antes de qualquer passo que grava ---------------------------------


@pytest.mark.parametrize(
    ("kind", "data", "waits"),
    [
        # Início de um passo que grava (``grava``) e aviso de passo irreversível.
        (RunEventKind.STEP_STARTED, {"efeito": "write", "grava": True}, True),
        (RunEventKind.STEP_STARTED, {"efeito": "irreversible", "irreversivel": True}, True),
        # Passo de leitura, e o passo irreversível que falhou antes do aviso.
        (RunEventKind.STEP_STARTED, {"efeito": "read"}, False),
        (RunEventKind.STEP_STARTED, {"efeito": "irreversible"}, False),
        # Só o valor booleano vale; o texto "true" (que viria da página) não.
        (RunEventKind.STEP_STARTED, {"efeito": "write", "grava": "true"}, False),
        # Outros tipos de evento nunca esperam, mesmo com as mesmas chaves.
        (RunEventKind.STEP_SUCCEEDED, {"grava": True}, False),
        (RunEventKind.RUN_STOPPED_BEFORE_WRITE, {"efeito": "write", "grava": True}, False),
    ],
)
def test_runner_waits_for_the_ack_before_every_step_that_writes(
    kind: RunEventKind, data: dict[str, object], waits: bool
) -> None:
    event = _ack_event(0, kind, **data)
    assert ports.waits_for_ack(event) is waits
    # ``needs_ack`` continua dizendo só se é o aviso de passo irreversível.
    assert ports.needs_ack(event) is (waits and data.get("irreversivel") is True)


async def test_acking_sink_waits_for_the_ack_before_a_step_that_writes(tmp_path: Path) -> None:
    import asyncio

    inner = ListEventSink()
    sink = ports.AckingEventSink(inner, tmp_path, timeout_s=5, poll_s=0.01)
    event = _ack_event(4, RunEventKind.STEP_STARTED, efeito="write", grava=True)
    task = asyncio.create_task(sink.emit(event))
    await asyncio.sleep(0.05)
    assert not task.done()  # o passo que grava não começa sem a confirmação
    assert inner.events == [event]  # o início sai antes da espera
    ports.ack_path(tmp_path, 4).write_text("ok")
    await asyncio.wait_for(task, 2)


async def test_acking_sink_fails_a_step_that_writes_without_ack(tmp_path: Path) -> None:
    from rapa.runner.errors import ErrorCode, StepError

    sink = ports.AckingEventSink(ListEventSink(), tmp_path, timeout_s=0.05, poll_s=0.01)
    event = _ack_event(0, RunEventKind.STEP_STARTED, efeito="write", grava=True)
    with pytest.raises(StepError) as caught:
        await sink.emit(event)
    assert caught.value.code is ErrorCode.SEM_CONFIRMACAO
    assert caught.value.step_id == "emitir"  # o passo que não começou
    assert "passo que grava" in caught.value.message
    assert "não foi executado" in caught.value.message
