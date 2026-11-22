"""Guarda da saída na execução: o navegador do robô só alcança os domínios dele.

O ``route`` do Playwright não vê websockets; a guarda vê (é a mesma do hospedeiro da
sessão, com a lista de hosts do manifesto).
"""

import http.server
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from rapa.core.contracts import RunnerJob, RunState
from rapa.core.ids import new_id
from rapa.core.robot import Manifest
from rapa.fixtures.server import HOST_RESOLVER_ARG
from rapa.runner.ports import ListEventSink
from rapa.runner.worker import build_parser, main, run_job
from rapa.session.egress import EgressPolicy

_PAGE = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><title>Página de teste</title></head>
<body>
<h1>Página de teste</h1>
<p id="situacao"></p>
<script>
const params = new URLSearchParams(location.search);
function tentar(url) {
  return new Promise((resolve) => {
    let feito = false;
    const fim = (valor) => { if (!feito) { feito = true; resolve(valor); } };
    try {
      const ws = new WebSocket(url);
      ws.onopen = () => { ws.close(); fim("aberto"); };
      ws.onerror = () => fim("erro");
    } catch (e) { fim("erro"); }
    setTimeout(() => fim("tempo"), 8000);
  });
}
(async () => {
  const fora = await tentar(params.get("fora"));
  const dentro = await tentar(params.get("dentro"));
  const el = document.getElementById("situacao");
  el.textContent = `fora=${fora} dentro=${dentro} webrtc=${typeof RTCPeerConnection}`;
  el.dataset.pronto = "1";
})();
</script>
</body></html>
"""


class _PageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = _PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return


@contextmanager
def page_server() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@asynccontextmanager
async def websocket_server() -> AsyncIterator[tuple[int, list[str]]]:
    """Um websocket em 127.0.0.1 que anota o ``Host`` de cada conexão."""
    hosts: list[str] = []

    async def handle(connection: ServerConnection) -> None:
        hosts.append(str(connection.request.headers.get("Host")) if connection.request else "")
        await connection.close()

    async with serve(handle, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        yield int(port), hosts


def _manifest(page_port: int, ws_port: int) -> Manifest:
    url = (
        f"http://pagina.rapa.test:{page_port}/?fora=ws://fora.rapa.test:{ws_port}/"
        f"&dentro=ws://pagina.rapa.test:{ws_port}/"
    )
    target = {
        "intent": "situação dos websockets",
        "locators": [{"by": "css", "value": "#situacao[data-pronto]"}],
    }
    return Manifest.model_validate(
        {
            "name": "abrir_websockets",
            "title": "Abrir websockets",
            "description": "Abre uma página que tenta dois websockets.",
            "allowed_domains": ["pagina.rapa.test"],
            "steps": [
                {"kind": "navigate", "id": "abrir", "title": "Abrir a página", "url": url},
                {"kind": "wait_for", "id": "esperar", "title": "Esperar", "target": target},
                {
                    "kind": "extract_text",
                    "id": "ler",
                    "title": "Ler a situação",
                    "target": target,
                    "into": "situacao",
                },
            ],
            "outputs": [
                {
                    "name": "situacao",
                    "title": "Situação",
                    "type": "string",
                    "source": "vars.situacao",
                }
            ],
        }
    )


async def _run(
    manifest: Manifest, tmp_path: Path, chromium: str, egress: EgressPolicy | None
) -> str:
    job = RunnerJob(
        run_id=new_id(),
        company_id=new_id(),
        attempt=1,
        manifest=manifest,
        inputs={},
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    result = await run_job(
        job,
        secrets=None,
        events=ListEventSink(),
        artifacts_dir=tmp_path / f"artefatos-{egress is not None}",
        chromium=chromium,
        # Sem proxy nenhum do ambiente (o Chromium usaria um https_proxy no websocket); com
        # a guarda, o interruptor do operador sai e não tira a guarda do caminho.
        browser_args=(HOST_RESOLVER_ARG, "--no-proxy-server"),
        egress=egress,
    )
    assert result.state is RunState.SUCCEEDED, result.error_message
    assert result.outputs is not None
    return str(result.outputs["situacao"])


@pytest.mark.browser
async def test_a_websocket_to_a_host_outside_the_list_is_refused_by_the_guard(
    chromium_path: str, tmp_path: Path
) -> None:
    with page_server() as page_port:
        async with websocket_server() as (ws_port, hosts):
            manifest = _manifest(page_port, ws_port)
            # Sem a guarda, o route do Playwright não vê o websocket: ele sai para fora.
            plain = await _run(manifest, tmp_path, chromium_path, None)
            assert plain == "fora=aberto dentro=aberto webrtc=function"
            assert sorted(hosts) == sorted(
                [f"fora.rapa.test:{ws_port}", f"pagina.rapa.test:{ws_port}"]
            )
            hosts.clear()
            # Com a guarda, só o domínio do robô passa, e a página fica sem WebRTC.
            guarded = await _run(manifest, tmp_path, chromium_path, EgressPolicy())
            assert guarded == "fora=erro dentro=aberto webrtc=undefined"
            assert hosts == [f"pagina.rapa.test:{ws_port}"]


def test_the_worker_flag_carries_the_networks_and_the_proxy(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--job=j",
            "--result=r",
            "--events=e",
            "--artifacts=a",
            "--guarda-de-saida",
            "--rede-interna-permitida=10.20.0.0/16",
            "--proxy-saida=http://proxy.interno:3128",
        ]
    )
    assert args.egress_guard and args.internal_networks == ["10.20.0.0/16"]
    policy = EgressPolicy.parse(args.internal_networks, args.egress_proxy)
    assert [str(n) for n in policy.allowed_networks] == ["10.20.0.0/16"]
    # A guarda não vem sozinha por causa das redes: sem a opção, o worker recusa.
    base = ["--job", str(tmp_path / "j"), "--result=r", "--events=e", "--artifacts=a"]
    for extra in (["--rede-interna-permitida=10.20.0.0/16"], ["--proxy-saida=http://p:1"]):
        with pytest.raises(SystemExit) as caught:
            main([*base, *extra])
        assert caught.value.code == 2
    with pytest.raises(SystemExit):
        main([*base, "--guarda-de-saida", "--rede-interna-permitida=127.0.0.0/8"])
