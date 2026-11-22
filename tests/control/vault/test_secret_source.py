"""Os segredos de uma execução, do OpenBao: só o que está registrado na área do robô, só
debaixo da empresa da execução, só os campos permitidos, e nada de valor nas mensagens."""

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.launcher import SecretsUnavailable
from rapa.control.services import PgPlatform, PgProvisioning
from rapa.control.vault import (
    BaoSettings,
    OpenBaoClient,
    OpenBaoSecretSource,
    company_secret_path,
    required_fields,
)
from rapa.core.contracts import RunnerJob, RunTrigger
from rapa.core.robot import CredentialKind, CredentialRef, Manifest
from tests.control.credentials.support import a1_protocolo
from tests.control.db.support import BASE, PROTOCOLO_INPUTS, Tenant, make_tenant, protocolo, publish

from .fakebao import ROLE, FakeBao
from .support import ADDRESS, bao_client, sa_token_file

pytestmark = pytest.mark.database

PASSWORD = "senha-de-teste-que-nao-pode-vazar"
SEED = "JBSWY3DPEHPK3PXP"
PFX = b"\x30\x82PFX de teste"


async def _run(app_db: Database, tenant: Tenant, manifest: Manifest | None = None) -> RunnerJob:
    """Uma execução de verdade (a área vem do banco) e o trabalho que o lançador recebe."""
    platform = PgPlatform(app_db)
    await publish(platform, tenant, protocolo())
    run = await platform.start(
        tenant.operator, "emitir_protocolo", PROTOCOLO_INPUTS, RunTrigger.REST
    )
    return RunnerJob(
        run_id=run.run_id,
        company_id=tenant.company_id,
        attempt=1,
        manifest=manifest or protocolo(),
        inputs=PROTOCOLO_INPUTS,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


async def _register(
    app_db: Database,
    tenant: Tenant,
    name: str,
    kind: CredentialKind,
    vault_path: str,
    *,
    area_id: UUID | None = None,
    origin: str = BASE,
    **extra: Any,
) -> UUID:
    return await PgProvisioning(app_db).register_credential(
        tenant.company_id,
        area_id or tenant.area_id,
        CredentialRef(name=name, kind=kind, origin=origin),
        vault_path,
        **extra,
    )


async def _portal_registered(app_db: Database, tenant: Tenant) -> None:
    await _register(app_db, tenant, "portal", CredentialKind.PASSWORD, "portal/acesso")
    await _register(app_db, tenant, "portal_otp", CredentialKind.TOTP, "portal/otp")


def _put_portal(bao: FakeBao, company: UUID, **fields: Any) -> None:
    bao.put_secret(
        f"empresas/{company}/portal/acesso",
        {"username": "ana", "password": PASSWORD, **fields},
    )
    bao.put_secret(f"empresas/{company}/portal/otp", {"totp_secret": SEED})


async def _secrets(
    dispatcher_db: Database, bao: FakeBao, tmp_path: Path, job: RunnerJob
) -> dict[str, dict[str, str]]:
    client = bao_client(bao, tmp_path)
    try:
        found = await OpenBaoSecretSource(dispatcher_db, client).secrets_for(job)
    finally:
        await client.aclose()
    return {name: dict(fields) for name, fields in found.items()}


async def _unavailable(
    dispatcher_db: Database, bao: FakeBao, tmp_path: Path, job: RunnerJob
) -> str:
    with pytest.raises(SecretsUnavailable) as caught:
        await _secrets(dispatcher_db, bao, tmp_path, job)
    message = caught.value.message
    for value in (PASSWORD, SEED, base64.b64encode(PFX).decode()):
        assert value not in message
    return message


async def test_only_the_allowed_fields_of_the_registered_secrets(
    app_db: Database, dispatcher_db: Database, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Cofre feliz")
    job = await _run(app_db, tenant)
    await _portal_registered(app_db, tenant)
    bao = FakeBao()
    # Um campo a mais (e até um pfx_path, que é do lançador) fica no cofre.
    _put_portal(bao, tenant.company_id, observacao="não sai", pfx_path="/etc/shadow")
    found = await _secrets(dispatcher_db, bao, tmp_path, job)
    assert found == {
        "portal": {"username": "ana", "password": PASSWORD},
        "portal_otp": {"totp_secret": SEED},
    }
    # Só leituras debaixo da empresa da execução.
    reads = [path for method, path in bao.requests if path.startswith("rapa/")]
    assert reads and all(
        path.startswith(f"rapa/data/empresas/{tenant.company_id}/") for path in reads
    )


async def test_the_company_prefix_is_always_put_by_the_code(
    app_db: Database, dispatcher_db: Database, tmp_path: Path
) -> None:
    """O caminho registrado nunca alcança outra empresa: com ``..``, barra no começo, ``.``
    ou barras seguidas, nem vai ao cofre; sem o segredo debaixo da própria empresa, falta."""
    tenant = await make_tenant(app_db, "Cofre curioso")
    other = await make_tenant(app_db, "Cofre vizinho")
    job = await _run(app_db, tenant)
    bao = FakeBao()
    _put_portal(bao, other.company_id)  # o segredo da outra empresa, no mesmo caminho
    await _portal_registered(app_db, tenant)
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert "o segredo da credencial 'portal' não está no cofre" in message
    for path in (f"../{other.company_id}/portal/acesso", "/portal/acesso", "portal/./acesso"):
        other_tenant = await make_tenant(app_db, f"Cofre {len(path)}")
        other_job = await _run(app_db, other_tenant)
        await _register(app_db, other_tenant, "portal", CredentialKind.PASSWORD, path)
        await _register(app_db, other_tenant, "portal_otp", CredentialKind.TOTP, "portal/otp")
        bao.requests.clear()
        message = await _unavailable(dispatcher_db, bao, tmp_path, other_job)
        assert "o caminho no cofre da credencial 'portal' é inválido" in message
        assert not any(".." in p or "//" in p or "/./" in p for _, p in bao.requests)
    for bad in ("a//b", "a/", "", ".", "a/../b", "a" * 501, "a b"):
        with pytest.raises(ValueError):
            company_secret_path(tenant.company_id, bad)
    assert company_secret_path(tenant.company_id, "x:y/z.1") == (
        f"empresas/{tenant.company_id}/x%3Ay/z.1"
    )


async def test_registration_rules_before_reading_the_vault(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Cofre com regras")
    job = await _run(app_db, tenant)
    bao = FakeBao()
    _put_portal(bao, tenant.company_id)
    # Sem registro nenhum.
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert "a credencial 'portal' não está registrada na área do robô" in message
    assert "a credencial 'portal_otp' não está registrada na área do robô" in message
    assert not [p for _, p in bao.requests if p.startswith("rapa/")]
    # Registrada só na outra área, com outro tipo ou para outra origem: também não vale.
    await _register(
        app_db,
        tenant,
        "portal",
        CredentialKind.PASSWORD,
        "portal/acesso",
        area_id=tenant.other_area_id,
    )
    await _register(app_db, tenant, "portal_otp", CredentialKind.PASSWORD, "portal/otp")
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert "a credencial 'portal' não está registrada na área do robô" in message
    assert "a credencial 'portal_otp' está registrada com outro tipo" in message
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM credentials WHERE company_id = :c"), {"c": tenant.company_id}
        )
    await _register(
        app_db,
        tenant,
        "portal",
        CredentialKind.PASSWORD,
        "portal/acesso",
        origin="https://outro-portal.rapa.test",
    )
    await _register(app_db, tenant, "portal_otp", CredentialKind.TOTP, "portal/otp")
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert message == "a credencial 'portal' está registrada para outra origem"
    # Revogada.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM credentials WHERE company_id = :c"), {"c": tenant.company_id}
        )
    await _portal_registered(app_db, tenant)
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credentials SET revoked_at = now(), revoked_by = 'adm' "
                "WHERE company_id = :c AND name = 'portal'"
            ),
            {"c": tenant.company_id},
        )
    assert await _unavailable(dispatcher_db, bao, tmp_path, job) == (
        "a credencial 'portal' foi revogada"
    )


