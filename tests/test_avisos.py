"""Testes do calculo de avisos. Dados sinteticos."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from recebiveis.avisos import (
    ADIADO,
    ENVIAR,
    FILA_APROVACAO,
    REVISAO,
    agenda_avisos,
    decidir_aviso,
    eh_dia_util,
    planejar_execucao,
    proximo_dia_util,
)
from recebiveis.config import Config
from recebiveis.estado import Estado

ID_A = "23791" + "1" * 39
ID_B = "23791" + "2" * 39
SEM_FERIADOS: frozenset[date] = frozenset()

CONFIG = Config(
    entrada=Path("entrada"),
    idioma_ocr="por",
    avisos=(-7, 1, 3),
    confianca_minima=0.8,
    aprovacao_vencidos=True,
    fase_validacao_concluida=False,
    max_percentual_baixas=30,
    max_envios_por_execucao=50,
    feriados=SEM_FERIADOS,
    banco=Path("estado.db"),
)

# Calendario de novembro/2026: 16=seg 17=ter 18=qua 19=qui 20=sex 21=sab 22=dom 23=seg


def _boleto(vencimento: date | None, status="aberto", requer_revisao=0, boleto_id=ID_A) -> dict:
    return {
        "boleto_id": boleto_id,
        "status": status,
        "requer_revisao": requer_revisao,
        "vencimento": vencimento.isoformat() if vencimento else None,
    }


def _aviso(tipo, status="pendente", data_envio=None) -> dict:
    return {"tipo_aviso": tipo, "status": status, "data_envio": data_envio}


# --- dia util -----------------------------------------------------------------


def test_dia_util_e_empurrado_em_fim_de_semana_e_feriado():
    assert proximo_dia_util(date(2026, 11, 20), SEM_FERIADOS) == date(2026, 11, 20)  # sexta
    assert proximo_dia_util(date(2026, 11, 21), SEM_FERIADOS) == date(2026, 11, 23)  # sabado
    assert proximo_dia_util(date(2026, 11, 22), SEM_FERIADOS) == date(2026, 11, 23)  # domingo
    assert proximo_dia_util(date(2026, 12, 25), frozenset({date(2026, 12, 25)})) == date(2026, 12, 28)


def test_feriado_em_dia_util_adjacente_a_fim_de_semana():
    feriados = frozenset({date(2026, 11, 23)})  # segunda feriado depois do fim de semana

    assert proximo_dia_util(date(2026, 11, 21), feriados) == date(2026, 11, 24)
    assert not eh_dia_util(date(2026, 11, 23), feriados)


def test_agenda_usa_os_dias_do_config_e_ajusta_dias_nao_uteis():
    agenda = agenda_avisos(date(2026, 11, 20), (-7, 1, 3), SEM_FERIADOS)  # vence sexta

    assert [(a.tipo_aviso, a.data_prevista) for a in agenda] == [
        ("D-7", date(2026, 11, 13)),
        ("D+1", date(2026, 11, 23)),  # sabado -> segunda
        ("D+3", date(2026, 11, 23)),
    ]


def test_vencimento_no_sabado_e_no_domingo():
    sabado = agenda_avisos(date(2026, 11, 21), (-7, 1, 3), SEM_FERIADOS)
    domingo = agenda_avisos(date(2026, 11, 22), (-7, 1, 3), SEM_FERIADOS)

    assert [a.data_prevista for a in sabado] == [date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 24)]
    assert [a.data_prevista for a in domingo] == [date(2026, 11, 16), date(2026, 11, 23), date(2026, 11, 25)]


def test_vencimento_em_feriado_empurra_o_aviso_seguinte():
    feriados = frozenset({date(2026, 12, 25)})

    agenda = agenda_avisos(date(2026, 12, 24), (1, 3), feriados)  # D+1 = feriado, D+3 = domingo

    assert [a.data_prevista for a in agenda] == [date(2026, 12, 28), date(2026, 12, 28)]


def test_dias_configurados_diferentes_geram_outra_agenda():
    agenda = agenda_avisos(date(2026, 11, 16), (-2, 5), SEM_FERIADOS)

    assert [a.tipo_aviso for a in agenda] == ["D-2", "D+5"]


# --- decisao -------------------------------------------------------------------


def test_d0_nao_gera_aviso():
    assert decidir_aviso(_boleto(date(2026, 11, 20)), [], CONFIG, date(2026, 11, 20)) is None


def test_nada_antes_da_data_prevista():
    assert decidir_aviso(_boleto(date(2026, 11, 20)), [], CONFIG, date(2026, 11, 12)) is None


def test_d_menos_7_vai_para_aprovacao_ate_a_fase_de_validacao_terminar():
    boleto = _boleto(date(2026, 11, 20))

    antes = decidir_aviso(boleto, [], CONFIG, date(2026, 11, 13))
    depois = decidir_aviso(boleto, [], replace(CONFIG, fase_validacao_concluida=True), date(2026, 11, 13))

    assert (antes.tipo_aviso, antes.acao) == ("D-7", FILA_APROVACAO)
    assert (depois.tipo_aviso, depois.acao) == ("D-7", ENVIAR)


def test_aviso_de_vencido_exige_aprovacao_por_padrao():
    boleto = _boleto(date(2026, 11, 16))
    historico = [_aviso("D-7", "enviado", "2026-11-09")]

    padrao = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 17))
    liberado = decidir_aviso(boleto, historico, replace(CONFIG, aprovacao_vencidos=False), date(2026, 11, 17))

    assert (padrao.tipo_aviso, padrao.acao) == ("D+1", FILA_APROVACAO)
    assert liberado.acao == ENVIAR


def test_d3_sem_execucao_previa_do_d1_avalia_d1_primeiro_e_nao_envia_d3():
    boleto = _boleto(date(2026, 11, 16))  # D+1 = 17, D+3 = 19
    historico = [_aviso("D-7", "enviado", "2026-11-09")]

    decisao = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 19))

    assert decisao.tipo_aviso == "D+1"
    assert decisao.dias_atraso == 2
    assert decisao.acao == FILA_APROVACAO  # 2 dias de atraso ainda e permitido


def test_atraso_acima_de_2_dias_nao_envia_e_vai_para_revisao():
    boleto = _boleto(date(2026, 11, 16))
    historico = [_aviso("D-7", "enviado", "2026-11-09")]

    decisao = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 20))

    assert (decisao.tipo_aviso, decisao.acao, decisao.dias_atraso) == ("D+1", REVISAO, 3)
    assert "atraso" in decisao.motivo


def test_atraso_e_medido_contra_a_data_ja_ajustada_para_dia_util():
    boleto = _boleto(date(2026, 11, 20))  # D+1 cai no sabado -> prevista na segunda 23
    historico = [_aviso("D-7", "enviado", "2026-11-13")]

    na_quarta = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 25))  # atraso 2
    na_quinta = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 26))  # atraso 3

    assert na_quarta.acao == FILA_APROVACAO
    assert na_quinta.acao == REVISAO


def test_execucao_repetida_no_mesmo_dia_nao_gera_segundo_aviso_de_vencido():
    boleto = _boleto(date(2026, 11, 16))
    historico = [
        _aviso("D-7", "enviado", "2026-11-09"),
        _aviso("D+1", "enviado", "2026-11-19"),
        _aviso("D+3"),
    ]

    assert decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 19)) is None


def test_proximo_aviso_de_vencido_sai_no_dia_seguinte():
    boleto = _boleto(date(2026, 11, 20))  # D+1 e D+3 previstos para segunda 23
    historico = [_aviso("D-7", "enviado", "2026-11-13"), _aviso("D+1", "enviado", "2026-11-23")]

    segunda = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 23))
    terca = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 24))

    assert segunda is None
    assert (terca.tipo_aviso, terca.acao) == ("D+3", FILA_APROVACAO)


def test_aviso_previo_perdido_apos_o_vencimento_vai_para_revisao():
    config = replace(CONFIG, avisos=(-1, 1))
    boleto = _boleto(date(2026, 11, 19))  # D-1 = quarta 18

    no_d0 = decidir_aviso(boleto, [], config, date(2026, 11, 19))
    no_d1 = decidir_aviso(boleto, [], config, date(2026, 11, 20))  # atraso 2, mas ja vencido

    assert no_d0 is None
    assert (no_d1.tipo_aviso, no_d1.acao) == ("D-1", REVISAO)
    assert "vencimento" in no_d1.motivo


@pytest.mark.parametrize(
    "boleto",
    [
        _boleto(date(2026, 11, 16), status="pago"),
        _boleto(date(2026, 11, 16), status="em_revisao"),
        _boleto(date(2026, 11, 16), requer_revisao=1),
        _boleto(None),
    ],
)
def test_boleto_pago_em_revisao_ou_sem_vencimento_nao_recebe_aviso(boleto):
    assert decidir_aviso(boleto, [], CONFIG, date(2026, 11, 17)) is None


@pytest.mark.parametrize("status", ["enviado", "cancelado", "em_revisao"])
def test_aviso_ja_tratado_nao_e_reavaliado(status):
    boleto = _boleto(date(2026, 11, 16))
    historico = [_aviso("D-7", "enviado", "2026-11-09"), _aviso("D+1", status, "2026-11-17")]

    decisao = decidir_aviso(boleto, historico, CONFIG, date(2026, 11, 17))

    assert decisao is None


# --- planejamento com o estado -----------------------------------------------------


@pytest.fixture
def estado(tmp_path):
    with Estado(tmp_path / "estado.db") as e:
        yield e


def _registrar(estado, boleto_id, vencimento, **extras):
    estado.registrar_boleto(
        boleto_id,
        hoje=date(2026, 11, 1),
        cnpj_pagador="12345678000195",
        nome_pagador="Cliente Sintetico",
        valor=Decimal("10.00"),
        vencimento=vencimento,
        **extras,
    )


def test_planejamento_cria_agenda_idempotente(estado):
    _registrar(estado, ID_A, date(2026, 11, 20))

    planejar_execucao(estado, CONFIG, date(2026, 11, 10))
    planejar_execucao(estado, CONFIG, date(2026, 11, 10))

    historico = estado.historico_avisos(ID_A)
    assert [a["tipo_aviso"] for a in historico] == ["D-7", "D+1", "D+3"]
    assert all(a["status"] == "pendente" for a in historico)


def test_planejamento_de_hoje_e_execucao_repetida(estado):
    _registrar(estado, ID_A, date(2026, 11, 16))
    estado.registrar_aviso(ID_A, "D-7", date(2026, 11, 9))
    estado.confirmar_envio(ID_A, "D-7", date(2026, 11, 9))
    hoje = date(2026, 11, 17)

    primeira = planejar_execucao(estado, CONFIG, hoje)
    assert [(d.tipo_aviso, d.acao) for d in primeira] == [("D+1", FILA_APROVACAO)]

    estado.confirmar_envio(ID_A, "D+1", hoje)  # simula o envio aprovado
    assert planejar_execucao(estado, CONFIG, hoje) == []


def test_aviso_atrasado_e_encaminhado_para_revisao_no_estado(estado):
    _registrar(estado, ID_A, date(2026, 11, 16))
    estado.registrar_aviso(ID_A, "D-7", date(2026, 11, 9))
    estado.confirmar_envio(ID_A, "D-7", date(2026, 11, 9))

    decisoes = planejar_execucao(estado, CONFIG, date(2026, 11, 20))

    assert [(d.tipo_aviso, d.acao) for d in decisoes] == [("D+1", REVISAO)]
    assert estado.obter_aviso(ID_A, "D+1")["status"] == "em_revisao"
    # na execucao seguinte, o D+1 em revisao nao volta; avalia-se o D+3
    seguinte = planejar_execucao(estado, CONFIG, date(2026, 11, 20))
    assert [(d.tipo_aviso, d.acao) for d in seguinte] == [("D+3", FILA_APROVACAO)]


def test_boleto_pago_entre_d1_e_d3_nao_gera_mais_avisos(estado):
    _registrar(estado, ID_A, date(2026, 11, 16))
    estado.registrar_aviso(ID_A, "D-7", date(2026, 11, 9))
    estado.confirmar_envio(ID_A, "D-7", date(2026, 11, 9))
    planejar_execucao(estado, CONFIG, date(2026, 11, 17))
    estado.confirmar_envio(ID_A, "D+1", date(2026, 11, 17))

    estado.marcar_pago(ID_A, date(2026, 11, 18), "arquivo removido da pasta")

    assert planejar_execucao(estado, CONFIG, date(2026, 11, 19)) == []
    assert estado.obter_aviso(ID_A, "D+3")["status"] == "cancelado"


def test_limite_de_envios_por_execucao_adia_o_excedente(estado):
    for boleto_id in (ID_A, ID_B):
        _registrar(estado, boleto_id, date(2026, 11, 16))
        estado.registrar_aviso(boleto_id, "D-7", date(2026, 11, 9))
        estado.confirmar_envio(boleto_id, "D-7", date(2026, 11, 9))

    decisoes = planejar_execucao(estado, replace(CONFIG, max_envios_por_execucao=1), date(2026, 11, 17))

    assert sorted(d.acao for d in decisoes) == [ADIADO, FILA_APROVACAO]
    adiado = next(d for d in decisoes if d.acao == ADIADO)
    assert estado.obter_aviso(adiado.boleto_id, "D+1")["status"] == "pendente"  # volta na proxima


def test_planejamento_ignora_boleto_em_revisao(estado):
    _registrar(estado, ID_A, date(2026, 11, 16), motivos_revisao=("CNPJ do pagador invalido",))

    assert planejar_execucao(estado, CONFIG, date(2026, 11, 17)) == []
    assert estado.historico_avisos(ID_A) == []
