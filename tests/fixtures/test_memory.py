import pytest

from rapa.core.contracts import Conflict, Forbidden, Role, RunState, RunTrigger, VersionStatus
from rapa.core.ids import new_id
from rapa.fixtures.memory import MemoryPlatform, make_principal
from rapa.fixtures.robots import emitir_protocolo

BASE = "http://portal-v1.rapa.test:8080"
HOST = "portal-v1.rapa.test"
INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}


async def published_platform() -> tuple[MemoryPlatform, dict[str, object]]:
    platform = MemoryPlatform()
    company, area = new_id(), new_id()
    creator = make_principal(company, Role.CREATOR, subject="criadora")
    approver = make_principal(company, Role.APPROVER, Role.OPERATOR, subject="aprovador")
    version = await platform.create_version(creator, area, emitir_protocolo(BASE, HOST))
    await platform.mark_validated(creator, version.robot_id, 1)
    await platform.publish(approver, version.robot_id, 1)
    return platform, {"company": company, "area": area, "creator": creator, "approver": approver}


async def test_lifecycle_and_catalog() -> None:
    platform, ctx = await published_platform()
    catalog = await platform.list_published(ctx["approver"])  # type: ignore[arg-type]
    assert [s.slug for s in catalog] == ["emitir_protocolo"]
    assert catalog[0].effect.value == "irreversible"
    found = await platform.search(ctx["approver"], "emissão protocolo")  # type: ignore[arg-type]
    assert found and found[0].slug == "emitir_protocolo"


async def test_creator_cannot_approve_own_version() -> None:
    platform = MemoryPlatform()
    company, area = new_id(), new_id()
    both = make_principal(company, Role.CREATOR, Role.APPROVER, subject="mesma")
    version = await platform.create_version(both, area, emitir_protocolo(BASE, HOST))
    await platform.mark_validated(both, version.robot_id, 1)
    with pytest.raises(Forbidden):
        await platform.publish(both, version.robot_id, 1)
    info = await platform.get_version(both, version.robot_id, 1)
    assert info is not None and info.status is VersionStatus.VALIDATED


async def test_other_company_sees_nothing() -> None:
    platform, ctx = await published_platform()
    outsider = make_principal(new_id(), Role.COMPANY_ADMIN)
    assert await platform.list_published(outsider) == []
    run = await platform.start(ctx["approver"], "emitir_protocolo", INPUTS, RunTrigger.REST)  # type: ignore[arg-type]
    assert await platform.get(outsider, run.run_id) is None


async def test_idempotency() -> None:
    platform, ctx = await published_platform()
    p = ctx["approver"]
    a = await platform.start(p, "emitir_protocolo", INPUTS, RunTrigger.MCP, "chave-1")  # type: ignore[arg-type]
    b = await platform.start(p, "emitir_protocolo", INPUTS, RunTrigger.MCP, "chave-1")  # type: ignore[arg-type]
    assert a.run_id == b.run_id
    with pytest.raises(Conflict):
        await platform.start(
            p, "emitir_protocolo", {**INPUTS, "uf": "RJ"}, RunTrigger.MCP, "chave-1"
        )  # type: ignore[arg-type]
    done = platform.set_run_state(a.run_id, RunState.SUCCEEDED, outputs={"protocolo": "PRT-1"})
    assert done.finished_at is not None
