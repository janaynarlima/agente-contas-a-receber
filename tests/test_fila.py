"""Fila de aprovacao e entrega. SMTP simulado; nenhum e-mail real."""

import smtplib
from datetime import date
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest

from recebiveis.avisos import FILA_APROVACAO, Decisao, decidir_aviso
from recebiveis.fila import dias_do_tipo, enfileirar, entregar, preparar_email, ErroPreparo
from recebiveis.reconciliacao import reconciliar
from tests.conftest import CNPJ, com_cliente_sem_email
from tests.test_reconciliacao import ler as ler_falso  # leitor falso (nao usado aqui)

HOJE = date(2026, 11, 23)


def _decisao(ambiente, tipo="D+1"):
    return Decisao(ambiente.boleto_id, tipo, date(2026, 11, 23), FILA_APROVACAO)


def _enfileirar(ambiente, tipo="D+1"):
    return enfileirar(ambiente.estado, ambiente.config, _decisao(ambiente, tipo),
                      ambiente.clientes, HOJE, ambiente.env)


def test_dias_do_tipo():
    assert (dias_do_tipo("D-7"), dias_do_tipo("D+1"), dias_do_tipo("D+3")) == (-7, 1, 3)
    with pytest.raises(ErroPreparo):
        dias_do_tipo("X")


def test_enfileirar_grava_o_eml_e_muda_o_status(ambiente):
    resultado = _enfileirar(ambiente)

    assert resultado.situacao == "enfileirado"
    arquivo = Path(resultado.arquivo)
    assert arquivo.parent == ambiente.config.fila_aprovacao.resolve()
    assert arquivo.read_bytes().startswith(b"From:") or b"Subject" in arquivo.read_bytes()
    aviso = ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")
    assert aviso["status"] == "aguardando_aprovacao"
    assert aviso["arquivo_fila"] == str(arquivo)


def test_email_usa_nome_do_cadastro_e_nao_o_texto_do_boleto(ambiente):
    arquivo = Path(_enfileirar(ambiente).arquivo)

    lido = message_from_bytes(arquivo.read_bytes(), policy=default)
    corpo = lido.get_content()

    assert "Cliente Sintetico Ltda" in corpo
    assert "Texto do Boleto" not in corpo
    assert "R$ 1.234,56" in corpo
    assert lido["To"] == "cobranca@example.com"
    assert lido["Bcc"] == "controle@example.com"


def test_enfileirar_duas_vezes_nao_duplica(ambiente):
    _enfileirar(ambiente)

    segunda = _enfileirar(ambiente)

    assert segunda.situacao == "ja_enfileirado"
    assert len(ambiente.estado.listar_avisos("aguardando_aprovacao")) == 1


def test_cliente_sem_email_vai_para_revisao(ambiente):
    com_cliente_sem_email(ambiente)

    resultado = _enfileirar(ambiente)

    assert resultado.situacao == "revisao"
    assert "sem e-mail" in resultado.motivo
    assert ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["status"] == "em_revisao"
    assert not ambiente.config.fila_aprovacao.exists()


def test_cnpj_fora_do_cadastro_vai_para_revisao(ambiente):
    ambiente.clientes.clear()

    resultado = _enfileirar(ambiente)

    assert resultado.situacao == "revisao"
    assert "nao encontrado" in resultado.motivo


def test_boleto_sem_valor_vai_para_revisao_sem_inventar(ambiente):
    ambiente.estado._con.execute("UPDATE boletos SET valor=NULL")

    resultado = _enfileirar(ambiente)

    assert resultado.situacao == "revisao"
    assert "valor" in resultado.motivo


def test_aviso_em_aprovacao_segura_os_seguintes(ambiente):
    _enfileirar(ambiente, "D+1")
    boleto = ambiente.estado.obter_boleto(ambiente.boleto_id)

    decisao = decidir_aviso(boleto, ambiente.estado.historico_avisos(ambiente.boleto_id),
                            ambiente.config, date(2026, 11, 24))

    assert decisao is None  # o D+3 espera o D+1 ser resolvido


def test_preparar_email_exige_email(ambiente):
    com_cliente_sem_email(ambiente)
    boleto = ambiente.estado.obter_boleto(ambiente.boleto_id)

    with pytest.raises(ErroPreparo, match="sem e-mail"):
        preparar_email(ambiente.config, boleto, ambiente.clientes[CNPJ], "D+1", ambiente.env)


def _aprovar(ambiente, tipo="D+1"):
    _enfileirar(ambiente, tipo)
    assert ambiente.estado.aprovar_aviso(ambiente.boleto_id, tipo, HOJE, "maria")


