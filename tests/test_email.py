"""Testes de templates e e-mail. Nenhum envio real: SMTP e simulado."""

import smtplib
from datetime import date
from decimal import Decimal
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest

from recebiveis import emailer
from recebiveis.emailer import (
    ErroEnvio,
    ErroTemplate,
    carregar_env,
    enviar_smtp,
    formatar_valor,
    gravar_eml,
    montar_mensagem,
    nome_do_template,
    renderizar_aviso,
    validar_template,
)

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
DADOS = dict(
    nome_cliente="Cliente Sintetico Ltda",
    valor=Decimal("1234.56"),
    vencimento=date(2026, 11, 20),
    numero_boleto="23790.12343 56789.012343 56789.012343 9 16660000123456",
)
BOM = "Assunto: Teste\nOlá, {{ nome_cliente }}. Vence em {{ vencimento }}.\n"


def test_formata_valor_em_reais():
    assert formatar_valor(Decimal("1234.5")) == "R$ 1.234,50"
    assert formatar_valor(Decimal("0.99")) == "R$ 0,99"
    assert formatar_valor(Decimal("1000000")) == "R$ 1.000.000,00"


def test_template_certo_para_cada_tipo_e_d0_nao_tem():
    assert nome_do_template(-7) == "aviso_previo.txt.j2"
    assert nome_do_template(1) == "aviso_vencido.txt.j2"
    with pytest.raises(ErroTemplate):
        nome_do_template(0)


def test_aviso_previo_renderiza_com_as_variaveis_permitidas():
    aviso = renderizar_aviso(TEMPLATES, -7, **DADOS)

    assert aviso.assunto == "Lembrete: boleto com vencimento em 20/11/2026"
    assert "Cliente Sintetico Ltda" in aviso.corpo
    assert "R$ 1.234,56" in aviso.corpo
    assert "20/11/2026" in aviso.corpo
    assert DADOS["numero_boleto"] in aviso.corpo
    assert "Assunto:" not in aviso.corpo


@pytest.mark.parametrize("dias", [-7, 1])
def test_nome_que_ja_termina_em_ponto_nao_gera_ponto_duplo(dias):
    # Regressao do ensaio com dados reais: "LTDA." + "." virava "LTDA..".
    aviso = renderizar_aviso(TEMPLATES, dias, **{**DADOS, "nome_cliente": "COMERCIAL AURORA LTDA."})

    assert "Olá, COMERCIAL AURORA LTDA." in aviso.corpo
    assert ".." not in aviso.corpo.splitlines()[0]


def test_aviso_vencido_e_cordial_e_tem_a_frase_obrigatoria():
    aviso = renderizar_aviso(TEMPLATES, 1, **DADOS)

    assert "caso já tenha efetuado o pagamento, desconsidere esta mensagem." in aviso.corpo.casefold()
    assert "Olá, Cliente Sintetico Ltda." in aviso.corpo


@pytest.mark.parametrize("dias", [-7, 1, 3])
def test_templates_do_projeto_passam_na_validacao(dias):
    arquivo = TEMPLATES / nome_do_template(dias)

    validar_template(arquivo.read_text(encoding="utf-8"), vencido=dias > 0)


def test_rejeita_variavel_fora_da_lista_permitida():
    with pytest.raises(ErroTemplate, match="nao permitidas: email_cliente"):
        validar_template(BOM + "{{ email_cliente }}", vencido=False)


def test_rejeita_acesso_a_atributos_perigosos(tmp_path):
    (tmp_path / "aviso_previo.txt.j2").write_text(
        BOM + "{{ nome_cliente.__class__.__mro__ }}", encoding="utf-8"
    )

    with pytest.raises(Exception):  # sandbox bloqueia; StrictUndefined falha
        renderizar_aviso(tmp_path, -7, **DADOS)


@pytest.mark.parametrize(
    "termo",
    ["juros de 2% ao dia", "multa por atraso", "desconto de 10%", "vamos protestar o título",
     "seu nome será negativado", "cobrança judicial", "pague IMEDIATAMENTE", "última chance",
     "garantimos prazo", "Inadimplência", "isto é uma ameaça", "vamos ameaçar", "PROTESTO em cartório"],
)
def test_rejeita_termos_proibidos_com_ou_sem_acento_e_caixa(termo):
    with pytest.raises(ErroTemplate, match="termo proibido"):
        validar_template(BOM + termo + "\n", vencido=False)


def test_vencido_sem_a_frase_obrigatoria_e_rejeitado():
    with pytest.raises(ErroTemplate, match="deve conter a frase"):
        validar_template(BOM, vencido=True)


