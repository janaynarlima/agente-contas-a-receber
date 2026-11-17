"""Testes da reconciliacao de baixa. Arquivos e dados sinteticos em pasta temporaria.

Cada arquivo .pdf de teste contem apenas um token; o leitor falso devolve os
dados do boleto a partir dele, sem OCR.
"""

import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from recebiveis.avisos import agenda_avisos
from recebiveis.config import Config
from recebiveis.estado import Estado
from recebiveis.extrator import DadosBoleto
from recebiveis.reconciliacao import (
    MOTIVO_BAIXA,
    boleto_ainda_na_entrada,
    mascarar_id,
    reconciliar,
    varrer_entrada,
)

HOJE = date(2026, 11, 17)
CONFIG = Config(
    entrada=Path("entrada"),
    idioma_ocr="por",
    avisos=(-7, 1, 3),
    confianca_minima=0.8,
    aprovacao_vencidos=True,
    fase_validacao_concluida=False,
    max_percentual_baixas=30,
    max_envios_por_execucao=50,
    feriados=frozenset(),
    banco=Path("estado.db"),
)


def bid(numero: int) -> str:
    return f"23791{numero:039d}"  # identificador sintetico de 44 digitos


def ler(caminho: Path) -> DadosBoleto:
    """Leitor falso: token 'ID:n' -> boleto n; 'SEMID' -> sem codigo; 'ERRO' -> falha."""
    token = caminho.read_text(encoding="utf-8").strip()
    if token == "ERRO":
        raise RuntimeError("falha de OCR simulada")
    if token == "SEMID":
        return DadosBoleto(None, None, None, None, None, ("campo ausente: linha_digitavel",))
    numero = int(token.split(":")[1])
    return DadosBoleto("12345678000195", "Cliente Sintetico", None, date(2026, 11, 20), bid(numero))


@pytest.fixture
def ambiente(tmp_path):
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    with Estado(tmp_path / "estado.db") as estado:
        yield entrada, estado, replace(CONFIG, entrada=entrada)


def colocar(entrada: Path, nome: str, conteudo: str, subpasta: str = "") -> Path:
    pasta = entrada / subpasta if subpasta else entrada
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / nome
    arquivo.write_text(conteudo, encoding="utf-8")
    return arquivo


def popular(entrada: Path, quantidade: int) -> None:
    for numero in range(1, quantidade + 1):
        colocar(entrada, f"boleto{numero}.pdf", f"ID:{numero}")


def test_registra_boletos_novos_pelo_codigo_de_barras(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 3)

    resultado = reconciliar(estado, config, HOJE, ler)

    assert len(resultado.novos) == 3
    assert {b["boleto_id"] for b in estado.listar_boletos("aberto")} == {bid(1), bid(2), bid(3)}


