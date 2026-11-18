"""Portal de teste em pt-BR.

Imita padrões comuns de portais brasileiros: login com senha e código TOTP,
tabela paginada com downloads, formulário com CPF e máscara, passo
irreversível ("Emitir"), envio de arquivo, conteúdo que aparece com atraso e um
iframe. A variante ``v2`` muda rótulos, ordem de colunas e ids, para testar o
conserto de robôs quando o site muda.

Nunca use este portal fora de testes: as credenciais são fixas.
"""

import html
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Literal

import pyotp
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

__all__ = ["DEFAULT_PASSWORD", "DEFAULT_TOTP_SECRET", "DEFAULT_USER", "PortalState", "create_app"]

DEFAULT_USER = "ana.souza"
DEFAULT_PASSWORD = "Senha-Forte-123"
DEFAULT_TOTP_SECRET = "KVKFKRCPNZQUYMLXOVYDSQKJKZDTSRLD"
SESSION_COOKIE = "sessao_portal"

type Variant = Literal["v1", "v2"]

_CLIENTES = [
    "Padaria Pão Quente Ltda",
    "Construtora Alvorada S.A.",
    "Mercado São João",
    "Clínica Bem Estar",
    "Transportes Rápido Sul",
]
_SITUACOES = ["Autorizada", "Cancelada", "Autorizada", "Autorizada", "Denegada"]
_UFS = ["AC", "BA", "DF", "MG", "PR", "RJ", "RS", "SC", "SP"]
PAGE_SIZE = 10
TOTAL_NOTAS = 25


@dataclass(frozen=True, slots=True)
class Nota:
    numero: str
    cliente: str
    valor: Decimal
    situacao: str

    @property
    def valor_br(self) -> str:
        inteiro, _, centavos = f"{self.valor:.2f}".partition(".")
        grupos = f"{int(inteiro):,}".replace(",", ".")
        return f"{grupos},{centavos}"


def _notas() -> list[Nota]:
    return [
        Nota(
            numero=f"{1000 + i:06d}",
            cliente=_CLIENTES[i % len(_CLIENTES)],
            valor=Decimal(1234 + i * 517) / 100 * 10,
            situacao=_SITUACOES[i % len(_SITUACOES)],
        )
        for i in range(TOTAL_NOTAS)
    ]


def cpf_valido(cpf: str) -> bool:
    """Valida os dígitos verificadores de um CPF."""
    digits = [int(c) for c in cpf if c.isdigit()]
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits[:size], range(size + 1, 1, -1), strict=True))
        check = (total * 10) % 11 % 10
        if check != digits[size]:
            return False
    return True


