"""Auxiliares dos testes de observabilidade: ler as amostras das métricas."""

import re

from prometheus_client.parser import text_string_to_metric_families

from rapa.control.observability.metrics import Metrics

#: Um uuid em qualquer lugar do texto das métricas.
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def samples(metrics: Metrics, name: str) -> dict[tuple[tuple[str, str], ...], float]:
    """As amostras de ``name`` (o nome da amostra, com ``_total`` nos contadores)."""
    out: dict[tuple[tuple[str, str], ...], float] = {}
    for family in text_string_to_metric_families(metrics.render().decode()):
        for sample in family.samples:
            if sample.name == name:
                out[tuple(sorted(sample.labels.items()))] = sample.value
    return out


def value(metrics: Metrics, name: str, **labels: str) -> float | None:
    """O valor da amostra com exatamente esses rótulos (``None`` se não houver)."""
    return samples(metrics, name).get(tuple(sorted(labels.items())))


def fragments(secret: str, size: int = 4) -> set[str]:
    """Todos os pedaços de ``size`` caracteres de ``secret`` (nenhum pode vazar)."""
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


def leaked(secret: str, text: str, size: int = 4) -> set[str]:
    """Os pedaços de ``secret`` que aparecem em ``text`` (vazio: nada vazou)."""
    return {piece for piece in fragments(secret, size) if piece in text}
