"""Telas da interface com o ``User`` simulado do NiceGUI e a plataforma em memória."""

import json
from uuid import UUID

import pytest

from rapa.core.contracts import Principal, RunState, RunTrigger, VersionStatus
from rapa.core.robot import Manifest
from rapa.fixtures.robots import emitir_protocolo
from tests.ui.conftest import BASE, HOST, World, login, logout

INPUTS = {"nome": "Maria", "cpf": "529.982.247-25", "uf": "SP"}

pytestmark = pytest.mark.usefixtures("no_error_logs")


async def test_login_required_and_dev_login(world: World) -> None:
    async with world.user() as user:
        await user.open("/robos")
        await user.should_see(marker="pagina-login")
        await user.should_not_see(marker="usuario-logado")
        await user.should_see(marker="perfil-operador")
        user.find(marker="perfil-operador").click()
        # Volta para a página pedida antes do login.
        await user.should_see(marker="lista-robos", retries=50)
        await user.should_see("Perfil operador")
        await user.should_see("Empresa A")
        cookies = {cookie.name for cookie in user.http_client.cookies.jar}
        assert cookies == {"rapa_sessao", "rapa_vinculo"}
        assert len(world.store) == 1

        await logout(user)
        await user.should_see(marker="saiu")
        assert len(world.store) == 0
        await user.open("/catalogo")
        await user.should_see(marker="pagina-login")


async def test_catalog_shows_only_own_company(world: World) -> None:
    async with world.user() as user:
        await login(user, "operador")
        await user.should_see(marker="robo-emitir_protocolo")
        await user.should_not_see(marker="robo-baixar_notas")
        await user.open("/catalogo?q=notas")
        await user.should_see(marker="catalogo-vazio")

        await logout(user)
        await login(user, "outra")
        await user.should_see(marker="robo-baixar_notas")
        await user.should_not_see(marker="robo-emitir_protocolo")
        await user.should_see("Empresa B")
        await user.open("/executar/emitir_protocolo")
        await user.should_see("Robô não encontrado ou não publicado.")
        await user.open(f"/robos/{world.robot_ids['emitir_protocolo']}")
        await user.should_see("Robô não encontrado.")


def _second_version() -> str:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    for step in data["steps"]:
        if step["id"] == "digitar_nome":
            step["title"] = "Digitar o nome completo"
    data["steps"].append(
        {
            "kind": "wait_for",
            "id": "aguardar_recibo",
            "title": "Aguardar o recibo",
            "target": {
                "intent": "texto Protocolo emitido",
                "locators": [{"by": "text", "value": "Protocolo emitido"}],
            },
        }
    )
    return json.dumps(data, ensure_ascii=False)


async def test_creator_cannot_publish_own_version(world: World) -> None:
    robot_id = world.robot_ids["emitir_protocolo"]
    async with world.user() as user:
        await login(user, "criadora", "/robos")
        await user.should_see(marker="nova-versao")
        user.find(marker="manifesto-json").type(_second_version())
        user.find(marker="criar-versao").click()
        await user.should_see(marker="aprovacao", retries=50)
        await user.should_see("Mudanças em relação à versão 1 (publicada)")
        await user.should_see(marker="passo-novo-aguardar_recibo")
        await user.should_see(marker="passo-alterado-digitar_nome")
        await user.should_see(marker="passo-digitar_senha")
        await user.should_see("valor do cofre: portal, senha", retries=5)

        user.find(marker="validar").click()
        await user.should_see(marker="motivo-publicar", retries=50)
        await user.should_see("Quem criou a versão não pode aprová-la", retries=5)
        await user.should_not_see(marker="publicar")
        info = await world.platform.get_version(world.principals["criadora"], robot_id, 2)
        assert info is not None and info.status is VersionStatus.VALIDATED

        await logout(user)
        await login(user, "aprovador", f"/robos/{robot_id}?versao=2")
        await user.should_see(marker="publicar")
        user.find(marker="publicar").click()
        await user.should_see("Versão 2 publicada.", retries=50)
        info = await world.platform.get_version(world.principals["aprovador"], robot_id, 2)
        assert info is not None
        assert info.status is VersionStatus.PUBLISHED
        assert info.approved_by == "aprovador@a"


