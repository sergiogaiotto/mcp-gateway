"""Portão de política determinístico.

Roda depois de toda compilação e de todo conserto, antes de publicar. Não usa
modelo de linguagem: só regras. Cada violação traz uma mensagem em pt-BR para a
pessoa que vai aprovar o robô.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rapa.core import templates
from rapa.core.robot import (
    AssertStep,
    ClickStep,
    Condition,
    CredentialKind,
    DownloadStep,
    EqualsCondition,
    ExtractTableStep,
    ExtractTextStep,
    FieldType,
    FillStep,
    ForEachStep,
    IfStep,
    Locator,
    LocatorBy,
    Manifest,
    NavigateStep,
    SecretValue,
    SelectStep,
    Step,
    Target,
    TextContainsCondition,
    TextValue,
    UploadStep,
    UrlContainsCondition,
    VisibleCondition,
    WaitForStep,
    child_blocks,
)

__all__ = [
    "MAX_NESTING",
    "RAW_SELECTORS",
    "RESERVED_NAMES",
    "TEST_TLD",
    "Violation",
    "check",
    "first_write_step",
    "iter_targets",
]

MAX_NESTING = 3
#: Domínio reservado para testes (RFC 2606); só ele aceita http sem TLS.
TEST_TLD = ".test"
#: Estratégias cujo valor vira seletor sem escape; não aceitam valores dinâmicos.
RAW_SELECTORS = frozenset({LocatorBy.CSS, LocatorBy.XPATH})
#: Nomes que nenhum robô pode usar: ``busca`` é a rota fixa ``GET /v1/catalogo/busca``
#: e os outros são as ferramentas fixas do MCP (e a operação ``consultar_execucao`` do
#: OpenAPI dos robôs). A regra vale na criação de versões, não na leitura do manifesto,
#: para versões já guardadas continuarem abrindo.
RESERVED_NAMES = frozenset({"busca", "buscar_robo", "executar_robo", "consultar_execucao"})


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    message: str
    step_id: str | None = None


@dataclass
class _VarInfo:
    kind: str  # "text", "table" ou "file"
    columns: list[str] | None = None


@dataclass
class _Checker:
    manifest: Manifest
    violations: list[Violation] = field(default_factory=list)
    defined: dict[str, _VarInfo] = field(default_factory=dict)
    seen_ids: set[str] = field(default_factory=set)

    def add(self, code: str, message: str, step_id: str | None = None) -> None:
        self.violations.append(Violation(code, message, step_id))

    # -- templates -----------------------------------------------------------

    def check_template(
        self,
        template: str,
        step_id: str | None,
        item_fields: set[str] | None,
        where: str,
    ) -> None:
        try:
            refs = templates.references(template)
        except templates.TemplateError as exc:
            self.add("template_invalido", f"{where}: {exc}", step_id)
            return
        inputs = {f.name: f for f in self.manifest.inputs}
        for ref in refs:
            if ref.scope is templates.Scope.INPUTS:
                spec = inputs.get(ref.name)
                if spec is None:
                    self.add(
                        "entrada_desconhecida",
                        f"{where}: a entrada '{ref.name}' não foi declarada",
                        step_id,
                    )
                elif spec.type in (FieldType.LIST, FieldType.FILE):
                    self.add(
                        "entrada_tipo_invalido",
                        f"{where}: a entrada '{ref.name}' é {spec.type.value} e não pode ser digitada",
                        step_id,
                    )
            elif ref.scope is templates.Scope.VARS:
                info = self.defined.get(ref.name)
                if info is None:
                    self.add(
                        "variavel_desconhecida",
                        f"{where}: a variável '{ref.name}' é usada antes de ser extraída",
                        step_id,
                    )
                elif info.kind != "text":
                    self.add(
                        "variavel_tipo_invalido",
                        f"{where}: a variável '{ref.name}' guarda {info.kind} e não pode ser digitada",
                        step_id,
                    )
            elif item_fields is None:
                self.add(
                    "item_fora_de_repeticao",
                    f"{where}: 'item.{ref.name}' fora de uma repetição",
                    step_id,
                )
            elif item_fields and ref.name not in item_fields:
                self.add(
                    "campo_de_item_desconhecido",
                    f"{where}: o item não tem o campo '{ref.name}'",
                    step_id,
                )

    def check_locator(
        self, locator: Locator, step_id: str, item_fields: set[str] | None, where: str
    ) -> None:
        self.check_template(locator.value, step_id, item_fields, where)
        if locator.name is not None:
            self.check_template(locator.name, step_id, item_fields, where)
        if locator.by in RAW_SELECTORS:
            try:
                dynamic = templates.has_references(locator.value)
            except templates.TemplateError:
                return  # já registrado como template inválido
            if dynamic:
                # Um valor entra cru no seletor: aspas, colchetes ou vírgulas nele
                # escolheriam outro elemento. Papel, rótulo, texto e test_id escapam.
                self.add(
                    "seletor_dinamico",
                    f"{where}: localizadores {locator.by.value} não podem usar entradas, "
                    "variáveis nem itens; use papel, rótulo, texto ou test_id",
                    step_id,
                )

    def check_target(self, target: Target, step_id: str, item_fields: set[str] | None) -> None:
        where = f"alvo '{target.intent}'"
        for locator in target.locators:
            self.check_locator(locator, step_id, item_fields, where)
        if target.within is not None:
            self.check_locator(target.within, step_id, item_fields, where)
        if target.within_has_text is not None:
            self.check_template(target.within_has_text, step_id, item_fields, where)

    def check_condition(
        self, condition: Condition, step_id: str | None, item_fields: set[str] | None
    ) -> None:
        match condition:
            case VisibleCondition(target=target):
                self.check_target(target, step_id or "", item_fields)
            case TextContainsCondition(target=target, text=text):
                if target is not None:
                    self.check_target(target, step_id or "", item_fields)
                self.check_template(text, step_id, item_fields, "condição")
            case UrlContainsCondition(value=value):
                self.check_template(value, step_id, item_fields, "condição")
            case EqualsCondition(left=left, right=right):
                self.check_template(left, step_id, item_fields, "condição")
                self.check_template(right, step_id, item_fields, "condição")

    # -- passos --------------------------------------------------------------

    def check_navigate(self, step: NavigateStep, item_fields: set[str] | None) -> None:
        self.check_template(step.url, step.id, item_fields, "endereço")
        try:
            parts = templates.parse(step.url)
        except templates.TemplateError:
            return
        probe = "".join(part if isinstance(part, str) else "x" for part in parts)
        literal_prefix = parts[0] if parts and isinstance(parts[0], str) else ""
        split = urlsplit(probe)
        host = (split.hostname or "").lower()
        prefix_split = urlsplit(literal_prefix)
        # Domínio, porta e usuário vêm inteiros do trecho literal do começo.
        if (
            not host
            or (prefix_split.hostname or "").lower() != host
            or prefix_split.netloc.lower() != split.netloc.lower()
        ):
            self.add(
                "dominio_dinamico",
                "o domínio do endereço precisa ser fixo; só o caminho pode usar valores",
                step.id,
            )
            return
        if host not in self.manifest.allowed_domains:
            self.add(
                "dominio_nao_permitido",
                f"o endereço vai para '{host}', que não está nos domínios permitidos",
                step.id,
            )
        if split.scheme != "https" and not (split.scheme == "http" and host.endswith(TEST_TLD)):
            self.add("endereco_sem_tls", "o endereço precisa usar https", step.id)
        if split.username or split.password:
            self.add(
                "endereco_com_credencial", "o endereço não pode conter usuário ou senha", step.id
            )

    def check_fill(self, step: FillStep, item_fields: set[str] | None) -> None:
        self.check_target(step.target, step.id, item_fields)
        value = step.value
        if isinstance(value, TextValue):
            self.check_template(value.template, step.id, item_fields, "valor")
            return
        self.check_secret(value, step.id)

    def check_secret(self, value: SecretValue, step_id: str) -> None:
        credentials = {c.name: c for c in self.manifest.credentials}
        credential = credentials.get(value.credential)
        if credential is None:
            self.add(
                "credencial_desconhecida",
                f"a credencial '{value.credential}' não foi declarada",
                step_id,
            )
            return
        allowed = {
            CredentialKind.PASSWORD: {"username", "password"},
            CredentialKind.TOTP: {"totp"},
            CredentialKind.PFX_A1: set(),
        }[credential.kind]
        if value.field not in allowed:
            self.add(
                "credencial_campo_invalido",
                f"a credencial '{credential.name}' ({credential.kind.value}) não tem o campo '{value.field}'",
                step_id,
            )

    def check_step(self, step: Step, depth: int, item_fields: set[str] | None) -> None:
        if step.id in self.seen_ids:
            self.add(
                "passo_repetido", f"o id de passo '{step.id}' aparece mais de uma vez", step.id
            )
        self.seen_ids.add(step.id)
        if depth > MAX_NESTING:
            self.add(
                "aninhamento_excessivo",
                f"mais de {MAX_NESTING} níveis de repetição/condição",
                step.id,
            )

        match step:
            case NavigateStep():
                self.check_navigate(step, item_fields)
            case FillStep():
                self.check_fill(step, item_fields)
            case SelectStep():
                self.check_target(step.target, step.id, item_fields)
                self.check_template(step.option, step.id, item_fields, "opção")
            case (
                ClickStep() | UploadStep() | ExtractTextStep() | ExtractTableStep() | DownloadStep()
            ):
                self.check_target(step.target, step.id, item_fields)
            case WaitForStep():
                if step.target is not None:
                    self.check_target(step.target, step.id, item_fields)
                if step.url_contains is not None:
                    self.check_template(
                        step.url_contains, step.id, item_fields, "endereço esperado"
                    )
            case AssertStep():
                self.check_condition(step.condition, step.id, item_fields)
            case IfStep():
                self.check_condition(step.condition, step.id, item_fields)
            case _:
                target = getattr(step, "target", None)
                if isinstance(target, Target):
                    self.check_target(target, step.id, item_fields)

        if isinstance(step, UploadStep):
            spec = next((f for f in self.manifest.inputs if f.name == step.file_input), None)
            if spec is None or spec.type is not FieldType.FILE:
                self.add(
                    "entrada_arquivo_desconhecida",
                    f"'{step.file_input}' precisa ser uma entrada do tipo arquivo",
                    step.id,
                )
        if isinstance(step, DownloadStep):
            self.check_template(step.save_as, step.id, item_fields, "nome do arquivo")
            self.defined[step.into] = _VarInfo("file")
        elif isinstance(step, ExtractTextStep):
            self.defined[step.into] = _VarInfo("text")
        elif isinstance(step, ExtractTableStep):
            self.defined[step.into] = _VarInfo("table", step.columns)

        if isinstance(step, ForEachStep):
            fields_ = self.for_each_fields(step)
            for child in step.steps:
                self.check_step(child, depth + 1, fields_)
        else:
            for block in child_blocks(step):
                for child in block:
                    self.check_step(child, depth + 1, item_fields)

    def for_each_fields(self, step: ForEachStep) -> set[str]:
        scope, name = step.items.split(".", 1)
        if scope == "inputs":
            spec = next((f for f in self.manifest.inputs if f.name == name), None)
            if spec is None or spec.type is not FieldType.LIST or not spec.item_fields:
                self.add(
                    "lista_desconhecida",
                    f"'{step.items}' precisa ser uma entrada do tipo lista",
                    step.id,
                )
                return set()
            return {f.name for f in spec.item_fields}
        info = self.defined.get(name)
        if info is None or info.kind != "table":
            self.add(
                "lista_desconhecida",
                f"'{step.items}' precisa vir de uma extração de tabela anterior",
                step.id,
            )
            return set()
        if not info.columns:
            self.add(
                "tabela_sem_colunas",
                f"a tabela '{name}' precisa declarar as colunas para ser percorrida",
                step.id,
            )
            return set()
        return set(info.columns)

    # -- manifesto -----------------------------------------------------------

    def run(self) -> list[Violation]:
        if self.manifest.name in RESERVED_NAMES:
            self.add(
                "nome_reservado",
                f"o nome '{self.manifest.name}' é reservado pelo RaPA (rotas e ferramentas "
                "fixas); escolha outro nome para o robô",
            )
        for credential in self.manifest.credentials:
            host = urlsplit(credential.origin).hostname or ""
            if host not in self.manifest.allowed_domains:
                self.add(
                    "credencial_fora_dos_dominios",
                    f"a credencial '{credential.name}' é de '{host}', fora dos domínios permitidos",
                )
        for step in self.manifest.steps:
            self.check_step(step, 1, None)
        for condition in self.manifest.postconditions:
            self.check_condition(condition, None, None)
        expected_kind = {FieldType.FILE: "file", FieldType.LIST: "table"}
        for output in self.manifest.outputs:
            name = output.source.split(".", 1)[1]
            info = self.defined.get(name)
            kind = expected_kind.get(output.type, "text")
            if info is None:
                self.add(
                    "saida_sem_origem",
                    f"a saída '{output.name}' vem de '{output.source}', que nunca é preenchida",
                )
            elif info.kind != kind:
                self.add(
                    "saida_tipo_invalido",
                    f"a saída '{output.name}' é {output.type.value}, mas '{output.source}' guarda {info.kind}",
                )
        return self.violations


def check(manifest: Manifest) -> list[Violation]:
    """Verifica um manifesto já validado estruturalmente. Lista vazia = aprovado."""
    return _Checker(manifest).run()


def first_write_step(manifest: Manifest) -> str | None:
    """Id do primeiro passo que grava algo no sistema de destino, se houver."""
    for step, _ in manifest.iter_steps():
        if step.effect.value != "read":
            return step.id
    return None


def iter_targets(steps: Iterable[Step]) -> Iterator[Target]:
    """Todos os alvos de uma lista de passos (útil para o conserto)."""
    for step in steps:
        target = getattr(step, "target", None)
        if isinstance(target, Target):
            yield target
        for block in child_blocks(step):
            yield from iter_targets(block)
