"""Códigos de erro estáveis do executor.

Os códigos aparecem em ``RunResult.error_code`` e nos eventos ``step_failed``.
São estáveis: o conserto de robôs e o plano de controle decidem por eles. As
mensagens que acompanham cada erro ficam em pt-BR e nunca contêm valores de
credenciais.
"""

from enum import StrEnum

__all__ = ["ErrorCode", "StepError"]


class ErrorCode(StrEnum):
    #: Nenhum localizador encontrou o elemento.
    ELEMENTO_NAO_ENCONTRADO = "elemento_nao_encontrado"
    #: O localizador encontrou mais de um elemento.
    ELEMENTO_AMBIGUO = "elemento_ambiguo"
    #: Num passo que grava, só um localizador alternativo encontrou o elemento.
    LOCALIZADOR_ALTERNATIVO_EM_GRAVACAO = "localizador_alternativo_em_gravacao"
    DOMINIO_NAO_PERMITIDO = "dominio_nao_permitido"
    CREDENCIAL_DESCONHECIDA = "credencial_desconhecida"
    CREDENCIAL_ORIGEM_INVALIDA = "credencial_origem_invalida"
    SEGREDO_INDISPONIVEL = "segredo_indisponivel"
    ARQUIVO_INDISPONIVEL = "arquivo_indisponivel"
    ENTRADA_INVALIDA = "entrada_invalida"
    TEMPLATE_INVALIDO = "template_invalido"
    LISTA_INVALIDA = "lista_invalida"
    LIMITE_DE_ITENS_EXCEDIDO = "limite_de_itens_excedido"
    VERIFICACAO_FALHOU = "verificacao_falhou"
    POS_CONDICAO_FALHOU = "pos_condicao_falhou"
    DOWNLOAD_FALHOU = "download_falhou"
    ARTEFATO_FALHOU = "artefato_falhou"
    NAVEGACAO_FALHOU = "navegacao_falhou"
    ACAO_FALHOU = "acao_falhou"
    #: O tempo do passo acabou depois de o elemento ter sido encontrado.
    TEMPO_DO_PASSO_ESGOTADO = "tempo_do_passo_esgotado"
    #: O prazo total da execução acabou.
    TEMPO_ESGOTADO = "tempo_esgotado"
    CANCELADA = "cancelada"
    #: Modo de validação: a execução parou antes do primeiro passo que grava.
    PAROU_ANTES_DE_GRAVAR = "parou_antes_de_gravar"
    NAVEGADOR_INDISPONIVEL = "navegador_indisponivel"
    #: O plano de controle não confirmou o aviso de passo irreversível; nada foi enviado.
    SEM_CONFIRMACAO = "sem_confirmacao"
    #: O certificado A1 do robô não pode ser usado agora (registro, revogação, validade ou
    #: autorização do custodiante): o despachante não lançou o executor.
    CERTIFICADO_A1_BLOQUEADO = "certificado_a1_bloqueado"
    ERRO_INTERNO = "erro_interno"
    #: A empresa foi congelada com a execução ainda na fila: ela foi cancelada sem começar.
    EMPRESA_CONGELADA = "empresa_congelada"


class StepError(Exception):
    """Falha de um passo, com código estável e mensagem em pt-BR sem segredos."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, object] = details or {}
        #: Passo mais interno onde a falha aconteceu (preenchido pelo executor).
        self.step_id: str | None = None
        #: Se o executor já registrou a falha (evento e tela).
        self.reported = False