async def test_a_missing_field_that_a_step_types_is_unavailable(
    app_db: Database, dispatcher_db: Database, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Cofre incompleto")
    job = await _run(app_db, tenant)
    await _portal_registered(app_db, tenant)
    bao = FakeBao()
    bao.put_secret(f"empresas/{tenant.company_id}/portal/acesso", {"username": "ana"})
    bao.put_secret(f"empresas/{tenant.company_id}/portal/otp", {"totp_secret": 123})
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert "falta 'password' no segredo da credencial 'portal'" in message
    assert "o campo 'totp_secret' da credencial 'portal_otp' não é texto" in message


async def test_the_a1_certificate_comes_as_pfx_base64_and_only_when_valid(
    app_db: Database, dispatcher_db: Database, superuser_engine: AsyncEngine, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Cofre com A1")
    job = await _run(app_db, tenant, a1_protocolo())
    await _portal_registered(app_db, tenant)
    credential = await _register(
        app_db,
        tenant,
        "certificado",
        CredentialKind.PFX_A1,
        "a1/principal",
        not_after=datetime.now(UTC) + timedelta(days=30),
        fingerprint_sha256="ab" * 32,
    )
    bao = FakeBao()
    _put_portal(bao, tenant.company_id)
    encoded = base64.b64encode(PFX).decode()
    bao.put_secret(
        f"empresas/{tenant.company_id}/a1/principal",
        {"pfx_base64": encoded, "pfx_password": "senha-do-pfx"},
    )
    found = await _secrets(dispatcher_db, bao, tmp_path, job)
    assert found["certificado"] == {"pfx_base64": encoded, "pfx_password": "senha-do-pfx"}
    # Sem o PFX no cofre: falta.
    bao.put_secret(f"empresas/{tenant.company_id}/a1/principal", {"pfx_password": "x"})
    assert "falta 'pfx_base64'" in await _unavailable(dispatcher_db, bao, tmp_path, job)
    # Vencido: nem vai ao cofre.
    async with superuser_engine.begin() as conn:
        await conn.execute(
            text("UPDATE credentials SET not_after = now() - interval '1 day' WHERE id = :id"),
            {"id": credential},
        )
    bao.requests.clear()
    message = await _unavailable(dispatcher_db, bao, tmp_path, job)
    assert "o certificado 'certificado' venceu" in message
    assert not any("a1/principal" in path for _, path in bao.requests)


async def test_vault_down_delivers_nothing(
    app_db: Database, dispatcher_db: Database, tmp_path: Path
) -> None:
    tenant = await make_tenant(app_db, "Cofre fora do ar")
    job = await _run(app_db, tenant)
    await _portal_registered(app_db, tenant)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectTimeout("sem resposta")

    client = OpenBaoClient(
        BaoSettings(address=ADDRESS, role=ROLE, sa_token_file=sa_token_file(tmp_path)),
        transport=httpx2.MockTransport(down),
    )
    try:
        with pytest.raises(SecretsUnavailable, match="o cofre não respondeu"):
            await OpenBaoSecretSource(dispatcher_db, client).secrets_for(job)
    finally:
        await client.aclose()


def test_required_fields_follow_the_steps_and_the_a1() -> None:
    assert required_fields(protocolo()) == {
        "portal": {"username", "password"},
        "portal_otp": {"totp_secret"},
    }
    assert required_fields(a1_protocolo())["certificado"] == {"pfx_base64"}
