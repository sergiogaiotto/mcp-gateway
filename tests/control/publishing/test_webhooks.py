"""Webhooks no PostgreSQL: a chave mostrada uma vez, a assinatura, repetição, entradas e limite."""

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapa.control.db import Database
from rapa.control.db.schema import TENANT_TABLES
from rapa.control.services import PgWebhooks, WebhookKeys, verify_chain
from rapa.control.services._common import PRIORITY_BY_TRIGGER
from rapa.control.services.webhooks import WebhookGate, WebhookLimiter, sign_delivery
from rapa.core.contracts import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    QuotaExceeded,
    RunState,
    RunTrigger,
    SignatureInvalid,
    WebhookChanges,
    WebhookSpec,
)
from rapa.core.ids import new_id
from tests.control.db.support import PROTOCOLO_INPUTS

from .support import SERVER_KEY, World, build_world, runs_of, signed, table_dump

pytestmark = pytest.mark.database

BODY = json.dumps(PROTOCOLO_INPUTS).encode()


async def _world(app_db: Database, superuser: AsyncEngine) -> World:
    world = await build_world(app_db, superuser)
    return world


async def _create(world: World, **spec: Any) -> tuple[Any, str]:
    created = await world.webhooks.create_webhook(
        world.a.operator,
        world.robots_a["emitir_protocolo"],
        WebhookSpec(title="Pedidos do ERP", **spec),
    )
    return created.webhook, created.secret


