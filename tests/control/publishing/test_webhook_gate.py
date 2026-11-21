"""A porta da entrega dos webhooks, antes do banco: desconhecidos, recusas por cliente e leituras."""

import pytest

from rapa.control.services.webhooks import WebhookGate, sign_delivery
from rapa.core.contracts import QuotaExceeded
from rapa.core.ids import new_id


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_unknown_webhooks_are_remembered_for_a_while_and_the_list_is_bounded() -> None:
    clock = Clock()
    gate = WebhookGate(unknown_ttl_s=60, max_keys=3, clock=clock)
    first = new_id()
    assert not gate.known_unknown(first)
    gate.remember_unknown(first)
    assert gate.known_unknown(first)
    clock.now += 59
    assert gate.known_unknown(first)
    clock.now += 1
    assert not gate.known_unknown(first)  # passou o prazo: o banco é consultado de novo
    # A lista guarda só os mais recentes.
    ids = [new_id() for _ in range(4)]
    for webhook_id in ids:
        gate.remember_unknown(webhook_id)
    assert [gate.known_unknown(w) for w in ids] == [False, True, True, True]
    # Sem prazo, nada é guardado.
    off = WebhookGate(unknown_ttl_s=0, clock=clock)
    off.remember_unknown(first)
    assert not off.known_unknown(first)


def test_lookups_of_the_whole_process_have_a_bucket() -> None:
    clock = Clock()
    gate = WebhookGate(lookups_per_s=2, clock=clock)
    for _ in range(4):  # folga de duas vezes o limite por segundo
        gate.admit("10.0.0.1")
    with pytest.raises(QuotaExceeded) as caught:
        gate.admit("10.0.0.2")  # o balde é do processo, não do cliente
    assert caught.value.retry_after_s == 1
    clock.now += 0.5
    gate.admit("10.0.0.3")
    with pytest.raises(QuotaExceeded):
        gate.admit("10.0.0.3")


def test_refusals_of_a_client_spend_its_bucket_and_good_deliveries_do_not() -> None:
    clock = Clock()
    gate = WebhookGate(lookups_per_s=1000, failures_per_minute=3, clock=clock)
    # Quem só entrega certo nunca é limitado aqui.
    for _ in range(100):
        gate.admit("10.0.0.1")
    for _ in range(3):
        gate.admit("10.0.0.9")
        gate.failed("10.0.0.9")
    with pytest.raises(QuotaExceeded) as caught:
        gate.admit("10.0.0.9")
    assert caught.value.retry_after_s == 20  # 3 por minuto: uma ficha a cada 20 s
    gate.admit("10.0.0.1")  # os outros clientes seguem
    clock.now += 20
    gate.admit("10.0.0.9")
    # Sem endereço conhecido, todos dividem o mesmo balde.
    for _ in range(3):
        gate.failed(None)
    with pytest.raises(QuotaExceeded):
        gate.admit(None)


def test_a_slow_process_bucket_still_lets_one_lookup_through() -> None:
    clock = Clock()
    gate = WebhookGate(lookups_per_s=0.1, clock=clock)
    gate.admit("x")
    with pytest.raises(QuotaExceeded) as caught:
        gate.admit("x")
    assert caught.value.retry_after_s == 10
    clock.now += 10
    gate.admit("x")


def test_gate_limits_must_be_positive() -> None:
    for kwargs in (
        {"lookups_per_s": 0},
        {"failures_per_minute": 0},
        {"unknown_ttl_s": -1},
        {"max_keys": 0},
    ):
        with pytest.raises(ValueError):
            WebhookGate(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("delivery", ["e.1", ".", "a" * 201, "", "com espaço", "ç"])
def test_signing_helper_refuses_delivery_ids_out_of_format(delivery: str) -> None:
    # Sem ponto no identificador, ``<timestamp>.<delivery>.<corpo>`` só se separa de um jeito.
    with pytest.raises(ValueError):
        sign_delivery("k" * 40, "1700000000", delivery, b"{}")
    with pytest.raises(ValueError):
        sign_delivery("k" * 40, "17.0", "e-1", b"{}")