def test_template_sem_linha_de_assunto_e_rejeitado():
    with pytest.raises(ErroTemplate, match="Assunto"):
        validar_template("Olá, {{ nome_cliente }}\n", vencido=False)


def test_template_com_sintaxe_quebrada_e_rejeitado():
    with pytest.raises(ErroTemplate, match="sintaxe"):
        validar_template(BOM + "{% if %}", vencido=False)


def test_template_ausente(tmp_path):
    with pytest.raises(ErroTemplate, match="nao encontrado"):
        renderizar_aviso(tmp_path, -7, **DADOS)


def test_mensagem_e_eml_em_dry_run(tmp_path):
    aviso = renderizar_aviso(TEMPLATES, 1, **DADOS)
    mensagem = montar_mensagem(
        aviso, remetente="financeiro@example.com", destinatario="cobranca@example.com",
        bcc="controle@example.com",
    )

    destino = gravar_eml(mensagem, tmp_path / "saida", "***555460_D+1_2026-11-23")

    assert destino.parent == (tmp_path / "saida").resolve()
    lido = message_from_bytes(destino.read_bytes(), policy=default)
    assert lido["To"] == "cobranca@example.com"
    assert lido["Bcc"] == "controle@example.com"
    assert lido["Subject"] == "Boleto com vencimento em 20/11/2026"
    assert "R$ 1.234,56" in lido.get_content()


def test_eml_nunca_escapa_da_pasta_de_saida(tmp_path):
    aviso = renderizar_aviso(TEMPLATES, -7, **DADOS)
    mensagem = montar_mensagem(aviso, remetente="a@example.com", destinatario="b@example.com")

    destino = gravar_eml(mensagem, tmp_path / "saida", "../../fora/evil")

    assert destino.parent == (tmp_path / "saida").resolve()
    assert not (tmp_path / "fora").exists()


def test_nao_aceita_quebra_de_linha_em_cabecalho():
    aviso = emailer.Aviso(assunto="ok", corpo="corpo")

    with pytest.raises(ValueError):
        montar_mensagem(aviso, remetente="a@example.com", destinatario="b@example.com\nBcc: x@example.com")


def test_carrega_env_com_precedencia_do_ambiente(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comentario\nSMTP_HOST=smtp.example.com\nSMTP_PORT="587"\nEMAIL_REMETENTE=a@example.com\n', encoding="utf-8")
    monkeypatch.setenv("SMTP_HOST", "outro.example.com")

    valores = carregar_env(env)

    assert valores["SMTP_HOST"] == "outro.example.com"
    assert valores["SMTP_PORT"] == "587"


class SMTPFalso:
    chamadas: list = []

    def __init__(self, host, porta, timeout=None, context=None):
        SMTPFalso.chamadas.append(("conectar", host, porta))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def starttls(self, context=None):
        SMTPFalso.chamadas.append(("starttls",))

    def login(self, usuario, senha):
        SMTPFalso.chamadas.append(("login", usuario))

    def send_message(self, mensagem):
        SMTPFalso.chamadas.append(("enviar", mensagem["To"]))


@pytest.fixture
def mensagem():
    aviso = emailer.Aviso(assunto="Assunto", corpo="Corpo")
    return montar_mensagem(aviso, remetente="a@example.com", destinatario="b@example.com")


def test_envio_smtp_usa_starttls_e_login(monkeypatch, mensagem):
    SMTPFalso.chamadas = []
    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)

    enviar_smtp(mensagem, {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
                           "SMTP_USER": "u", "SMTP_PASS": "p"})

    assert SMTPFalso.chamadas == [
        ("conectar", "smtp.example.com", 587), ("starttls",), ("login", "u"), ("enviar", "b@example.com")
    ]


def test_envio_na_porta_465_usa_ssl(monkeypatch, mensagem):
    SMTPFalso.chamadas = []
    monkeypatch.setattr(smtplib, "SMTP_SSL", SMTPFalso)

    enviar_smtp(mensagem, {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465"})

    assert ("starttls",) not in SMTPFalso.chamadas
    assert SMTPFalso.chamadas[-1] == ("enviar", "b@example.com")


def test_smtp_indisponivel_vira_erro_de_envio_sem_vazar_dados(monkeypatch, mensagem):
    def falha(*_, **__):
        raise ConnectionRefusedError("conexao recusada para b@example.com")

    monkeypatch.setattr(smtplib, "SMTP", falha)

    with pytest.raises(ErroEnvio) as erro:
        enviar_smtp(mensagem, {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587"})

    assert "ConnectionRefusedError" in str(erro.value)
    assert "b@example.com" not in str(erro.value)


def test_smtp_sem_configuracao(mensagem):
    with pytest.raises(ErroEnvio, match="nao configurados"):
        enviar_smtp(mensagem, {})
