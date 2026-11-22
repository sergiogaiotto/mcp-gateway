"""As contas de rede dos templates (deploy/helm/rapa/templates/_cidr.tpl) contra o ``ipaddress``.

O chart lê cada CIDR em blocos de 16 bits para conferir os valores (bits de host,
sobreposição, contido em) e para fechar as redes de IPv4 também pelo NAT64. Aqui, um chart de
teste com o mesmo arquivo lê uma lista de textos difíceis, e cada resposta precisa ser a do
Python, que é quem lê RAPA_REDES_INTERNAS_PERMITIDAS na guarda de saída.
"""

import ipaddress
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.deploy.support import CHART, run_helm

#: Textos que o chart precisa ler como o Python lê (ou recusar quando o Python recusa, ou
#: quando a forma não é a canônica: zeros à esquerda e o IPv4 com pontos dentro do IPv6).
TEXTS = [
    "0.0.0.0/0",
    "0.0.0.0/00",
    "0.0.0.0/1",
    "128.0.0.0/1",
    "10.30.0.0/24",
    "10.30.0.1/24",
    "010.30.0.0/24",
    "10.30.0.0/024",
    "10.30.0.0/33",
    "256.0.0.0/8",
    "255.255.255.255/32",
    "169.254.169.254/32",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "100.127.255.255/32",
    "::/0",
    "::/000",
    "::/1",
    "::1/128",
    "::/96",
    "::ffff:0:0/96",
    "::ffff:a9fe:a9fe/128",
    "::ffff:169.254.169.254/128",
    "64:ff9b::/96",
    "64:ff9b::a9fe:a9fe/128",
    "2002::/16",
    "fd00:ec2::254/128",
    "FD00:EC2::/32",
    "fe80::/10",
    "2001:db8::/32",
    "2001:db8:0:0:0:0:0:1/127",
    "2001:0db8::/32",
    "1:2:3:4:5:6:7:8/128",
    "1:2:3:4:5:6:7::/112",
    "::2:3:4:5:6:7:8/128",
    "1::2::3/64",
    "1:::2/64",
    ":1::/64",
    "1:2:3:4:5:6:7:8:9/128",
    "12345::/16",
    "::/129",
    "rede-interna",
    "10.0.0.0",
    "",
]

#: Pares para a sobreposição e o "contido em".
PAIRS = [
    ("0.0.0.0/1", "127.0.0.0/8"),
    ("128.0.0.0/1", "127.0.0.0/8"),
    ("10.30.0.0/24", "10.0.0.0/8"),
    ("10.0.0.0/8", "10.30.0.0/24"),
    ("169.254.169.254/32", "169.254.0.0/16"),
    ("192.168.0.0/16", "169.254.0.0/16"),
    ("172.31.255.0/24", "172.16.0.0/12"),
    ("172.32.0.0/16", "172.16.0.0/12"),
    ("100.128.0.0/16", "100.64.0.0/10"),
    ("fd00:ec2::254/128", "fd00:ec2::/32"),
    ("fd00::/8", "fd00:ec2::/32"),
    ("fd01::/16", "fd00:ec2::/32"),
    ("2001:db8::/32", "::/96"),
    ("::/48", "::/96"),
    ("::1/128", "::/96"),
    ("64:ff9b::a9fe:a9fe/128", "64:ff9b::/96"),
    ("64:ff9b:1::/48", "64:ff9b::/96"),
    ("2001:db8:8000::/33", "2001:db8::/33"),
    ("10.0.0.0/8", "::/0"),
]

#: Redes de IPv4 que o chart escreve dentro do prefixo NAT64.
NAT64 = ["0.0.0.0/8", "10.0.0.0/8", "10.30.0.0/24", "203.0.113.0/24", "198.51.100.7/32"]

