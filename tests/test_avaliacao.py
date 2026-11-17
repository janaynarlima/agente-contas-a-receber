"""Golden set sintetico e metricas.

Os testes que dependem de OCR so rodam se o Tesseract estiver instalado.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from recebiveis.avaliacao import gerador
from recebiveis.avaliacao.gerador import Caso, carregar_gabarito, gerar_conjunto
from recebiveis.avaliacao.metricas import (
    ResultadoCaso,
    _comparar,
    gerar_relatorio_md,
    medir_conjunto,
    normalizar_nome,
    resumir,
)
from recebiveis.avaliacao.sintetico import cnpj_valido, fator_de, gerar_boleto
from recebiveis.validador import validar_cnpj, validar_linha_digitavel


def _tesseract_disponivel() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


COM_OCR = pytest.mark.skipif(not _tesseract_disponivel(), reason="Tesseract nao instalado")


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    pasta = tmp_path_factory.mktemp("golden")
    gerar_conjunto(pasta)
    return pasta


# --- gerador -----------------------------------------------------------------------


def test_conjunto_tem_ao_menos_20_boletos_e_cobre_o_exigido_no_prompt(golden):
    casos = carregar_gabarito(golden)

    assert len(casos) >= 20
    assert {"pdf_texto", "pdf_escaneado", "png", "jpg"} <= {c.formato for c in casos}
    assert {"coluna", "linha", "blocos", "sacado"} <= {c.layout for c in casos}  # "bancos diferentes"
    ids = {c.id for c in casos}
    for obrigatorio in ("neg_cnpj_invalido", "neg_vencimento_divergente", "neg_valor_divergente",
                        "neg_sem_pagador", "ocr_ruim_ruido", "ocr_ruim_baixa_resolucao",
                        "pdf_texto_cnpj15", "pdf_texto_pos2025"):
        assert obrigatorio in ids
    assert all((golden / c.arquivo).is_file() for c in casos)


def test_geracao_e_deterministica(tmp_path):
    a = gerar_conjunto(tmp_path / "a")
    b = gerar_conjunto(tmp_path / "b")

    assert [c.__dict__ for c in a] == [c.__dict__ for c in b]
    assert (tmp_path / "a" / "gabarito.json").read_text() == (tmp_path / "b" / "gabarito.json").read_text()


def test_gabarito_so_tem_dados_ficticios_validos(golden):
    for caso in carregar_gabarito(golden):
        if caso.cnpj:
            assert validar_cnpj(caso.cnpj)
            assert not caso.cnpj.startswith(("11222333", "12345678"))  # nao reaproveita exemplos de documentacao
        assert caso.esperado in ("aceitar", "revisao", "indiferente")


def test_gerador_independente_de_boleto_passa_na_validacao_de_producao():
    barras, linha = gerar_boleto(date(2026, 12, 4), 123456, "237", "0" * 25)

    assert validar_linha_digitavel(linha)
    assert fator_de(date(2025, 2, 21)) == 9999 and fator_de(date(2025, 2, 22)) == 1000
    assert fator_de(date(2025, 3, 10)) == 1000 + 16  # 16 dias depois do reinicio do fator


def test_cnpj_sintetico_tem_digitos_verificadores_corretos():
    assert all(validar_cnpj(cnpj_valido(n)) for n in range(1, 200, 7))


def test_nao_sobrescreve_o_que_nao_e_do_conjunto(tmp_path):
    (tmp_path / "meu_arquivo.txt").write_text("importante")

    gerar_conjunto(tmp_path)

    assert (tmp_path / "meu_arquivo.txt").read_text() == "importante"


# --- metricas (sem OCR) --------------------------------------------------------------


def _caso(**sobrescritas) -> Caso:
    base = dict(id="x", arquivo="x.pdf", formato="pdf_texto", layout="coluna", descricao="", esperado="aceitar",
                cnpj="12345678000195", nome="Comercial Aurora Ltda", valor="100.00", vencimento="2026-12-04")
    base.update(sobrescritas)
    return Caso(**base)


def _resultado(caso, extraido, motivos=()):
    r = ResultadoCaso(caso=caso, extraido=extraido, motivos_revisao=tuple(motivos))
    r.acertos, r.indevidos = _comparar(caso, extraido)
    return r


CORRETO = {"cnpj": "12345678000195", "nome": "COMERCIAL AURORA Ltda", "valor": Decimal("100.00"),
           "vencimento": date(2026, 12, 4)}


def test_normalizar_nome_ignora_acento_caixa_e_espacos():
    assert normalizar_nome("  Indústria   ALFA ") == "industria alfa"
    assert normalizar_nome(None) is None


def test_caso_correto_e_aceito_nao_e_falso_aceite():
    r = _resultado(_caso(), CORRETO)

    assert r.aceito and r.todos_corretos and not r.falso_aceite and not r.revisao_desnecessaria


def test_aceitar_com_campo_errado_e_falso_aceite():
    r = _resultado(_caso(), {**CORRETO, "nome": "Comercial Aurora Ltda CNPJ/CPF: 12.345"})

    assert r.aceito and r.falso_aceite


def test_caso_que_exigia_revisao_e_aceito_e_falso_aceite():
    r = _resultado(_caso(esperado="revisao"), CORRETO)

    assert r.falso_aceite


def test_revisao_obrigatoria_capturada_nao_e_falso_aceite():
    r = _resultado(_caso(esperado="revisao"), CORRETO, motivos=("divergencia",))

    assert not r.falso_aceite and not r.revisao_desnecessaria


def test_revisao_de_caso_que_podia_ser_aceito_e_desnecessaria_mas_segura():
    r = _resultado(_caso(), {**CORRETO, "cnpj": None}, motivos=("campo ausente: cnpj_pagador",))

    assert r.revisao_desnecessaria and not r.falso_aceite


def test_dado_extraido_quando_o_gabarito_diz_que_nao_deve_ser_aceito_e_erro():
    caso = _caso(esperado="revisao", cnpj=None)  # so existe o CNPJ do beneficiario

    r = _resultado(caso, {**CORRETO, "cnpj": "11222333000181"})

    assert r.indevidos == ["cnpj"]
    assert not r.todos_corretos


def test_campos_nao_avaliados_sao_ignorados():
    caso = _caso(valor=None, campos_nao_avaliados=["valor"])

    r = _resultado(caso, {**CORRETO, "valor": Decimal("999")})

    assert "valor" not in r.acertos and r.todos_corretos


def test_ocr_ruim_indiferente_aceito_com_erro_e_falha_mas_correto_ou_revisao_servem():
    caso = _caso(esperado="indiferente")

    assert _resultado(caso, {**CORRETO, "valor": Decimal("1")}).falso_aceite
    assert not _resultado(caso, CORRETO).falso_aceite
    assert not _resultado(caso, {**CORRETO, "valor": None}, motivos=("campo ausente: valor",)).falso_aceite


def test_resumo_e_relatorio_citam_conjunto_metrica_e_limites():
    resultados = [_resultado(_caso(id="a"), CORRETO),
                  _resultado(_caso(id="b", esperado="revisao"), CORRETO)]
    resultados[0].segundos, resultados[1].segundos = 0.01, 0.02

    resumo = resumir(resultados)
    texto = gerar_relatorio_md(resultados, {"semente": 1, "data": "17/11/2026", "versao_codigo": "t", "tesseract": "5"})

    assert resumo.falsos_aceites == ["b"]
    assert resumo.por_campo["cnpj"] == (2, 2)
    assert "sintéticos" in texto and "golden/" in texto
    assert "Falso aceite" in texto and "**SIM**" in texto
    assert "não se aplica: nenhum LLM" in texto
    assert "não** estima o acerto em boletos reais" in texto


# --- medicao de ponta a ponta -------------------------------------------------------------


def test_leitura_sem_ocr_dos_pdfs_com_texto_nao_tem_falso_aceite(golden):
    resultados = [r for r in medir_conjunto(golden) if r.caso.formato == "pdf_texto"]

    assert resultados
    assert [r.caso.id for r in resultados if r.falso_aceite] == []
    assert all(r.em_revisao for r in resultados if r.caso.esperado == "revisao")
    assert all(not r.em_revisao and r.todos_corretos for r in resultados if r.caso.esperado == "aceitar")


@COM_OCR
def test_medicao_completa_nunca_aceita_dado_errado_e_captura_toda_revisao_obrigatoria(golden):
    resultados = medir_conjunto(golden)
    resumo = resumir(resultados)

    assert resumo.falsos_aceites == []  # invariante de seguranca
    assert resumo.revisao_necessaria_capturada == resumo.revisao_necessaria_total
    aceitaveis = [r for r in resultados if r.caso.esperado == "aceitar"]
    assert sum(r.aceito and r.todos_corretos for r in aceitaveis) >= 0.9 * len(aceitaveis)
