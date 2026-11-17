"""Testes do SQLite de estado. Dados sinteticos; banco em pasta temporaria."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from recebiveis.estado import Estado, ErroEstado

HOJE = date(2026, 11, 17)
ID_A = "23791" + "1" * 39  # identificador sintetico de 44 digitos
ID_B = "23791" + "2" * 39


@pytest.fixture
def estado(tmp_path):
    with Estado(tmp_path / "pasta nova" / "estado.db") as e:
        yield e


def _novo(estado, boleto_id=ID_A, **extras):
    return estado.registrar_boleto(
        boleto_id,
        hoje=HOJE,
        cnpj_pagador="12345678000195",
        nome_pagador="Cliente Sintetico",
        valor=Decimal("1234.56"),
        vencimento=date(2026, 11, 20),
        **extras,
    )


def test_cria_banco_e_pasta(tmp_path):
    caminho = tmp_path / "a" / "b" / "estado.db"

    with Estado(caminho):
        pass

    assert caminho.exists()


def test_registra_e_le_boleto(estado):
    assert _novo(estado) is True

    boleto = estado.obter_boleto(ID_A)

    assert boleto["status"] == "aberto"
    assert boleto["valor"] == "1234.56"
    assert boleto["vencimento"] == "2026-11-20"
    assert boleto["motivos_revisao"] == []
    assert estado.pode_receber_aviso(ID_A)


def test_boleto_repetido_nao_e_sobrescrito(estado):
    _novo(estado)
    estado.marcar_pago(ID_A, HOJE, "arquivo removido da pasta")

    assert _novo(estado) is False
    assert estado.obter_boleto(ID_A)["status"] == "pago"


def test_boleto_com_motivo_de_revisao_nasce_em_revisao_e_nao_recebe_aviso(estado):
    _novo(estado, motivos_revisao=("CNPJ do pagador invalido",))

    boleto = estado.obter_boleto(ID_A)

    assert boleto["status"] == "em_revisao"
    assert boleto["motivos_revisao"] == ["CNPJ do pagador invalido"]
    assert not estado.pode_receber_aviso(ID_A)


def test_chave_unica_impede_aviso_duplicado(estado):
    _novo(estado)

    assert estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21)) is True
    assert estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21)) is False
    assert len(estado.historico_avisos(ID_A)) == 1


def test_banco_rejeita_duplicidade_mesmo_por_sql_direto(estado):
    _novo(estado)
    estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))

    with pytest.raises(sqlite3.IntegrityError):
        estado._con.execute(
            "INSERT INTO avisos (boleto_id, tipo_aviso, data_prevista, status) "
            "VALUES (?, 'D+1', '2026-11-21', 'pendente')",
            (ID_A,),
        )


def test_aviso_exige_boleto_existente(estado):
    with pytest.raises(sqlite3.IntegrityError):
        estado.registrar_aviso(ID_B, "D+1", date(2026, 11, 21))


def test_execucao_repetida_no_mesmo_dia_confirma_envio_uma_vez(estado):
    _novo(estado)
    estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))

    assert estado.confirmar_envio(ID_A, "D+1", HOJE) is True
    assert estado.confirmar_envio(ID_A, "D+1", HOJE) is False
    assert estado.obter_aviso(ID_A, "D+1")["status"] == "enviado"


def test_duas_execucoes_concorrentes_so_uma_envia(tmp_path):
    caminho = tmp_path / "estado.db"
    with Estado(caminho) as primeira:
        _novo(primeira)
        primeira.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))
        with Estado(caminho) as segunda:
            resultados = [
                primeira.confirmar_envio(ID_A, "D+1", HOJE),
                segunda.confirmar_envio(ID_A, "D+1", HOJE),
            ]

    assert sorted(resultados) == [False, True]


def test_envio_que_falha_volta_a_pendente_e_pode_ser_reenviado(estado):
    _novo(estado)
    estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))
    estado.confirmar_envio(ID_A, "D+1", HOJE)

    assert estado.reverter_envio(ID_A, "D+1", "SMTP indisponivel") is True

    aviso = estado.obter_aviso(ID_A, "D+1")
    assert aviso["status"] == "pendente"
    assert aviso["data_envio"] is None
    assert estado.confirmar_envio(ID_A, "D+1", HOJE) is True


def test_baixa_registra_motivo_e_cancela_avisos_pendentes(estado):
    _novo(estado)
    estado.registrar_aviso(ID_A, "D-7", date(2026, 11, 13))
    estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))
    estado.confirmar_envio(ID_A, "D-7", HOJE)

    assert estado.marcar_pago(ID_A, HOJE, "arquivo removido da pasta") is True

    boleto = estado.obter_boleto(ID_A)
    assert boleto["status"] == "pago"
    assert boleto["data_baixa"] == "2026-11-17"
    assert boleto["motivo_baixa"] == "arquivo removido da pasta"
    assert estado.obter_aviso(ID_A, "D+1")["status"] == "cancelado"
    assert estado.obter_aviso(ID_A, "D-7")["status"] == "enviado"  # historico preservado


def test_boleto_em_revisao_nao_e_baixado_automaticamente(estado):
    _novo(estado, motivos_revisao=("divergencia de valor",))

    assert estado.marcar_pago(ID_A, HOJE, "arquivo removido da pasta") is False
    assert estado.obter_boleto(ID_A)["status"] == "em_revisao"


def test_baixa_de_boleto_inexistente_ou_ja_pago_nao_faz_nada(estado):
    _novo(estado)
    estado.marcar_pago(ID_A, HOJE, "m")

    assert estado.marcar_pago(ID_A, HOJE, "m") is False
    assert estado.marcar_pago(ID_B, HOJE, "m") is False


def test_reabertura_volta_a_aberto_mas_exige_revisao_e_bloqueia_aviso(estado):
    _novo(estado)
    estado.marcar_pago(ID_A, HOJE, "arquivo removido da pasta")

    assert estado.reabrir(ID_A, HOJE, "boleto pago reapareceu") is True

    boleto = estado.obter_boleto(ID_A)
    assert boleto["status"] == "aberto"
    assert boleto["requer_revisao"] == 1
    assert boleto["data_baixa"] is None
    assert boleto["motivos_revisao"] == ["boleto pago reapareceu"]
    assert not estado.pode_receber_aviso(ID_A)


def test_so_boleto_pago_pode_ser_reaberto(estado):
    _novo(estado)

    assert estado.reabrir(ID_A, HOJE, "x") is False


def test_decisao_humana_libera_boleto_reaberto(estado):
    _novo(estado)
    estado.marcar_pago(ID_A, HOJE, "m")
    estado.reabrir(ID_A, HOJE, "reapareceu")

    assert estado.liberar_revisao(ID_A, HOJE) is True
    assert estado.pode_receber_aviso(ID_A)


def test_aviso_pode_ser_encaminhado_para_revisao_e_nao_e_enviado(estado):
    _novo(estado)
    estado.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))

    assert estado.encaminhar_aviso_para_revisao(ID_A, "D+1", "atraso superior a 2 dias") is True

    assert estado.obter_aviso(ID_A, "D+1")["status"] == "em_revisao"
    assert estado.confirmar_envio(ID_A, "D+1", HOJE) is False


def test_listar_por_status_e_status_invalido(estado):
    _novo(estado, ID_A)
    _novo(estado, ID_B)
    estado.marcar_pago(ID_B, HOJE, "m")

    assert [b["boleto_id"] for b in estado.listar_boletos("aberto")] == [ID_A]
    assert [b["boleto_id"] for b in estado.listar_boletos("pago")] == [ID_B]
    assert len(estado.listar_boletos()) == 2
    with pytest.raises(ErroEstado):
        estado.listar_boletos("inexistente")


def test_estado_persiste_entre_execucoes(tmp_path):
    caminho = tmp_path / "estado.db"
    with Estado(caminho) as primeira:
        _novo(primeira)
        primeira.registrar_aviso(ID_A, "D+1", date(2026, 11, 21))
        primeira.confirmar_envio(ID_A, "D+1", HOJE)

    with Estado(caminho) as segunda:
        assert segunda.obter_aviso(ID_A, "D+1")["status"] == "enviado"
        assert segunda.registrar_aviso(ID_A, "D+1", date(2026, 11, 21)) is False