PROBE = """\
{{- $out := dict "parsed" (list) "pairs" (list) "nat64" (list) -}}
{{- range .Values.texts }}
{{- $n := dict }}
{{- include "rapa.cidrParse" (dict "text" . "out" $n) }}
{{- $_ := set $out "parsed" (append $out.parsed $n) }}
{{- end }}
{{- range .Values.pairs }}
{{- $a := dict }}{{- $b := dict }}
{{- include "rapa.cidrParse" (dict "text" (index . 0) "out" $a) }}
{{- include "rapa.cidrParse" (dict "text" (index . 1) "out" $b) }}
{{- $overlap := include "rapa.cidrOverlaps" (dict "a" $a "b" $b) }}
{{- $within := include "rapa.cidrWithin" (dict "a" $a "b" $b) }}
{{- $_ := set $out "pairs" (append $out.pairs (list $overlap $within)) }}
{{- end }}
{{- range .Values.nat64 }}
{{- $_ := set $out "nat64" (append $out.nat64 (include "rapa.nat64" (dict "text" .))) }}
{{- end }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: contas
data:
  resultado: {{ toJson $out | quote }}
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    chart = tmp_path_factory.mktemp("contas")
    (chart / "templates").mkdir()
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: contas\nversion: 0.1.0\n", "utf-8")
    shutil.copy(CHART / "templates" / "_cidr.tpl", chart / "templates" / "_cidr.tpl")
    (chart / "templates" / "contas.yaml").write_text(PROBE, encoding="utf-8")
    values: Path = chart / "valores.yaml"
    values.write_text(
        yaml.safe_dump({"texts": TEXTS, "pairs": [list(p) for p in PAIRS], "nat64": NAT64}),
        encoding="utf-8",
    )
    result = run_helm("template", "contas", str(chart), "-f", str(values))
    assert result.returncode == 0, result.stderr
    doc = yaml.safe_load(result.stdout)
    data: dict[str, Any] = json.loads(doc["data"]["resultado"])
    return data


def _python(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """O que o Python lê (estrito: sem bits de host), ou None; recusa também as formas não
    canônicas que o chart recusa (zeros à esquerda no prefixo e o IPv4 dentro do IPv6)."""
    address, _, prefix = text.partition("/")
    if (
        not prefix
        or (len(prefix) > 1 and prefix.startswith("0"))
        or ("." in address and ":" in address)
    ):
        return None
    try:
        return ipaddress.ip_network(text, strict=True)
    except ValueError:
        return None


def _words(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> list[int]:
    value, count = int(net.network_address), net.max_prefixlen // 16
    return [(value >> (16 * (count - 1 - i))) & 0xFFFF for i in range(count)]


@pytest.mark.parametrize("index", range(len(TEXTS)), ids=[t or "vazio" for t in TEXTS])
def test_the_chart_reads_each_cidr_like_python(results: dict[str, Any], index: int) -> None:
    text, parsed = TEXTS[index], results["parsed"][index]
    expected = _python(text)
    if expected is None:
        assert parsed["ok"] is False, text
        assert parsed["why"]
        return
    assert parsed["ok"] is True, (text, parsed.get("why"))
    assert parsed["version"] == expected.version
    assert parsed["prefix"] == expected.prefixlen
    assert parsed["words"] == _words(expected)


def test_leading_zeros_are_never_the_whole_internet(results: dict[str, Any]) -> None:
    """O que o Python e o Kubernetes leem como /0 o chart nunca aceita como CIDR."""
    for text in ("0.0.0.0/00", "::/000", "010.30.0.0/24", "10.30.0.0/024"):
        assert results["parsed"][TEXTS.index(text)]["ok"] is False, text


@pytest.mark.parametrize("index", range(len(PAIRS)), ids=[f"{a}~{b}" for a, b in PAIRS])
def test_overlap_and_within_like_python(results: dict[str, Any], index: int) -> None:
    a, b = (ipaddress.ip_network(n) for n in PAIRS[index])
    overlap, within = results["pairs"][index]
    same = a.version == b.version
    assert (overlap == "true") is (same and a.overlaps(b))
    assert (within == "true") is (same and a.subnet_of(b))  # type: ignore[arg-type]


def test_ipv4_networks_inside_the_nat64_prefix(results: dict[str, Any]) -> None:
    nat64 = ipaddress.ip_network("64:ff9b::/96")
    for text, written in zip(NAT64, results["nat64"], strict=True):
        v4 = ipaddress.ip_network(text)
        expected = ipaddress.IPv6Network(
            (int(nat64.network_address) | int(v4.network_address), 96 + v4.prefixlen)
        )
        assert ipaddress.ip_network(written) == expected, text
