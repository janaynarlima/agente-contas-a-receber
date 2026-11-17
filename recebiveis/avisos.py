"""Calculo e decisao de avisos de cobranca.

Regras (ver Prompt):
  - os dias de aviso vem do config.yaml (nunca fixos no codigo);
  - data prevista = vencimento + dias, empurrada para o proximo dia util
    quando cair em sabado, domingo ou feriado;
  - D0 nao gera aviso;
  - no maximo UM aviso por boleto por execucao: o mais antigo ainda nao
    tratado (se o mais antigo for encaminhado a revisao, nada mais e enviado
    para o boleto nessa execucao);
  - nunca dois avisos de vencido no mesmo dia;
  - aviso com atraso superior a 2 dias em relacao a data prevista nao e
    enviado: vai para revisao humana;
  - aviso previo (dias < 0) so e valido antes do vencimento;
  - boleto pago, em revisao ou sem vencimento nao recebe aviso;
  - avisos de vencido exigem aprovacao (aprovacao_vencidos) e, enquanto a fase
    de validacao operacional nao termina, o D-7 tambem.

Este modulo decide; nao envia e-mail nem confirma envio.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from recebiveis.config import Config, nome_tipo_aviso
from recebiveis.estado import Estado

ATRASO_MAXIMO_DIAS = 2

ENVIAR = "enviar"
FILA_APROVACAO = "fila_aprovacao"
REVISAO = "revisao"
ADIADO = "adiado"


@dataclass(frozen=True)
class AvisoPrevisto:
    tipo_aviso: str
    dias: int
    data_prevista: date


@dataclass(frozen=True)
class Decisao:
    boleto_id: str
    tipo_aviso: str
    data_prevista: date
    acao: str
    motivo: str = ""
    dias_atraso: int = 0


def eh_dia_util(data: date, feriados: frozenset[date]) -> bool:
    return data.weekday() < 5 and data not in feriados


def proximo_dia_util(data: date, feriados: frozenset[date]) -> date:
    """Retorna a propria data se for dia util; senao, o proximo dia util."""
    while not eh_dia_util(data, feriados):
        data += timedelta(days=1)
    return data


def agenda_avisos(
    vencimento: date, avisos: tuple[int, ...], feriados: frozenset[date]
) -> list[AvisoPrevisto]:
    """Datas previstas (ja ajustadas para dia util), da mais antiga a mais nova."""
    agenda = [
        AvisoPrevisto(
            tipo_aviso=nome_tipo_aviso(dias),
            dias=dias,
            data_prevista=proximo_dia_util(vencimento + timedelta(days=dias), feriados),
        )
        for dias in avisos
    ]
    return sorted(agenda, key=lambda previsto: (previsto.data_prevista, previsto.dias))


def _destino(dias: int, config: Config) -> str:
    if dias > 0:
        return FILA_APROVACAO if config.aprovacao_vencidos else ENVIAR
    return ENVIAR if config.fase_validacao_concluida else FILA_APROVACAO


def decidir_aviso(
    boleto: dict, historico: list[dict], config: Config, hoje: date
) -> Decisao | None:
    """Decide o que fazer com UM boleto hoje. Retorna None se nada a fazer.

    `boleto` e `historico` sao os dicts de Estado.obter_boleto/historico_avisos.
    """
    if boleto["status"] != "aberto" or boleto["requer_revisao"] or not boleto["vencimento"]:
        return None

    vencimento = date.fromisoformat(boleto["vencimento"])
    por_tipo = {aviso["tipo_aviso"]: aviso for aviso in historico}

    # D0 nao gera aviso.
    if hoje == vencimento:
        return None

    # Um aviso aguardando aprovacao (ou aprovado e ainda nao enviado) segura os
    # seguintes: o mais antigo nao enviado tem de sair antes.
    if any(a["status"] in ("aguardando_aprovacao", "aprovado") for a in historico):
        return None

    candidatos = [
        previsto
        for previsto in agenda_avisos(vencimento, config.avisos, config.feriados)
        if previsto.data_prevista <= hoje
        and por_tipo.get(previsto.tipo_aviso, {}).get("status", "pendente") == "pendente"
    ]
    if not candidatos:
        return None

    alvo = candidatos[0]  # o mais antigo ainda nao tratado
    atraso = (hoje - alvo.data_prevista).days
    base = dict(
        boleto_id=boleto["boleto_id"],
        tipo_aviso=alvo.tipo_aviso,
        data_prevista=alvo.data_prevista,
        dias_atraso=atraso,
    )

    if alvo.dias < 0 and hoje >= vencimento:
        return Decisao(**base, acao=REVISAO, motivo="aviso previo perdido: vencimento ja atingido")
    if atraso > ATRASO_MAXIMO_DIAS:
        return Decisao(
            **base, acao=REVISAO, motivo=f"atraso de {atraso} dias acima do limite de {ATRASO_MAXIMO_DIAS}"
        )

    # Nunca dois avisos de vencido no mesmo dia (cobre execucoes repetidas).
    if alvo.dias > 0:
        dia_hoje = hoje.isoformat()
        dias_por_tipo = {nome_tipo_aviso(dias): dias for dias in config.avisos}
        for aviso in historico:
            if (
                aviso["status"] == "enviado"
                and aviso["data_envio"] == dia_hoje
                and dias_por_tipo.get(aviso["tipo_aviso"], 0) > 0
            ):
                return None

    return Decisao(**base, acao=_destino(alvo.dias, config))


def planejar_execucao(estado: Estado, config: Config, hoje: date) -> list[Decisao]:
    """Planeja os avisos de todos os boletos abertos.

    Efeitos no estado: cria os avisos pendentes da agenda (idempotente) e
    encaminha para revisao os que passaram do limite de atraso. Nao confirma
    envios. Respeita max_envios_por_execucao (os excedentes ficam ADIADOS, sem
    alteracao de estado, e voltam na proxima execucao).
    """
    decisoes: list[Decisao] = []

    for boleto in estado.listar_boletos("aberto"):
        if boleto["requer_revisao"] or not boleto["vencimento"]:
            continue
        vencimento = date.fromisoformat(boleto["vencimento"])
        for previsto in agenda_avisos(vencimento, config.avisos, config.feriados):
            estado.registrar_aviso(boleto["boleto_id"], previsto.tipo_aviso, previsto.data_prevista)

        decisao = decidir_aviso(
            boleto, estado.historico_avisos(boleto["boleto_id"]), config, hoje
        )
        if decisao is None:
            continue
        if decisao.acao == REVISAO:
            estado.encaminhar_aviso_para_revisao(
                decisao.boleto_id, decisao.tipo_aviso, decisao.motivo
            )
        decisoes.append(decisao)

    envios = sorted(
        (d for d in decisoes if d.acao in (ENVIAR, FILA_APROVACAO)),
        key=lambda d: (d.data_prevista, d.boleto_id),
    )
    permitidos = {(d.boleto_id, d.tipo_aviso) for d in envios[: config.max_envios_por_execucao]}
    return [
        d
        if d.acao == REVISAO or (d.boleto_id, d.tipo_aviso) in permitidos
        else Decisao(
            d.boleto_id, d.tipo_aviso, d.data_prevista, ADIADO,
            "limite de envios por execucao atingido", d.dias_atraso,
        )
        for d in decisoes
    ]
