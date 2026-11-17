"""Testes da regra FEBRABAN com boletos sinteticos.

O gerador abaixo reimplementa os digitos verificadores de forma independente
do codigo de producao (formulas escritas por outro caminho), para que um erro
na regra nao se repita nos testes.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from recebiveis.extrator import extrair_dados_boleto
from recebiveis.validador import (
    ErroFebraban,
    fator_para_data,
    interpretar_codigo_barras,
    interpretar_linha_digitavel,
    linha_para_codigo_barras,
    validar_codigo_barras,
    validar_linha_digitavel,
)

BASE = date(1997, 10, 7)
LIVRE = "0123456789012345678901234"  # campo livre sintetico (25 digitos)


def _dv10(campo: str) -> str:
    pesos = [2 if i % 2 == 0 else 1 for i in range(len(campo))][::-1]
    soma = 0
    for digito, peso in zip(campo, pesos):
        soma += sum(int(c) for c in str(int(digito) * peso))
    return str((10 - soma % 10) % 10)


def _dv11(base43: str) -> str:
    pesos = [(i % 8) + 2 for i in range(len(base43))][::-1]
    total = sum(int(d) * p for d, p in zip(base43, pesos))
    dv = 11 - total % 11
    return "1" if dv > 9 or dv == 0 else str(dv)


def gerar(fator: int, centavos: int, banco="237", livre=LIVRE) -> tuple[str, str]:
    """Retorna (codigo_barras, linha_digitavel) sinteticos e validos."""
    sem_dv = f"{banco}9{fator:04d}{centavos:010d}{livre}"
    barras = sem_dv[:4] + _dv11(sem_dv) + sem_dv[4:]
    c1 = barras[:4] + barras[19:24]
    c2 = barras[24:34]
    c3 = barras[34:44]
    linha = (
        f"{c1[:5]}.{c1[5:]}{_dv10(c1)} "
        f"{c2[:5]}.{c2[5:]}{_dv10(c2)} "
        f"{c3[:5]}.{c3[5:]}{_dv10(c3)} "
        f"{barras[4]} {barras[5:19]}"
    )
    return barras, linha


def fator_de(data: date) -> int:
    dias = (data - BASE).days
    return dias if dias <= 9999 else dias - 9000


REF = date(2026, 11, 17)


def test_gerador_sintetico_e_aceito_pela_validacao():
    barras, linha = gerar(fator_de(date(2026, 11, 20)), 123456)

    assert validar_codigo_barras(barras)
    assert validar_linha_digitavel(linha)
    assert linha_para_codigo_barras(linha) == barras


def test_valor_e_vencimento_derivados_da_linha_digitavel():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)

    dados = interpretar_linha_digitavel(linha, referencia=REF)

    assert dados.valor == Decimal("1234.56")
    assert dados.vencimento == date(2026, 11, 20)


def test_codigo_de_barras_direto():
    barras, _ = gerar(fator_de(date(2026, 12, 5)), 2500)

    dados = interpretar_codigo_barras(barras, referencia=REF)

    assert dados.valor == Decimal("25.00")
    assert dados.vencimento == date(2026, 12, 5)


@pytest.mark.parametrize(
    "fator,esperado",
    [
        (1000, date(2000, 7, 3)),
        (9999, date(2025, 2, 21)),  # ultimo dia do primeiro ciclo
        (1000, date(2025, 2, 22)),  # reinicio do ciclo
        (1001, date(2025, 2, 23)),
    ],
)
def test_fator_de_vencimento_e_reinicio_em_2025(fator, esperado):
    assert fator_para_data(fator, referencia=esperado) == esperado


def test_mesmo_fator_resolve_pelo_ciclo_mais_proximo_da_referencia():
    assert fator_para_data(1000, referencia=date(2000, 7, 1)) == date(2000, 7, 3)
    assert fator_para_data(1000, referencia=date(2025, 3, 1)) == date(2025, 2, 22)


def test_fator_zero_significa_sem_vencimento_e_valor_zero_nao_informado():
    barras, _ = gerar(0, 0)

    dados = interpretar_codigo_barras(barras, referencia=REF)

    assert dados.vencimento is None
    assert dados.valor is None


def test_fator_abaixo_do_intervalo_e_rejeitado():
    with pytest.raises(ErroFebraban):
        fator_para_data(500, referencia=REF)


def test_rejeitar_digito_verificador_de_campo_da_linha():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    adulterada = linha[:9] + str((int(linha[9]) + 1) % 10) + linha[10:]

    assert not validar_linha_digitavel(adulterada)
    with pytest.raises(ErroFebraban, match="campo 1"):
        interpretar_linha_digitavel(adulterada)


def test_rejeitar_valor_adulterado_pelo_dv_geral():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    adulterada = linha[:-1] + str((int(linha[-1]) + 1) % 10)

    with pytest.raises(ErroFebraban, match="geral"):
        interpretar_linha_digitavel(adulterada)


def test_rejeitar_tamanho_errado():
    with pytest.raises(ErroFebraban, match="47"):
        interpretar_linha_digitavel("123")
    with pytest.raises(ErroFebraban, match="44"):
        interpretar_codigo_barras("123")


def _boleto(linha: str, vencimento="20/11/2026", valor="1.234,56") -> str:
    return f"""Beneficiario
