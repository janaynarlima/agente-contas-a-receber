from datetime import date
from decimal import Decimal

import pytest

from recebiveis.extrator import extrair_dados_boleto
from recebiveis.validador import validar_cnpj


def test_extrair_campos_do_pagador_com_beneficiario_antes():
    texto = """BENEFICIARIO
Nome: Empresa Beneficiaria
CNPJ/CPF: 11.222.333/0001-81
PAGADOR
Nome: Cliente Sintético
CNPJ/CPF: 12.345.678/0001-95
Vencimento: 20/11/2026
Valor do documento: R$ 1.234,56
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador == "12345678000195"
    assert dados.nome_pagador == "Cliente Sintético"
    assert dados.valor == Decimal("1234.56")
    assert dados.vencimento == date(2026, 11, 20)


def test_extrair_pagador_quando_beneficiario_aparece_depois():
    texto = """PAGADOR
Nome: Cliente Sintetico
CNPJ: 12.345.678/0001-95
BENEFICIARIO
Nome: Empresa Beneficiaria
CNPJ: 11.222.333/0001-81
Valor cobrado R$ 25,00
Data de vencimento: 01/12/2026
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador == "12345678000195"
    assert dados.nome_pagador == "Cliente Sintetico"


def test_extrair_nome_do_cabecalho_pagador():
    dados = extrair_dados_boleto(
        """Pagador: Cliente Sintético
CNPJ: 12.345.678/0001-95
"""
    )

    assert dados.nome_pagador == "Cliente Sintético"


def test_cnpj_invalido_vai_para_revisao_sem_excecao():
    texto = """PAGADOR
Nome: Cliente Sintetico
CNPJ: 12.345.678/0001-90
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador is None
    assert dados.nome_pagador == "Cliente Sintetico"
    assert "CNPJ do pagador invalido" in dados.motivos_revisao


def test_cnpj_com_tamanho_errado_vai_para_revisao():
    dados = extrair_dados_boleto("PAGADOR\nNome: Cliente Sintetico\nCNPJ: 12.345.678/0001-955\n")

    assert dados.cnpj_pagador is None
    assert "CNPJ do pagador deve conter 14 digitos" in dados.motivos_revisao


def test_cnpj_com_15_digitos_e_zero_a_esquerda_e_validado():
    dados = extrair_dados_boleto("PAGADOR\nNome: Cliente Sintetico\nCNPJ/CPF: 012.345.678/ 0001-95\n")

    assert dados.cnpj_pagador == "12345678000195"


def test_cnpj_de_15_digitos_com_zero_ainda_exige_digito_verificador():
    dados = extrair_dados_boleto("PAGADOR\nNome: Cliente Sintetico\nCNPJ/CPF: 012.345.678/0001-90\n")

    assert dados.cnpj_pagador is None
    assert "CNPJ do pagador invalido" in dados.motivos_revisao


def test_cnpj_de_15_digitos_sem_zero_inicial_nao_e_adivinhado():
    dados = extrair_dados_boleto("PAGADOR\nNome: Cliente Sintetico\nCNPJ/CPF: 112.345.678/0001-95\n")

    assert dados.cnpj_pagador is None


def test_rotulos_e_valores_em_linhas_separadas_com_nome_na_linha_seguinte():
    texto = """Beneficiario
CNPJ/CPF: 11.222.333/0001-81
Vencimento
20/11/2026
Valor/Percentual
(=) Valor do documento
1.234,56
Pagador:
Cliente Sintetico Ltda
CNPJ/CPF: 012.345.678/ 0001-95
Beneficiario final:
Nao informado
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador == "12345678000195"
    assert dados.nome_pagador == "Cliente Sintetico Ltda"
    assert dados.valor == Decimal("1234.56")
    assert dados.vencimento == date(2026, 11, 20)
    # Sem linha digitavel no texto, o unico motivo de revisao e a ausencia dela.
    assert dados.motivos_revisao == ("campo ausente: linha_digitavel",)