@dataclass
class PortalState:
    """Estado observável pelos testes (por exemplo, protocolos emitidos)."""

    variant: Variant
    totp_secret: str
    users: dict[str, str]
    sessions: dict[str, str] = field(default_factory=dict)
    pending_otp: dict[str, str] = field(default_factory=dict)
    protocolos: list[dict[str, str]] = field(default_factory=list)
    uploads: list[dict[str, object]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    notas: list[Nota] = field(default_factory=_notas)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>{html.escape(title)} · Portal de Teste</title></head>
<body>
<header><nav aria-label="Principal"><a href="/inicio">Início</a> · <a href="/sair">Sair</a></nav></header>
<main>
<h1>{html.escape(title)}</h1>
{body}
</main>
</body>
</html>"""
    )


def create_app(
    variant: Variant = "v1",
    *,
    totp_secret: str = DEFAULT_TOTP_SECRET,
    users: dict[str, str] | None = None,
) -> FastAPI:
    """Cria o portal. ``app.state.portal`` expõe o estado para os testes."""
    state = PortalState(
        variant=variant,
        totp_secret=totp_secret,
        users=users if users is not None else {DEFAULT_USER: DEFAULT_PASSWORD},
    )
    app = FastAPI(title="Portal de Teste RaPA", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.portal = state
    v2 = variant == "v2"

    def current_user(request: Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE)
        return state.sessions.get(token) if token else None

    def login_required(request: Request) -> RedirectResponse | None:
        if current_user(request) is None:
            return RedirectResponse("/login", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    async def home() -> HTMLResponse:
        return _page("Portal de Teste", '<p><a href="/login">Entrar</a></p>')

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(erro: str | None = None) -> HTMLResponse:
        alert = '<p role="alert">Usuário ou senha incorretos.</p>' if erro else ""
        user_label = "Login" if v2 else "Usuário"
        return _page(
            "Entrar",
            f"""{alert}
<form method="post" action="/login">
  <label for="{"login" if v2 else "usuario"}">{user_label}</label>
  <input id="{"login" if v2 else "usuario"}" name="usuario" autocomplete="username" required>
  <label for="senha">Senha</label>
  <input id="senha" name="senha" type="password" autocomplete="current-password" required>
  <button type="submit">{"Acessar" if v2 else "Entrar"}</button>
</form>""",
        )

    @app.post("/login")
    async def login(
        usuario: Annotated[str, Form()], senha: Annotated[str, Form()]
    ) -> RedirectResponse:
        expected = state.users.get(usuario)
        if expected is None or not secrets.compare_digest(expected, senha):
            return RedirectResponse("/login?erro=1", status_code=303)
        token = secrets.token_urlsafe(24)
        state.pending_otp[token] = usuario
        response = RedirectResponse("/login/codigo", status_code=303)
        response.set_cookie("otp_pendente", token, httponly=True, samesite="lax")
        return response

    @app.get("/login/codigo", response_class=HTMLResponse)
    async def otp_form(erro: str | None = None) -> HTMLResponse:
        alert = '<p role="alert">Código inválido.</p>' if erro else ""
        return _page(
            "Verificação em duas etapas",
            f"""{alert}
<form method="post" action="/login/codigo">
  <label for="codigo">Código de verificação</label>
  <input id="codigo" name="codigo" inputmode="numeric" autocomplete="one-time-code" maxlength="6" required>
  <button type="submit">Confirmar</button>
</form>""",
        )

    @app.post("/login/codigo")
    async def otp(request: Request, codigo: Annotated[str, Form()]) -> RedirectResponse:
        pending = request.cookies.get("otp_pendente", "")
        usuario = state.pending_otp.get(pending)
        if usuario is None:
            return RedirectResponse("/login", status_code=303)
        if not pyotp.TOTP(state.totp_secret).verify(codigo, valid_window=1):
            return RedirectResponse("/login/codigo?erro=1", status_code=303)
        del state.pending_otp[pending]
        token = secrets.token_urlsafe(24)
        state.sessions[token] = usuario
        response = RedirectResponse("/inicio", status_code=303)
        response.delete_cookie("otp_pendente")
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
        return response

    @app.get("/sair")
    async def logout(request: Request) -> RedirectResponse:
        state.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/inicio", response_class=HTMLResponse)
    async def inicio(request: Request) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        user = html.escape(current_user(request) or "")
        return _page(
            "Início",
            f"""<p>Olá, <span data-testid="usuario-logado">{user}</span>.</p>
<ul>
  <li><a href="/notas">Notas fiscais</a></li>
  <li><a href="/protocolo">{"Emitir documento" if v2 else "Emitir protocolo"}</a></li>
  <li><a href="/arquivos">Enviar arquivo</a></li>
  <li><a href="/lento">Relatório</a></li>
  <li><a href="/quadro">Consulta em quadro</a></li>
</ul>""",
        )

    @app.get("/notas", response_class=HTMLResponse)
    async def notas(request: Request, pagina: int = 1, page: int | None = None) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        current = page if (v2 and page is not None) else pagina
        total_pages = (len(state.notas) + PAGE_SIZE - 1) // PAGE_SIZE
        current = max(1, min(current, total_pages))
        rows = state.notas[(current - 1) * PAGE_SIZE : current * PAGE_SIZE]
        link_text = "Download" if v2 else "Baixar"
        param = "page" if v2 else "pagina"

        def cells(nota: Nota) -> str:
            values = {
                "numero": f"<td>{nota.numero}</td>",
                "cliente": f"<td>{html.escape(nota.cliente)}</td>",
                "valor": f"<td>{nota.valor_br}</td>",
                "situacao": f"<td>{nota.situacao}</td>",
            }
            order = (
                ["cliente", "numero", "situacao", "valor"]
                if v2
                else ["numero", "cliente", "valor", "situacao"]
            )
            link = f'<td><a href="/notas/{nota.numero}/download">{link_text}</a></td>'
            return "".join(values[key] for key in order) + link

        headers = (
            ["Cliente", "Número", "Situação", "Valor (R$)"]
            if v2
            else ["Número", "Cliente", "Valor (R$)", "Situação"]
        )
        head = "".join(f'<th scope="col">{h}</th>' for h in [*headers, "Arquivo"])
        body = "".join(f"<tr>{cells(n)}</tr>" for n in rows)
        nav = []
        if current > 1:
            nav.append(f'<a href="/notas?{param}={current - 1}">Anterior</a>')
        nav.append(f'<span data-testid="pagina-atual">Página {current} de {total_pages}</span>')
        if current < total_pages:
            nav.append(f'<a href="/notas?{param}={current + 1}">Próxima</a>')
        return _page(
            "Notas fiscais",
            f"""<table id="{"tbl-docs" if v2 else "tabela-notas"}">
<caption>Notas fiscais emitidas</caption>
<thead><tr>{head}</tr></thead>
<tbody>{body}</tbody>
</table>
<nav aria-label="Paginação">{" ".join(nav)}</nav>""",
        )

    @app.get("/notas/{numero}/download")
    async def download(request: Request, numero: str) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        nota = next((n for n in state.notas if n.numero == numero), None)
        if nota is None:
            return Response("Nota não encontrada", status_code=404)
        state.downloads.append(numero)
        content = (
            "%PDF-1.4\n% Portal de Teste RaPA\n"
            f"% Nota {nota.numero} - {nota.cliente} - R$ {nota.valor_br}\n%%EOF\n"
        ).encode()
        return Response(
            content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="nota-{nota.numero}.pdf"'},
        )

    @app.get("/protocolo", response_class=HTMLResponse)
    async def protocolo_form(request: Request, erro: str | None = None) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        alert = f'<p role="alert">{html.escape(erro)}</p>' if erro else ""
        ufs = "".join(f"<option>{uf}</option>" for uf in _UFS)
        cpf_label = "CPF do titular" if v2 else "CPF"
        button = "Emitir documento" if v2 else "Emitir"
        return _page(
            "Emitir documento" if v2 else "Emitir protocolo",
            f"""{alert}
<form method="post" action="/protocolo" id="{"frm-doc" if v2 else "form-protocolo"}">
  <label for="nome">Nome completo</label>
  <input id="nome" name="nome" required>
  <label for="cpf">{cpf_label}</label>
  <input id="cpf" name="cpf" inputmode="numeric" maxlength="14" placeholder="000.000.000-00" required>
  <label for="uf">UF</label>
  <select id="uf" name="uf">{ufs}</select>
  <label><input type="checkbox" name="declaro" value="sim"> Declaro que as informações são verdadeiras</label>
  <button type="submit">{button}</button>
</form>
<script>
  const cpf = document.getElementById('cpf');
  cpf.addEventListener('input', () => {{
    const d = cpf.value.replace(/\\D/g, '').slice(0, 11);
    let out = d.slice(0, 3);
    if (d.length > 3) out += '.' + d.slice(3, 6);
    if (d.length > 6) out += '.' + d.slice(6, 9);
    if (d.length > 9) out += '-' + d.slice(9, 11);
    cpf.value = out;
  }});
</script>""",
        )

    @app.post("/protocolo")
    async def protocolo(
        request: Request,
        nome: Annotated[str, Form()],
        cpf: Annotated[str, Form()],
        uf: Annotated[str, Form()],
        declaro: Annotated[str | None, Form()] = None,
    ) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        erro = None
        if not cpf_valido(cpf):
            erro = "CPF inválido."
        elif uf not in _UFS:
            erro = "UF inválida."
        elif declaro != "sim":
            erro = "É preciso confirmar a declaração."
        if erro:
            return RedirectResponse(f"/protocolo?erro={erro}", status_code=303)
        numero = f"PRT-2026-{len(state.protocolos) + 1:06d}"
        state.protocolos.append({"numero": numero, "nome": nome, "cpf": cpf, "uf": uf})
        return _page(
            "Protocolo emitido",
            f"""<p>Protocolo nº <strong data-testid="protocolo">{numero}</strong> emitido para {html.escape(nome)}.</p>
<p><a href="/inicio">Voltar ao início</a></p>""",
        )

    @app.get("/arquivos", response_class=HTMLResponse)
    async def arquivos_form(request: Request) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        return _page(
            "Enviar arquivo",
            """<form method="post" action="/arquivos" enctype="multipart/form-data">
  <label for="arquivo">Arquivo</label>
  <input id="arquivo" name="arquivo" type="file" required>
  <button type="submit">Enviar</button>
</form>""",
        )

    @app.post("/arquivos")
    async def arquivos(request: Request, arquivo: UploadFile) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        content = await arquivo.read()
        name = arquivo.filename or "sem-nome"
        state.uploads.append({"nome": name, "tamanho": len(content)})
        return _page(
            "Arquivo recebido",
            f'<p data-testid="arquivo-recebido">Arquivo recebido: {html.escape(name)} ({len(content)} bytes)</p>',
        )

    @app.get("/lento", response_class=HTMLResponse)
    async def lento(request: Request) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        return _page(
            "Relatório",
            """<p id="status">Processando…</p>
<script>
  setTimeout(() => {
    document.getElementById('status').textContent = 'Relatório pronto';
    const total = document.createElement('p');
    total.setAttribute('data-testid', 'total');
    total.textContent = 'Total: 25 notas';
    document.querySelector('main').appendChild(total);
  }, 1500);
</script>""",
        )

    @app.get("/quadro", response_class=HTMLResponse)
    async def quadro(request: Request) -> Response:
        if (redirect := login_required(request)) is not None:
            return redirect
        return _page(
            "Consulta em quadro",
            '<iframe id="quadro-consulta" title="Consulta" src="/quadro/conteudo" width="600" height="200"></iframe>',
        )

    @app.get("/quadro/conteudo", response_class=HTMLResponse)
    async def quadro_conteudo() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Consulta</title></head>
<body><button type="button" onclick="document.getElementById('r').textContent='Consulta confirmada'">Confirmar consulta</button>
<p id="r" data-testid="resultado-quadro"></p></body></html>"""
        )

    @app.get("/_teste/estado")
    async def estado() -> JSONResponse:
        return JSONResponse(
            {
                "variante": state.variant,
                "protocolos": state.protocolos,
                "uploads": state.uploads,
                "downloads": state.downloads,
            }
        )

    return app


def current_totp(secret: str = DEFAULT_TOTP_SECRET) -> str:
    """Código TOTP atual (para testes)."""
    return pyotp.TOTP(secret).now()