async def test_secret_is_shown_once_and_never_stored(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    assert secret.startswith("whsec_") and len(secret) > 40
    assert hook.path == f"/v1/webhooks/{hook.webhook_id}"
    assert hook.key_version == 1 and hook.enabled and hook.rate_per_minute == 60
    assert "secret" not in repr(
        await world.webhooks.create_webhook(
            world.a.operator, world.robots_a["baixar_notas"], WebhookSpec(title="Outro")
        )
    )
    # Leituras nunca trazem a chave.
    listed = await world.webhooks.list_webhooks(world.a.viewer, world.robots_a["emitir_protocolo"])
    assert [h.webhook_id for h in listed] == [hook.webhook_id]
    assert secret not in listed[0].model_dump_json()
    # Nem o banco nem a auditoria guardam a chave (nem a chave do servidor).
    dump = await table_dump(superuser_engine, "webhooks", "audit_log", "webhook_deliveries")
    assert secret not in dump and SERVER_KEY not in dump
    # A chave depende da chave do servidor: outra chave do servidor, outra chave.
    other = PgWebhooks(app_db, WebhookKeys("outra-chave-do-servidor-com-32-caracteres!!"))
    with pytest.raises(SignatureInvalid):
        await other.deliver(hook.webhook_id, **signed(secret, BODY, "e-1", at=world.clock.now))


async def test_valid_delivery_creates_a_webhook_run_and_repeats_are_idempotent(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    now = world.clock.now
    delivery = await world.webhooks.deliver(
        hook.webhook_id, **signed(secret, BODY, "pedido-1", at=now)
    )
    assert delivery.state is RunState.QUEUED and not delivery.duplicate
    [run] = await runs_of(superuser_engine, "webhook", world.a.company_id)
    assert run["id"] == delivery.run_id and run["inputs"] == PROTOCOLO_INPUTS
    assert run["priority"] == PRIORITY_BY_TRIGGER[RunTrigger.WEBHOOK] == 2
    assert run["requested_by"] == f"webhook:{hook.webhook_id}"
    # A mesma entrega de novo (o sistema repetiu): a mesma execução, sem criar outra.
    again = await world.webhooks.deliver(
        hook.webhook_id, **signed(secret, BODY, "pedido-1", at=now + timedelta(seconds=30))
    )
    assert again.duplicate and again.run_id == delivery.run_id
    # O mesmo identificador com outro corpo é conflito.
    other = json.dumps({**PROTOCOLO_INPUTS, "nome": "Outra Pessoa"}).encode()
    with pytest.raises(Conflict):
        await world.webhooks.deliver(hook.webhook_id, **signed(secret, other, "pedido-1", at=now))
    # Outra entrega, outra execução.
    third = await world.webhooks.deliver(
        hook.webhook_id, **signed(secret, other, "pedido-2", at=now)
    )
    assert third.run_id != delivery.run_id
    assert len(await runs_of(superuser_engine, "webhook", world.a.company_id)) == 2
    info = await world.webhooks.get_webhook(
        world.a.operator, world.robots_a["emitir_protocolo"], hook.webhook_id
    )
    assert info is not None and info.last_delivery_at is not None
    status = await world.platform.get(world.a.viewer, delivery.run_id)
    assert status is not None and status.trigger is RunTrigger.WEBHOOK
    async with app_db.tenant_session(world.a.company_id) as session:
        assert (await verify_chain(session, world.a.company_id)).ok


@pytest.mark.parametrize(
    "tamper",
    [
        "wrong_secret",
        "tampered_body",
        "other_delivery_id",
        "stale",
        "future",
        "bad_format",
        "missing_signature",
        "missing_timestamp",
        "missing_delivery",
        "unknown_webhook",
    ],
)
async def test_bad_signatures_are_refused_alike(
    app_db: Database, superuser_engine: AsyncEngine, tamper: str
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    now = world.clock.now
    webhook_id = hook.webhook_id
    args = signed(secret, BODY, "e-1", at=now)
    match tamper:
        case "wrong_secret":
            args = signed(secret + "x", BODY, "e-1", at=now)
        case "tampered_body":
            args["body"] = BODY.replace(b"Maria", b"Mario")
        case "other_delivery_id":
            # A assinatura cobre o identificador: copiar uma entrega com outro não vale.
            args["delivery_id"] = "e-2"
        case "stale":
            args = signed(secret, BODY, "e-1", at=now - timedelta(seconds=301))
        case "future":
            args = signed(secret, BODY, "e-1", at=now + timedelta(seconds=301))
        case "bad_format":
            args["signature"] = "md5=" + args["signature"][7:]
        case "missing_signature":
            args["signature"] = None
        case "missing_timestamp":
            args["timestamp"] = None
        case "missing_delivery":
            args["delivery_id"] = None
        case "unknown_webhook":
            webhook_id = new_id()
    with pytest.raises(SignatureInvalid) as caught:
        await world.webhooks.deliver(webhook_id, **args)
    # A mesma mensagem para todos: nada diz se o webhook existe.
    assert caught.value.message == (
        "assinatura inválida, horário fora da tolerância ou webhook desconhecido"
    )
    assert await runs_of(superuser_engine, "webhook", world.a.company_id) == []


async def test_clock_skew_within_five_minutes_is_accepted(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    for offset, delivery in ((-299, "a"), (299, "b")):
        at = world.clock.now + timedelta(seconds=offset)
        await world.webhooks.deliver(hook.webhook_id, **signed(secret, BODY, delivery, at=at))
    assert len(await runs_of(superuser_engine, "webhook", world.a.company_id)) == 2


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (json.dumps({**PROTOCOLO_INPUTS, "senha": "segredo-123"}), "campo não declarado: senha"),
        (json.dumps({"nome": "Maria", "uf": "SP"}), "campo obrigatório ausente: cpf"),
        (json.dumps({**PROTOCOLO_INPUTS, "cpf": "segredo-123"}), "cpf: valor fora do formato"),
        (json.dumps([PROTOCOLO_INPUTS]), "corpo: esperado objeto"),
        ("{não é json", "corpo: JSON inválido"),
        ('{"nome": NaN, "cpf": "529.982.247-25", "uf": "SP"}', "corpo: JSON inválido"),
        ("[" * 100_000 + "]" * 100_000, "corpo: JSON inválido"),
    ],
)
async def test_body_is_validated_against_the_inputs(
    app_db: Database, superuser_engine: AsyncEngine, body: str, detail: str
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    raw = body.encode()
    with pytest.raises(InvalidInput) as caught:
        await world.webhooks.deliver(
            hook.webhook_id, **signed(secret, raw, "e-1", at=world.clock.now)
        )
    assert any(detail in error for error in caught.value.errors), caught.value.errors
    assert "segredo-123" not in repr(caught.value.errors)
    assert await runs_of(superuser_engine, "webhook", world.a.company_id) == []
    # A entrega recusada não ocupa o identificador.
    ok = await world.webhooks.deliver(
        hook.webhook_id, **signed(secret, BODY, "e-1", at=world.clock.now)
    )
    assert not ok.duplicate


async def test_rate_limit_per_webhook(app_db: Database, superuser_engine: AsyncEngine) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world, rate_per_minute=2)
    other, other_secret = await _create(world, rate_per_minute=2)
    now = world.clock.now
    for n in range(2):
        await world.webhooks.deliver(hook.webhook_id, **signed(secret, BODY, f"e-{n}", at=now))
    with pytest.raises(QuotaExceeded) as caught:
        await world.webhooks.deliver(hook.webhook_id, **signed(secret, BODY, "e-9", at=now))
    assert caught.value.retry_after_s == 30
    # Cada webhook tem o seu balde.
    await world.webhooks.deliver(other.webhook_id, **signed(other_secret, BODY, "x", at=now))
    # Uma assinatura inválida não gasta o limite de ninguém.
    world.clock.mono += 30
    for _ in range(5):
        with pytest.raises(SignatureInvalid):
            await world.webhooks.deliver(hook.webhook_id, **signed("errada" * 6, BODY, "y", at=now))
    await world.webhooks.deliver(hook.webhook_id, **signed(secret, BODY, "e-10", at=now))


async def test_rotate_disable_and_delete(app_db: Database, superuser_engine: AsyncEngine) -> None:
    world = await _world(app_db, superuser_engine)
    robot = world.robots_a["emitir_protocolo"]
    operator = world.a.operator
    hook, old = await _create(world)
    now = world.clock.now
    rotated = await world.webhooks.rotate_secret(operator, robot, hook.webhook_id)
    assert rotated.secret != old and rotated.webhook.key_version == 2
    assert rotated.webhook.rotated_at is not None
    with pytest.raises(SignatureInvalid):
        await world.webhooks.deliver(hook.webhook_id, **signed(old, BODY, "e-1", at=now))
    await world.webhooks.deliver(hook.webhook_id, **signed(rotated.secret, BODY, "e-1", at=now))
    off = await world.webhooks.update_webhook(
        operator, robot, hook.webhook_id, WebhookChanges(enabled=False, title="Novo nome")
    )
    assert not off.enabled and off.title == "Novo nome"
    with pytest.raises(Conflict):
        await world.webhooks.deliver(hook.webhook_id, **signed(rotated.secret, BODY, "e-2", at=now))
    await world.webhooks.delete_webhook(operator, robot, hook.webhook_id)
    assert await world.webhooks.get_webhook(operator, robot, hook.webhook_id) is None
    assert await world.webhooks.list_webhooks(operator, robot) == []
    # Removido: como se não existisse.
    with pytest.raises(SignatureInvalid):
        await world.webhooks.deliver(hook.webhook_id, **signed(rotated.secret, BODY, "e-3", at=now))
    with pytest.raises(NotFound):
        await world.webhooks.rotate_secret(operator, robot, hook.webhook_id)
    async with app_db.tenant_session(world.a.company_id) as session:
        actions = set((await session.scalars(text("SELECT action FROM audit_log"))).all())
    assert {
        "webhook.criado",
        "webhook.chave_trocada",
        "webhook.alterado",
        "webhook.removido",
    } <= actions


async def test_permissions_and_other_companies(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    a, b = world.a, world.b
    robot = world.robots_a["emitir_protocolo"]
    for who in (a.viewer, a.creator):
        with pytest.raises(Forbidden):
            await world.webhooks.create_webhook(who, robot, WebhookSpec(title="x"))
    hook, _ = await _create(world)
    for who in (a.viewer, a.creator):
        with pytest.raises(Forbidden):
            await world.webhooks.rotate_secret(who, robot, hook.webhook_id)
        with pytest.raises(Forbidden):
            await world.webhooks.delete_webhook(who, robot, hook.webhook_id)
    for who in (a.outsider, b.admin):
        with pytest.raises(NotFound):
            await world.webhooks.list_webhooks(who, robot)
        assert await world.webhooks.get_webhook(who, robot, hook.webhook_id) is None
        with pytest.raises(NotFound):
            await world.webhooks.rotate_secret(who, robot, hook.webhook_id)
        with pytest.raises(NotFound):
            await world.webhooks.update_webhook(
                who, robot, hook.webhook_id, WebhookChanges(enabled=False)
            )
    # O robô da B com o webhook da A: não é dele.
    b_robot = world.robots_b["emitir_protocolo"]
    assert await world.webhooks.get_webhook(b.admin, b_robot, hook.webhook_id) is None


async def test_lookup_without_company_sees_only_that_webhook(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, _ = await _create(world)
    b_hook = await world.webhooks.create_webhook(
        world.b.operator, world.robots_b["emitir_protocolo"], WebhookSpec(title="B")
    )
    async with app_db.webhook_lookup_session(hook.webhook_id) as session:
        visible = (await session.execute(text("SELECT id, company_id FROM webhooks"))).all()
        assert visible == [(hook.webhook_id, world.a.company_id)]
        for table in TENANT_TABLES:
            if table.name == "webhooks":
                continue
            count = await session.scalar(text(f"SELECT count(*) FROM {table.name}"))  # noqa: S608
            assert count == 0, table.name
    # Nada se grava assim.
    with pytest.raises(DBAPIError) as caught:
        async with app_db.webhook_lookup_session(hook.webhook_id) as session:
            await session.execute(
                text("UPDATE webhooks SET title = 'tomado' WHERE id = :id"), {"id": hook.webhook_id}
            )
            await session.execute(
                text(
                    "INSERT INTO webhook_deliveries (company_id, webhook_id, delivery_id, "
                    "body_sha256, run_id) VALUES (:c, :w, 'x', repeat('0', 64), :r)"
                ),
                {"c": world.a.company_id, "w": hook.webhook_id, "r": new_id()},
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    async with superuser_engine.connect() as conn:
        title = await conn.scalar(
            text("SELECT title FROM webhooks WHERE id = :id"), {"id": hook.webhook_id}
        )
    assert title == "Pedidos do ERP"
    # Com uma empresa fixada, o identificador do webhook não abre nada de outra empresa.
    async with app_db.tenant_session(world.a.company_id) as session:
        await session.execute(
            text("SELECT set_config('rapa.webhook_id', :id, true)"),
            {"id": str(b_hook.webhook.webhook_id)},
        )
        ids = set((await session.scalars(text("SELECT id FROM webhooks"))).all())
    assert b_hook.webhook.webhook_id not in ids and hook.webhook_id in ids


def test_signature_format_is_documented() -> None:
    assert sign_delivery("k" * 40, "1700000000", "e-1", b"{}") == (
        "sha256=" + __import__("hmac").new(b"k" * 40, b"1700000000.e-1.{}", "sha256").hexdigest()
    )
    with pytest.raises(ValueError):
        WebhookKeys("curta")
    assert "chave" not in repr(WebhookKeys(SERVER_KEY)) and SERVER_KEY not in repr(
        WebhookKeys(SERVER_KEY)
    )


def _counting(world: World, gate: WebhookGate) -> tuple[PgWebhooks, list[object]]:
    """Os webhooks da empresa A com a porta dada, contando as leituras no banco."""
    hooks = PgWebhooks(
        world.webhooks.db,
        WebhookKeys(SERVER_KEY),
        limiter=WebhookLimiter(clock=world.clock.monotonic),
        gate=gate,
        clock=world.clock,
    )
    lookups: list[object] = []
    original = hooks._lookup

    async def counting(webhook_id: Any) -> Any:
        lookups.append(webhook_id)
        return await original(webhook_id)

    hooks._lookup = counting  # type: ignore[method-assign]
    return hooks, lookups


async def _outcome(hooks: PgWebhooks, webhook_id: Any, args: dict[str, Any], client: str) -> int:
    try:
        await hooks.deliver(webhook_id, **args, client=client)
    except SignatureInvalid:
        return 401
    except QuotaExceeded:
        return 429
    return 202


async def test_unknown_ids_and_refusing_clients_are_answered_before_the_database(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    gate = WebhookGate(lookups_per_s=1000, failures_per_minute=5, clock=world.clock.monotonic)
    hooks, lookups = _counting(world, gate)
    # O processo está apertado (mais da metade do balde gasta): só assim o endereço que
    # erra demais espera (veja o teste seguinte).
    for _ in range(1001):
        gate.admit("outros")
    now = world.clock.now
    attacker, partner = "203.0.113.7", "198.51.100.1"
    args = signed(secret, BODY, "e-1", at=now)
    # O mesmo identificador desconhecido, de novo e de novo: só o primeiro vai ao banco.
    unknown = new_id()
    assert [await _outcome(hooks, unknown, args, attacker) for _ in range(10)] == [401] * 10
    assert len(lookups) == 1
    # Identificadores sempre novos: cada recusa gasta uma ficha do endereço; sem fichas,
    # 429 sem ir ao banco.
    outcomes = [await _outcome(hooks, new_id(), args, attacker) for _ in range(10)]
    assert outcomes == [401] * 4 + [429] * 6
    assert len(lookups) == 5
    # Os outros clientes seguem entregando, e a entrega certa não gasta ficha nenhuma.
    for n in range(8):
        good = signed(secret, BODY, f"p-{n}", at=now)
        assert await _outcome(hooks, hook.webhook_id, good, partner) == 202
    # Uma assinatura errada ou um horário velho também contam como recusa do cliente.
    wrong = signed("errada" * 7, BODY, "p-9", at=now)
    stale = signed(secret, BODY, "p-9", at=now - timedelta(seconds=301))
    assert await _outcome(hooks, hook.webhook_id, wrong, "192.0.2.5") == 401
    assert await _outcome(hooks, hook.webhook_id, stale, "192.0.2.5") == 401
    for _ in range(3):
        await _outcome(hooks, hook.webhook_id, wrong, "192.0.2.5")
    assert (
        await _outcome(hooks, hook.webhook_id, signed(secret, BODY, "p-9", at=now), "192.0.2.5")
        == 429
    )
    # O endereço que errou demais espera a ficha voltar (5 por minuto: uma a cada 12 s).
    world.clock.mono += 12
    assert (
        await _outcome(hooks, hook.webhook_id, signed(secret, BODY, "p-9", at=now), "192.0.2.5")
        == 202
    )
    assert len(await runs_of(superuser_engine, "webhook", world.a.company_id)) == 9


async def test_refusals_from_a_shared_address_do_not_block_its_good_deliveries(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    # Atrás de um proxy sem FORWARDED_ALLOW_IPS (ou de um NAT), todos chegam do mesmo
    # endereço: quem manda uma recusa por segundo não deixa os outros sem entregar.
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    hooks, lookups = _counting(world, WebhookGate(clock=world.clock.monotonic))
    now = world.clock.now
    proxy = "10.0.0.1"
    for _ in range(120):  # meio minuto, quatro recusas por segundo
        world.clock.mono += 0.25
        await _outcome(hooks, new_id(), signed(secret, BODY, "x", at=now), proxy)
    good = signed(secret, BODY, "e-1", at=now)
    assert await _outcome(hooks, hook.webhook_id, good, proxy) == 202
    assert len(lookups) == 121  # o processo segue protegido pelo balde dele


async def test_lookups_of_the_process_are_limited_before_the_database(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    hooks, lookups = _counting(world, WebhookGate(lookups_per_s=1, clock=world.clock.monotonic))
    now = world.clock.now
    outcomes = [
        await _outcome(
            hooks, hook.webhook_id, signed(secret, BODY, f"e-{n}", at=now), f"10.0.0.{n}"
        )
        for n in range(4)
    ]
    # Folga de duas leituras; depois, 429 para todos, sem ir ao banco.
    assert outcomes == [202, 202, 429, 429] and len(lookups) == 2
    with pytest.raises(QuotaExceeded) as caught:
        await hooks.deliver(hook.webhook_id, **signed(secret, BODY, "e-9", at=now))
    assert caught.value.retry_after_s == 1
    world.clock.mono += 1
    assert await _outcome(hooks, hook.webhook_id, signed(secret, BODY, "e-9", at=now), "x") == 202


async def test_removed_webhook_is_remembered_as_unknown(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    hooks, lookups = _counting(world, WebhookGate(clock=world.clock.monotonic))
    now = world.clock.now
    assert await _outcome(hooks, hook.webhook_id, signed(secret, BODY, "e-1", at=now), "x") == 202
    await world.webhooks.delete_webhook(
        world.a.operator, world.robots_a["emitir_protocolo"], hook.webhook_id
    )
    for n in range(5):
        args = signed(secret, BODY, f"e-{n + 2}", at=now)
        assert await _outcome(hooks, hook.webhook_id, args, "x") == 401
    assert len(lookups) == 2  # a entrega boa e a primeira depois da remoção


@pytest.mark.parametrize("delivery", ["e-1.a", "a.b.c", "."])
async def test_delivery_ids_with_a_dot_are_refused(
    app_db: Database, superuser_engine: AsyncEngine, delivery: str
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    stamp = str(int(world.clock.now.timestamp()))
    # Uma entrega legítima de "e-1" com o corpo 'a.{...}' assina "<ts>.e-1.a.{...}": com
    # um ponto no identificador, a mesma assinatura serviria para "e-1.a" com o corpo
    # '{...}', que é um objeto JSON válido (outra execução, sem a chave).
    head, _, rest = delivery.partition(".")
    captured = sign_delivery(secret, stamp, head or "x", rest.encode() + b"." + BODY)
    with pytest.raises(SignatureInvalid):
        await world.webhooks.deliver(
            hook.webhook_id,
            timestamp=stamp,
            delivery_id=delivery,
            signature=captured,
            body=BODY,
        )
    assert await runs_of(superuser_engine, "webhook", world.a.company_id) == []


async def test_database_refuses_delivery_ids_with_a_dot(
    app_db: Database, superuser_engine: AsyncEngine
) -> None:
    world = await _world(app_db, superuser_engine)
    hook, secret = await _create(world)
    delivery = await world.webhooks.deliver(
        hook.webhook_id, **signed(secret, BODY, "e-1", at=world.clock.now)
    )
    with pytest.raises(DBAPIError) as caught:
        async with superuser_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO webhook_deliveries (company_id, webhook_id, delivery_id, "
                    "body_sha256, run_id) VALUES (:c, :w, 'e.2', repeat('0', 64), :r)"
                ),
                {"c": world.a.company_id, "w": hook.webhook_id, "r": delivery.run_id},
            )
    assert getattr(caught.value.orig, "sqlstate", None) == "23514"


def test_body_depth_is_bounded_before_reading() -> None:
    # O limite vale do mesmo jeito em qualquer máquina: o leitor de JSON aguentaria mais ou
    # menos níveis conforme a pilha.
    from rapa.control.services.webhooks import MAX_BODY_DEPTH, _check_depth

    _check_depth(b"[" * MAX_BODY_DEPTH + b"]" * MAX_BODY_DEPTH)
    _check_depth(b'{"a": "' + b"[" * 1000 + b'"}')  # dentro de um texto não conta
    _check_depth(b'{"a": "\\"}')  # barra escapada fecha o texto
    with pytest.raises(ValueError):
        _check_depth(b"[" * (MAX_BODY_DEPTH + 1))