def test_rotulo_de_cnpj_com_erro_de_ocr_ainda_exige_digito_verificador():
    valido = extrair_dados_boleto("Pagador: Cliente Sintetico CNP/CPF:12.345.678/0001-95\n")
    invalido = extrair_dados_boleto("Pagador: Cliente Sintetico CNH/CPF: 12.345.678/0001-90\n")

    assert valido.cnpj_pagador == "12345678000195"
    assert invalido.cnpj_pagador is None


def test_nome_e_documento_na_mesma_linha_nao_contaminam_o_nome():
    # Regressao achada pelo golden set: o nome saia como "Nome  CNPJ/CPF: ..." e
    # o boleto era aceito sem revisao.
    dados = extrair_dados_boleto("Pagador: Comercial Aurora Ltda  CNPJ/CPF: 12.345.678/0001-95\n")

    assert dados.nome_pagador == "Comercial Aurora Ltda"
    assert dados.cnpj_pagador == "12345678000195"


def test_separador_de_documento_com_hifen_ou_barra():
    for linha in ("Pagador: Aurora Ltda - CNPJ: 12.345.678/0001-95",
                  "Pagador: Aurora Ltda | CNPJ/CPF: 12.345.678/0001-95"):
        dados = extrair_dados_boleto(linha + "\n")
        assert dados.nome_pagador == "Aurora Ltda"
        assert dados.cnpj_pagador == "12345678000195"


def test_nome_na_linha_seguinte_tambem_separa_o_documento():
    dados = extrair_dados_boleto("Pagador:\nAurora Ltda  CNPJ/CPF: 12.345.678/0001-95\n")

    assert dados.nome_pagador == "Aurora Ltda"
    assert dados.cnpj_pagador == "12345678000195"


def test_nome_que_contem_a_sigla_nao_e_cortado_no_meio_da_palavra():
    dados = extrair_dados_boleto("Pagador: Cnpjota Servicos Ltda CNPJ: 12.345.678/0001-95\n")

    assert dados.nome_pagador == "Cnpjota Servicos Ltda"


def test_sacado_e_o_termo_antigo_de_pagador_e_cedente_encerra_o_trecho():
    texto = """Cedente
Empresa Beneficiaria  CNPJ: 11.222.333/0001-81
Sacado
Cliente Sintetico
CNPJ/CPF: 12.345.678/0001-95
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador == "12345678000195"
    assert dados.nome_pagador == "Cliente Sintetico"


def test_cedente_depois_do_sacado_nao_vira_pagador():
    texto = """Sacado
Cliente Sintetico
CNPJ/CPF: 12.345.678/0001-95
Cedente
Empresa Beneficiaria
CNPJ: 11.222.333/0001-81
"""

    assert extrair_dados_boleto(texto).cnpj_pagador == "12345678000195"


def test_campo_ausente_gera_motivo_de_revisao():
    dados = extrair_dados_boleto("PAGADOR\nNome: Cliente Sintetico\nCNPJ: 12.345.678/0001-95\n")

    assert "campo ausente: valor" in dados.motivos_revisao
    assert "campo ausente: vencimento" in dados.motivos_revisao


def test_data_invalida_vai_para_revisao():
    dados = extrair_dados_boleto("Vencimento: 31/02/2026\n")

    assert dados.vencimento is None
    assert "Data de vencimento invalida" in dados.motivos_revisao


def test_cnpj_do_beneficiario_nunca_e_usado_como_pagador():
    texto = """Beneficiario
CNPJ/CPF: 11.222.333/0001-81
Pagador:
Cliente Sintetico
"""

    dados = extrair_dados_boleto(texto)

    assert dados.cnpj_pagador is None


def test_nao_inferir_campos_ausentes():
    dados = extrair_dados_boleto(
        """PAGADOR
Nome: Cliente Sintetico
CNPJ: 12.345.678/0001-95
"""
    )

    assert dados.cnpj_pagador == "12345678000195"
    assert dados.nome_pagador == "Cliente Sintetico"
    assert dados.valor is None
    assert dados.vencimento is None


def test_validar_cnpj_formatado_e_rejeitar_digitos_repetidos():
    assert validar_cnpj("12.345.678/0001-95")
    assert not validar_cnpj("11.111.111/1111-11")