async def test_start_run_from_generated_form(world: World) -> None:
    async with world.user() as user:
        await login(user, "operador")
        user.find(marker="executar-emitir_protocolo").click()
        await user.should_see(marker="formulario-entradas", retries=50)
        user.find(marker="campo-nome").type("Maria da Silva")
        user.find(marker="campo-cpf").type("529.982.247-00")
        user.find(marker="campo-uf").type("SP")
        user.find(marker="confirmar-irreversivel").click()
        user.find(marker="executar").click()
        await user.should_see("Corrija os campos destacados.", retries=20)
        (cpf,) = user.find(marker="campo-cpf").elements
        assert getattr(cpf, "error", None) == "CPF inválido: confira os dígitos."
        assert world.platform.runs == {}

        user.find(marker="campo-cpf").clear().type("52998224725")
        user.find(marker="executar").click()
        await user.should_see("Na fila", retries=50)
        (run,) = world.platform.runs.values()
        assert run.inputs == {"nome": "Maria da Silva", "cpf": "529.982.247-25", "uf": "SP"}
        assert run.status.trigger.value == "manual"

        world.platform.set_run_state(
            run.status.run_id, RunState.SUCCEEDED, outputs={"protocolo": "PRT-2026-0001"}
        )
        await user.should_see("Concluída", retries=50)
        await user.should_see("protocolo: PRT-2026-0001", retries=20)

        await user.open("/execucoes")
        await user.should_see(marker=f"linha-{run.status.run_id}")
        user.find(marker=f"detalhes-{run.status.run_id}").click()
        await user.should_see(marker="numero-execucao", retries=50)
        await user.should_see("protocolo: PRT-2026-0001")


async def test_old_live_view_address_leads_to_gravar_and_unknown_run(world: World) -> None:
    async with world.user() as user:
        await login(user, "operador", "/ao-vivo")
        await user.should_see(marker="sem-permissao-gravar", retries=50)
        await user.open("/execucoes/nao-e-um-numero")
        await user.should_see("Execução não encontrada.")


async def test_policy_violations_block_validation(world: World) -> None:
    data = emitir_protocolo(BASE, HOST).model_dump(mode="json")
    data["name"] = "robo_com_problema"
    data["steps"][0]["url"] = "http://outro.rapa.test/login"
    info = await world.platform.create_version(
        world.principals["criadora"], world.area_a, Manifest.model_validate(data)
    )
    async with world.user() as user:
        await login(user, "criadora", f"/robos/{info.robot_id}")
        await user.should_see(marker="violacoes")
        await user.should_see(
            "• o endereço vai para 'outro.rapa.test', que não está nos domínios permitidos"
        )
        await user.should_see(marker="motivo-validar")
        await user.should_not_see(marker="validar")
        await user.should_see("Primeira versão: não há versão anterior para comparar.")


async def test_viewer_without_roles_sees_no_actions(world: World) -> None:
    robot_id = world.robot_ids["emitir_protocolo"]
    async with world.user() as user:
        await login(user, "operador", "/robos")
        await user.should_see(marker="robo-emitir_protocolo")
        await user.should_not_see(marker="nova-versao")
        await user.open(f"/robos/{robot_id}")
        await user.should_see(marker="passos")
        await user.should_not_see(marker="validar")
        await user.should_not_see(marker="publicar")


async def test_failed_and_indeterminate_runs_in_portuguese(world: World) -> None:
    operator = world.principals["operador"]
    failed = await world.platform.start(operator, "emitir_protocolo", INPUTS, RunTrigger.MANUAL)
    unknown = await world.platform.start(operator, "emitir_protocolo", INPUTS, RunTrigger.MCP)
    world.platform.set_run_state(
        failed.run_id,
        RunState.FAILED,
        error_code="cpf_recusado",
        error_message="O portal recusou o CPF informado.",
    )
    world.platform.set_run_state(unknown.run_id, RunState.INDETERMINATE)
    async with world.user() as user:
        await login(user, "operador", f"/execucoes/{failed.run_id}")
        await user.should_see("Falhou")
        await user.should_see("O portal recusou o CPF informado.")
        await user.should_see("Código: cpf_recusado")
        await user.should_see("Interface")
        await user.open(f"/execucoes/{unknown.run_id}")
        await user.should_see("Indeterminada")
        await user.should_see(marker="indeterminada")
        await user.should_see("MCP")

        await logout(user)
        await login(user, "outra", f"/execucoes/{failed.run_id}")
        await user.should_see("Execução não encontrada.")