def _entregar(ambiente, *, enviar, tipo="D+1", presente=True, de="aprovado"):
    return entregar(
        ambiente.estado, ambiente.config, ambiente.boleto_id, tipo, HOJE,
        ambiente.clientes, ambiente.env, enviar=enviar, de=de,
        arquivo_presente=lambda _id: presente,
    )


def test_aprovacao_registra_quem_e_quando(ambiente):
    _aprovar(ambiente)

    aviso = ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")

    assert (aviso["status"], aviso["aprovado_por"], aviso["data_decisao"]) == ("aprovado", "maria", "2026-11-23")


def test_dry_run_gera_eml_em_saida_e_nao_envia(ambiente, monkeypatch):
    _aprovar(ambiente)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP nao pode ser usado em dry-run"))

    resultado = _entregar(ambiente, enviar=False)

    assert resultado.situacao == "dry-run"
    assert Path(resultado.arquivo).parent == ambiente.config.saida.resolve()
    assert ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["status"] == "aprovado"  # nada enviado


class SMTPFalso:
    enviados: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def starttls(self, context=None):
        pass

    def login(self, *a):
        pass

    def send_message(self, mensagem):
        SMTPFalso.enviados.append(mensagem["To"])


def test_envio_real_so_com_enviar_confirma_e_limpa_a_fila(ambiente, monkeypatch):
    SMTPFalso.enviados = []
    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)
    _aprovar(ambiente)
    fila = Path(ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["arquivo_fila"])
    assert fila.exists()

    resultado = _entregar(ambiente, enviar=True)

    assert resultado.situacao == "enviado"
    assert SMTPFalso.enviados == ["cobranca@example.com"]
    assert ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["status"] == "enviado"
    assert not fila.exists()


def test_execucao_repetida_nao_envia_duas_vezes(ambiente, monkeypatch):
    SMTPFalso.enviados = []
    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)
    _aprovar(ambiente)

    _entregar(ambiente, enviar=True)
    segunda = _entregar(ambiente, enviar=True)

    assert segunda.situacao == "ja_tratado"
    assert len(SMTPFalso.enviados) == 1


def test_smtp_indisponivel_devolve_o_aviso_a_aprovado(ambiente, monkeypatch):
    def falha(*_a, **_k):
        raise ConnectionRefusedError("recusado")

    monkeypatch.setattr(smtplib, "SMTP", falha)
    _aprovar(ambiente)

    resultado = _entregar(ambiente, enviar=True)

    assert resultado.situacao == "falha"
    aviso = ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")
    assert aviso["status"] == "aprovado"  # tenta de novo na proxima execucao
    assert aviso["data_envio"] is None
    assert "cobranca@example.com" not in resultado.motivo


def test_arquivo_removido_antes_do_envio_nao_envia(ambiente, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("nao deveria enviar"))
    _aprovar(ambiente)

    resultado = _entregar(ambiente, enviar=True, presente=False)

    assert resultado.situacao == "bloqueado"
    assert "removido antes do envio" in resultado.motivo
    assert ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["status"] == "aprovado"


def test_boleto_pago_depois_da_aprovacao_nao_e_enviado(ambiente, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("nao deveria enviar"))
    _aprovar(ambiente)
    ambiente.estado.marcar_pago(ambiente.boleto_id, HOJE, "arquivo removido da pasta")

    resultado = _entregar(ambiente, enviar=True)

    assert resultado.situacao == "ja_tratado"  # o aviso foi cancelado pela baixa


def test_envio_real_exige_remetente_configurado(ambiente, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("nao deveria enviar"))
    ambiente.env.pop("EMAIL_REMETENTE")
    _aprovar(ambiente)

    resultado = _entregar(ambiente, enviar=True)

    assert resultado.situacao == "bloqueado"
    assert "EMAIL_REMETENTE" in resultado.motivo


def test_rejeicao_cancela_e_registra_motivo(ambiente):
    _enfileirar(ambiente)

    ok = ambiente.estado.rejeitar_aviso(ambiente.boleto_id, "D+1", HOJE, "maria", "cliente ja pagou")

    aviso = ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")
    assert ok and aviso["status"] == "cancelado"
    assert aviso["motivo"] == "rejeitado: cliente ja pagou"
    assert aviso["aprovado_por"] == "maria"


def test_baixa_remove_o_email_da_fila_de_aprovacao(ambiente):
    fila = Path(_enfileirar(ambiente).arquivo)
    assert fila.exists()
    # a pasta de entrada esta vazia e o unico boleto aberto: baixa so com confirmacao
    resultado = reconciliar(ambiente.estado, ambiente.config, HOJE, ler_falso,
                            confirmar_baixas_acima_do_limite=True)

    assert resultado.baixados
    assert not fila.exists()
    assert ambiente.estado.obter_aviso(ambiente.boleto_id, "D+1")["status"] == "cancelado"
