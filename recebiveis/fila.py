"""Fila de aprovacao e entrega de avisos.

Fluxo:
  decisao `fila_aprovacao` -> enfileirar(): renderiza o e-mail, grava o .eml em
  fila_aprovacao/ e move o aviso para `aguardando_aprovacao`;
  uma pessoa aprova ou rejeita (comando `aprovar`);
  entregar(): confere de novo o arquivo de entrada, gera o .eml em saida/
  (dry-run) ou envia por SMTP (somente com enviar=True, isto e, --enviar).

O LLM nunca participa: o texto vem exclusivamente dos templates Jinja.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path

from recebiveis.avisos import Decisao
from recebiveis.clientes import Cliente, buscar_cliente
from recebiveis.config import Config
from recebiveis.emailer import (
    ErroEnvio,
    ErroTemplate,
    enviar_smtp,
    gravar_eml,
    montar_mensagem,
    renderizar_aviso,
)
from recebiveis.estado import Estado
from recebiveis.reconciliacao import remover_da_fila
from recebiveis.validador import ErroFebraban, codigo_barras_para_linha

REMETENTE_PADRAO = "remetente-nao-configurado@example.invalid"
_TIPO = re.compile(r"^D([+-]\d+)$")


class ErroPreparo(ValueError):
    """Nao foi possivel montar o e-mail do aviso."""


@dataclass(frozen=True)
class ResultadoFila:
    situacao: str  # enfileirado | revisao | ja_enfileirado
    motivo: str = ""
    arquivo: str | None = None


@dataclass(frozen=True)
class ResultadoEntrega:
    situacao: str  # enviado | dry-run | bloqueado | falha | ja_tratado
    motivo: str = ""
    arquivo: str | None = None


def dias_do_tipo(tipo_aviso: str) -> int:
    correspondencia = _TIPO.match(tipo_aviso)
    if not correspondencia:
        raise ErroPreparo(f"tipo de aviso invalido: {tipo_aviso}")
    return int(correspondencia.group(1))


def id_curto(boleto_id: str) -> str:
    """Identificador curto e UNICO para nomes de arquivo e telas.

    Os ultimos digitos do codigo de barras nao servem: boletos do mesmo
    beneficiario costumam terminar igual (campo livre), o que faria os e-mails
    da fila se sobrescreverem. O hash do identificador nao tem esse problema.
    """
    return hashlib.sha256(boleto_id.encode()).hexdigest()[:8]


def preparar_email(
    config: Config, boleto: dict, cliente: Cliente, tipo_aviso: str, env: dict[str, str]
) -> EmailMessage:
    """Monta a mensagem a partir do cadastro e do template; nada vem do texto do boleto."""
    if not cliente.email_cobranca:
        raise ErroPreparo("cliente sem e-mail cadastrado")
    try:
        valor = Decimal(boleto["valor"]) if boleto.get("valor") else None
    except InvalidOperation as erro:
        raise ErroPreparo("valor do boleto invalido") from erro
    if valor is None:
        raise ErroPreparo("valor do boleto ausente")
    if not boleto.get("vencimento"):
        raise ErroPreparo("vencimento do boleto ausente")
    try:
        numero = codigo_barras_para_linha(boleto["boleto_id"])
    except ErroFebraban as erro:
        raise ErroPreparo("boleto sem codigo de barras valido") from erro

    aviso = renderizar_aviso(
        config.templates,
        dias_do_tipo(tipo_aviso),
        nome_cliente=cliente.nome,
        valor=valor,
        vencimento=date.fromisoformat(boleto["vencimento"]),
        numero_boleto=numero,
    )
    return montar_mensagem(
        aviso,
        remetente=env.get("EMAIL_REMETENTE") or REMETENTE_PADRAO,
        destinatario=cliente.email_cobranca,
        bcc=config.bcc_financeiro,
    )


def enfileirar(
    estado: Estado,
    config: Config,
    decisao: Decisao,
    clientes: dict[str, Cliente],
    hoje: date,
    env: dict[str, str],
) -> ResultadoFila:
    """Coloca um aviso na fila de aprovacao, ou o envia para revisao se faltar dado."""
    boleto = estado.obter_boleto(decisao.boleto_id)
    if boleto is None:
        return ResultadoFila("revisao", "boleto desconhecido")

    def revisao(motivo: str) -> ResultadoFila:
        estado.encaminhar_aviso_para_revisao(decisao.boleto_id, decisao.tipo_aviso, motivo)
        return ResultadoFila("revisao", motivo)

    cliente = buscar_cliente(clientes, boleto["cnpj_pagador"])
    if cliente is None:
        return revisao("CNPJ do pagador nao encontrado no cadastro de clientes")
    try:
        mensagem = preparar_email(config, boleto, cliente, decisao.tipo_aviso, env)
    except (ErroPreparo, ErroTemplate) as erro:
        return revisao(str(erro))

    nome = f"{id_curto(decisao.boleto_id)}_{decisao.tipo_aviso}_{decisao.data_prevista.isoformat()}"
    arquivo = gravar_eml(mensagem, config.fila_aprovacao, nome)
    if not estado.enfileirar_aviso(decisao.boleto_id, decisao.tipo_aviso, str(arquivo), hoje):
        return ResultadoFila("ja_enfileirado", arquivo=str(arquivo))
    return ResultadoFila("enfileirado", arquivo=str(arquivo))


def entregar(
    estado: Estado,
    config: Config,
    boleto_id: str,
    tipo_aviso: str,
    hoje: date,
    clientes: dict[str, Cliente],
    env: dict[str, str],
    *,
    enviar: bool,
    de: str,
    arquivo_presente: Callable[[str], bool],
) -> ResultadoEntrega:
    """Gera o .eml (dry-run) ou envia (enviar=True) um aviso pendente/aprovado.

    Antes de qualquer envio o arquivo de entrada e conferido de novo: se o
    boleto sumiu entre a carga e o envio, NAO envia.
    """
    boleto = estado.obter_boleto(boleto_id)
    aviso = estado.obter_aviso(boleto_id, tipo_aviso)
    if boleto is None or aviso is None or aviso["status"] != de:
        return ResultadoEntrega("ja_tratado", "aviso nao esta mais no estado esperado")
    if boleto["status"] != "aberto" or boleto["requer_revisao"]:
        return ResultadoEntrega("bloqueado", "boleto pago, em revisao ou reaberto")

    cliente = buscar_cliente(clientes, boleto["cnpj_pagador"])
    if cliente is None:
        return ResultadoEntrega("bloqueado", "CNPJ do pagador nao encontrado no cadastro")
    try:
        mensagem = preparar_email(config, boleto, cliente, tipo_aviso, env)
    except (ErroPreparo, ErroTemplate) as erro:
        return ResultadoEntrega("bloqueado", str(erro))

    if enviar and not arquivo_presente(boleto_id):
        return ResultadoEntrega("bloqueado", "arquivo do boleto removido antes do envio")

    if not enviar:
        nome = f"{id_curto(boleto_id)}_{tipo_aviso}_{hoje.isoformat()}"
        return ResultadoEntrega("dry-run", arquivo=str(gravar_eml(mensagem, config.saida, nome)))

    if not env.get("EMAIL_REMETENTE"):
        return ResultadoEntrega("bloqueado", "EMAIL_REMETENTE nao configurado")
    if not estado.confirmar_envio(boleto_id, tipo_aviso, hoje, de=de):
        return ResultadoEntrega("ja_tratado", "outra execucao ja enviou este aviso")
    try:
        enviar_smtp(mensagem, env)
    except ErroEnvio as erro:
        estado.reverter_envio(boleto_id, tipo_aviso, str(erro), para=de)
        return ResultadoEntrega("falha", str(erro))

    remover_da_fila(aviso.get("arquivo_fila"), config.fila_aprovacao)
    return ResultadoEntrega("enviado")