async def test_list_outputs_render_as_table(world: World) -> None:
    other = world.principals["outra"]
    run = await world.platform.start(other, "baixar_notas", {}, RunTrigger.MANUAL)
    rows = [
        {"numero": "001000", "cliente": "Padaria Pão Quente Ltda", "valor": "123,40"},
        {"numero": "001001", "cliente": "Mercado São João", "valor": "175,10"},
    ]
    world.platform.set_run_state(run.run_id, RunState.SUCCEEDED, outputs={"notas": rows})
    async with world.user() as user:
        await login(user, "outra", f"/execucoes/{run.run_id}")
        await user.should_see("Concluída")
        await user.should_see("Padaria Pão Quente Ltda")
        await user.should_see("001001")


async def test_run_without_inputs_from_catalog(world: World) -> None:
    async with world.user() as user:
        await login(user, "outra")
        user.find(marker="executar-baixar_notas").click()
        await user.should_see("Este robô não tem entradas.", retries=50)
        user.find(marker="executar").click()
        await user.should_see("Na fila", retries=50)
    (run,) = world.platform.runs.values()
    assert run.inputs == {} and run.company_id == world.company_b


async def test_expired_session_sends_back_to_login(world: World) -> None:
    async with world.user() as user:
        await login(user, "operador", "/executar/emitir_protocolo")
        await user.should_see(marker="formulario-entradas")
        for key in list(world.store._sessions):
            await world.store.delete(key)
        user.find(marker="campo-nome").type("Maria")
        user.find(marker="campo-cpf").type("529.982.247-25")
        user.find(marker="campo-uf").type("SP")
        user.find(marker="confirmar-irreversivel").click()
        user.find(marker="executar").click()
        await user.should_see(marker="pagina-login", retries=50)
        await user.should_see("Sua sessão expirou. Entre de novo.")
    assert world.platform.runs == {}


async def test_not_found_page_and_security_headers(world: World) -> None:
    async with world.user() as user:
        response = await user.http_client.get("/nao-existe")
        assert response.status_code == 404
        assert "Página não encontrada" in response.text
        login_page = await user.http_client.get("/entrar", follow_redirects=True)
        assert login_page.status_code == 200
        assert login_page.headers["x-frame-options"] == "DENY"
        assert login_page.headers["x-content-type-options"] == "nosniff"


class _Accounts:
    """Contas como o banco as guarda; o teste desativa e derruba o banco."""

    def __init__(self, world: World) -> None:
        self.by_subject = {p.subject: p for p in world.principals.values()}
        self.down = False

    async def resolve(self, subject: str, company_id: UUID) -> Principal | None:
        if self.down:
            raise ConnectionError("banco fora do ar")
        principal = self.by_subject.get(subject)
        return principal if principal is not None and principal.company_id == company_id else None


async def test_deactivated_account_loses_the_open_session(world: World) -> None:
    accounts = _Accounts(world)
    world.resolver = accounts
    robot_id = world.robot_ids["emitir_protocolo"]
    async with world.user() as user:
        await login(user, "aprovador", f"/robos/{robot_id}")
        await user.should_see(marker="passos")
        # O administrador desativa a conta com a página aberta.
        del accounts.by_subject["aprovador@a"]
        await user.open(f"/robos/{robot_id}")
        await user.should_see(marker="pagina-login")
        await user.should_not_see(marker="usuario-logado")
    assert len(world.store) == 0


async def test_unavailable_database_never_opens_page_with_old_roles(world: World) -> None:
    accounts = _Accounts(world)
    world.resolver = accounts
    async with world.user() as user:
        await login(user, "operador", "/catalogo")
        accounts.down = True
        await user.open("/catalogo")
        await user.should_see(marker="erro")
        await user.should_see("Não foi possível conferir sua sessão agora")
        await user.should_not_see(marker="usuario-logado")
        await user.should_not_see(marker="robo-emitir_protocolo")
        # A sessão continua: com o banco de volta, a página abre normalmente.
        accounts.down = False
        await user.open("/catalogo")
        await user.should_see(marker="usuario-logado")
    assert len(world.store) == 1