Nome: Empresa Beneficiaria
CNPJ/CPF: 11.222.333/0001-81
Vencimento: {vencimento}
Valor do documento: {valor}
Pagador:
Cliente Sintetico Ltda
CNPJ/CPF: 12.345.678/0001-95
Beneficiario final: Nao informado
{linha}
"""


def test_extrator_usa_linha_digitavel_e_expoe_identificador():
    barras, linha = gerar(fator_de(date(2026, 11, 20)), 123456)

    dados = extrair_dados_boleto(_boleto(linha), referencia=REF)

    assert dados.codigo_barras == barras
    assert dados.valor == Decimal("1234.56")
    assert dados.vencimento == date(2026, 11, 20)
    assert dados.motivos_revisao == ()


def test_extrator_deriva_valor_e_vencimento_sem_rotulos():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    texto = f"Pagador:\nCliente Sintetico\nCNPJ: 12.345.678/0001-95\n{linha}\n"

    dados = extrair_dados_boleto(texto, referencia=REF)

    assert dados.valor == Decimal("1234.56")
    assert dados.vencimento == date(2026, 11, 20)
    assert dados.motivos_revisao == ()


def test_divergencia_de_valor_e_vencimento_vai_para_revisao():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)

    dados = extrair_dados_boleto(
        _boleto(linha, vencimento="21/11/2026", valor="999,99"), referencia=REF
    )

    assert "divergencia de valor entre texto e linha digitavel" in dados.motivos_revisao
    assert "divergencia de vencimento entre texto e linha digitavel" in dados.motivos_revisao


def test_linha_invalida_vai_para_revisao_sem_usar_o_texto_como_verdade():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    invalida = linha[:-1] + str((int(linha[-1]) + 1) % 10)

    dados = extrair_dados_boleto(_boleto(invalida), referencia=REF)

    assert dados.codigo_barras is None
    assert any(m.startswith("linha digitavel invalida") for m in dados.motivos_revisao)


def test_ausencia_de_linha_digitavel_gera_motivo():
    dados = extrair_dados_boleto("Pagador:\nCliente\nCNPJ: 12.345.678/0001-95\n", referencia=REF)

    assert "campo ausente: linha_digitavel" in dados.motivos_revisao


def test_copia_valida_basta_quando_a_outra_copia_tem_erro_de_ocr():
    _, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    ruim = linha[:-1] + str((int(linha[-1]) + 1) % 10)

    dados = extrair_dados_boleto(_boleto(linha) + ruim + "\n", referencia=REF)

    assert dados.codigo_barras is not None
    assert not any("invalida" in m for m in dados.motivos_revisao)


def test_linhas_validas_diferentes_no_mesmo_documento_vao_para_revisao():
    _, linha_a = gerar(fator_de(date(2026, 11, 20)), 123456)
    _, linha_b = gerar(fator_de(date(2026, 11, 21)), 999)

    dados = extrair_dados_boleto(_boleto(linha_a) + linha_b + "\n", referencia=REF)

    assert dados.codigo_barras is None
    assert any("divergentes" in m for m in dados.motivos_revisao)


def test_linha_com_espacos_no_lugar_de_pontos_e_aceita():
    barras, linha = gerar(fator_de(date(2026, 11, 20)), 123456)

    dados = extrair_dados_boleto(_boleto(linha.replace(".", " ")), referencia=REF)

    assert dados.codigo_barras == barras


def test_linha_com_dv_geral_colado_ao_fator_e_aceita():
    barras, linha = gerar(fator_de(date(2026, 11, 20)), 123456)
    colada = linha[:-15] + linha[-15:].replace(" ", "")

    dados = extrair_dados_boleto(_boleto(colada), referencia=REF)

    assert dados.codigo_barras == barras


def test_data_apos_reinicio_do_fator_no_extrator():
    barras, linha = gerar(fator_de(date(2025, 3, 10)), 5000)

    dados = extrair_dados_boleto(_boleto(linha, vencimento="10/03/2025", valor="50,00"), referencia=date(2025, 3, 1))

    assert dados.codigo_barras == barras
    assert dados.vencimento == date(2025, 3, 10)
    assert dados.motivos_revisao == ()