def test_arquivo_removido_entre_execucoes_baixa_o_boleto(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    (entrada / "boleto4.pdf").unlink()

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.baixados == [mascarar_id(bid(4))]
    boleto = estado.obter_boleto(bid(4))
    assert boleto["status"] == "pago"
    assert boleto["data_baixa"] == "2026-11-18"
    assert boleto["motivo_baixa"] == MOTIVO_BAIXA
    assert {b["boleto_id"] for b in estado.listar_boletos("aberto")} == {bid(1), bid(2), bid(3)}


def test_arquivo_renomeado_nao_e_considerado_pago(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    (entrada / "boleto1.pdf").rename(entrada / "nome totalmente diferente.pdf")

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.baixados == []
    assert estado.obter_boleto(bid(1))["status"] == "aberto"


def test_arquivo_movido_para_subpasta_nao_e_considerado_pago(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    destino = entrada / "arquivados" / "2026"
    destino.mkdir(parents=True)
    (entrada / "boleto2.pdf").rename(destino / "boleto2.pdf")

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.baixados == []
    assert estado.obter_boleto(bid(2))["status"] == "aberto"


def test_a_leitura_e_recursiva(ambiente):
    entrada, estado, config = ambiente
    colocar(entrada, "a.pdf", "ID:1", subpasta="x/y/z")

    assert bid(1) in varrer_entrada(entrada, ler).identificados


def test_baixa_cancela_avisos_pendentes_e_conta_os_cancelados(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    for previsto in agenda_avisos(date(2026, 11, 20), config.avisos, config.feriados):
        estado.registrar_aviso(bid(4), previsto.tipo_aviso, previsto.data_prevista)
    (entrada / "boleto4.pdf").unlink()

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.avisos_cancelados == 3
    assert {a["status"] for a in estado.historico_avisos(bid(4))} == {"cancelado"}


def test_execucao_repetida_e_idempotente(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    (entrada / "boleto4.pdf").unlink()
    reconciliar(estado, config, HOJE, ler)

    segunda = reconciliar(estado, config, HOJE, ler)

    assert (segunda.novos, segunda.baixados, segunda.reabertos) == ([], [], [])


def test_pasta_inexistente_aborta_e_nao_baixa(ambiente, tmp_path):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)

    resultado = reconciliar(estado, replace(config, entrada=tmp_path / "nao_existe"), HOJE, ler)

    assert resultado.abortada
    assert "inexistente" in resultado.motivo_abortar
    assert len(estado.listar_boletos("aberto")) == 4


def test_pasta_inacessivel_aborta_e_nao_baixa(ambiente, monkeypatch):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)

    def walk_negado(pasta, onerror=None, **_):
        onerror(PermissionError("acesso negado"))
        return iter(())

    monkeypatch.setattr(os, "walk", walk_negado)

    resultado = reconciliar(estado, config, HOJE, ler)

    assert resultado.abortada
    assert resultado.baixados == []
    assert len(estado.listar_boletos("aberto")) == 4


def test_falha_de_leitura_de_um_arquivo_aborta_a_reconciliacao(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    colocar(entrada, "ruim.pdf", "ERRO")
    (entrada / "boleto1.pdf").unlink()

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.abortada
    assert "falha de leitura" in resultado.motivo_abortar
    assert estado.obter_boleto(bid(1))["status"] == "aberto"  # nada baixado


def test_pasta_vazia_nao_baixa_tudo_sem_confirmacao(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    for arquivo in entrada.glob("*.pdf"):
        arquivo.unlink()

    bloqueada = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert bloqueada.precisa_confirmacao
    assert bloqueada.baixados == []
    assert len(bloqueada.baixas_bloqueadas) == 4
    assert len(estado.listar_boletos("aberto")) == 4

    confirmada = reconciliar(estado, config, date(2026, 11, 18), ler,
                             confirmar_baixas_acima_do_limite=True)

    assert len(confirmada.baixados) == 4


def test_limite_de_percentual_de_baixas(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 10)
    reconciliar(estado, config, HOJE, ler)

    for numero in (1, 2, 3, 4):
        (entrada / f"boleto{numero}.pdf").unlink()
    acima = reconciliar(estado, config, date(2026, 11, 18), ler)  # 40%
    assert acima.precisa_confirmacao and acima.baixados == []
    assert "40%" in acima.motivo_bloqueio

    colocar(entrada, "boleto4.pdf", "ID:4")
    no_limite = reconciliar(estado, config, date(2026, 11, 18), ler)  # 30%, nao excede
    assert not no_limite.precisa_confirmacao
    assert len(no_limite.baixados) == 3


def test_boleto_em_revisao_nunca_e_baixado(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    estado.registrar_boleto(bid(99), hoje=HOJE, motivos_revisao=("divergencia de valor",))

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)  # 99 nao esta na pasta

    assert mascarar_id(bid(99)) not in resultado.baixados
    assert estado.obter_boleto(bid(99))["status"] == "em_revisao"


def test_boleto_pago_que_reaparece_volta_a_aberto_com_revisao_humana(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    arquivo = entrada / "boleto4.pdf"
    arquivo.unlink()
    reconciliar(estado, config, date(2026, 11, 18), ler)
    assert estado.obter_boleto(bid(4))["status"] == "pago"

    colocar(entrada, "boleto4.pdf", "ID:4")
    resultado = reconciliar(estado, config, date(2026, 11, 19), ler)

    assert resultado.reabertos == [mascarar_id(bid(4))]
    boleto = estado.obter_boleto(bid(4))
    assert boleto["status"] == "aberto" and boleto["requer_revisao"] == 1
    assert not estado.pode_receber_aviso(bid(4))


def test_arquivo_sem_identificador_bloqueia_baixas_e_vai_para_revisao(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    colocar(entrada, "scan_ruim.pdf", "SEMID")
    (entrada / "boleto1.pdf").unlink()

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    assert resultado.baixados == []
    assert resultado.sem_identificador == ["scan_ruim.pdf"]
    assert "sem identificador" in resultado.motivo_bloqueio
    assert estado.obter_boleto(bid(1))["status"] == "aberto"
    provisorios = [b for b in estado.listar_boletos("em_revisao") if b["boleto_id"].startswith("doc:")]
    assert len(provisorios) == 1
    assert "sem identificador FEBRABAN valido" in provisorios[0]["motivos_revisao"]
    assert not estado.pode_receber_aviso(provisorios[0]["boleto_id"])


def test_registro_provisorio_some_quando_o_arquivo_e_removido(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    ruim = colocar(entrada, "scan_ruim.pdf", "SEMID")
    reconciliar(estado, config, HOJE, ler)
    assert any(b["boleto_id"].startswith("doc:") for b in estado.listar_boletos("em_revisao"))

    ruim.unlink()
    reconciliar(estado, config, date(2026, 11, 18), ler)

    assert not any(b["boleto_id"].startswith("doc:") for b in estado.listar_boletos("em_revisao"))


def test_mesmo_boleto_em_dois_arquivos_conta_uma_vez(ambiente):
    entrada, estado, config = ambiente
    colocar(entrada, "a.pdf", "ID:1")
    colocar(entrada, "b.pdf", "ID:1", subpasta="copia")

    resultado = reconciliar(estado, config, HOJE, ler)

    assert len(estado.listar_boletos()) == 1
    assert resultado.duplicados == [mascarar_id(bid(1))]


def test_arquivos_que_nao_sao_boletos_sao_ignorados(ambiente):
    entrada, estado, config = ambiente
    colocar(entrada, "boleto.pdf", "ID:1")
    colocar(entrada, "notas.txt", "ERRO")
    colocar(entrada, "~$boleto.pdf", "ERRO")

    assert list(varrer_entrada(entrada, ler).identificados) == [bid(1)]


def test_verificacao_antes_do_envio(ambiente, tmp_path):
    entrada, estado, config = ambiente
    arquivo = colocar(entrada, "boleto1.pdf", "ID:1")

    assert boleto_ainda_na_entrada(entrada, bid(1), ler) is True

    arquivo.unlink()  # sumiu entre a carga e o envio
    assert boleto_ainda_na_entrada(entrada, bid(1), ler) is False
    assert boleto_ainda_na_entrada(tmp_path / "nao_existe", bid(1), ler) is False


def test_verificacao_antes_do_envio_com_falha_de_leitura_nao_autoriza(ambiente):
    entrada, estado, config = ambiente
    colocar(entrada, "boleto1.pdf", "ID:1")
    colocar(entrada, "ruim.pdf", "ERRO")

    assert boleto_ainda_na_entrada(entrada, bid(1), ler) is False


def test_eventos_para_log_nao_expoem_o_identificador_completo(ambiente):
    entrada, estado, config = ambiente
    popular(entrada, 4)
    reconciliar(estado, config, HOJE, ler)
    (entrada / "boleto4.pdf").unlink()

    resultado = reconciliar(estado, config, date(2026, 11, 18), ler)

    texto = str(resultado.eventos)
    assert bid(4) not in texto
    assert "***" in texto
