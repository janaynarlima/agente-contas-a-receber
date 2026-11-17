"""CLI: aprovar e reabrir. Entradas simuladas; sem e-mail real."""

import smtplib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import recebiveis.cli as cli
from recebiveis.cli import main
from recebiveis.fila import enfileirar
from recebiveis.avisos import FILA_APROVACAO, Decisao
from tests.conftest import CNPJ

HOJE = date(2026, 11, 23)


@pytest.fixture
def cenario(ambiente):
    """Config em arquivo + cadastro + .env + um aviso D+1 na fila."""
    a = ambiente
    p = a.tmp
    (p / "clientes.csv").write_text(
        f"cnpj,nome,email_cobranca\n{CNPJ},Cliente Sintetico Ltda,cobranca@example.com\n", encoding="utf-8"
    )
    (p / ".env").write_text(
        "EMAIL_REMETENTE=financeiro@example.com\nSMTP_HOST=smtp.example.com\nSMTP_PORT=587\n", encoding="utf-8"
    )
    c = a.config
    (p / "config.yaml").write_text(
        "\n".join([
            f'entrada: "{c.entrada.as_posix()}"',
            "avisos: [-7, 1, 3]",
            "confianca_minima: 0.8",
            "aprovacao_vencidos: true",
            "fase_validacao_concluida: false",
            "max_percentual_baixas: 30",
            "max_envios_por_execucao: 50",
            "feriados: []",
            f'banco: "{c.banco.as_posix()}"',
            f'saida: "{c.saida.as_posix()}"',
            f'fila_aprovacao: "{c.fila_aprovacao.as_posix()}"',
            f'templates: "{c.templates.as_posix()}"',
            f'clientes: "{(p / "clientes.csv").as_posix()}"',
            'bcc_financeiro: ""',
        ]),
        encoding="utf-8",
    )
    enfileirar(a.estado, c, Decisao(a.boleto_id, "D+1", date(2026, 11, 23), FILA_APROVACAO),
               a.clientes, HOJE, a.env)
    return a


def rodar(cenario, argumentos, respostas=()):
    p = cenario.tmp
    linhas: list[str] = []
    fila_respostas = iter(respostas)
    codigo = main(
        [*argumentos, "--config", str(p / "config.yaml"), "--env", str(p / ".env")],
        entrada=lambda _prompt: next(fila_respostas),
        saida=linhas.append,
        hoje=HOJE,
    )
    return codigo, "\n".join(linhas)


def _status(cenario, tipo="D+1"):
    return cenario.estado.obter_aviso(cenario.boleto_id, tipo)["status"]


def test_sem_fila_nao_ha_o_que_aprovar(cenario):
    cenario.estado.rejeitar_aviso(cenario.boleto_id, "D+1", HOJE, "x", "teste")

    codigo, texto = rodar(cenario, ["aprovar"])

    assert codigo == 0 and "Nenhum aviso aguardando aprovacao" in texto


def test_listar_mostra_cliente_valor_vencimento_dias_na_pasta_e_historico(cenario):
    codigo, texto = rodar(cenario, ["aprovar", "--listar"])

    assert codigo == 0
    assert "Cliente Sintetico Ltda" in texto
    assert "R$ 1.234,56" in texto
    assert "20/11/2026" in texto
    assert "Dias na pasta:  13" in texto  # visto em 10/11, hoje 23/11
    assert "D-7: pendente" in texto and "D+1: aguardando aprovacao" in texto
    assert _status(cenario) == "aguardando_aprovacao"  # listar nao decide


def test_a_tela_mascara_cnpj_e_email(cenario):
    _, texto = rodar(cenario, ["aprovar", "--listar"])

    assert "12.345.***/****-95" in texto
    assert "co***@example.com" in texto
    assert "12345678000195" not in texto
    assert "cobranca@example.com" not in texto


def test_aprovar_sem_enviar_e_dry_run(cenario, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("dry-run nao pode usar SMTP"))

    codigo, texto = rodar(cenario, ["aprovar"], respostas=["a"])

    assert codigo == 0
    assert "Dry-run" in texto and "nada enviado" in texto
    assert _status(cenario) == "aprovado"
    arquivos = list(cenario.config.saida.glob("*.eml"))
    assert len(arquivos) == 1


def test_aprovar_com_enviar_envia_de_verdade_e_registra_o_aprovador(cenario, monkeypatch):
    enviados = []

    class SMTPFalso:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def starttls(self, context=None): pass
        def login(self, *a): pass
        def send_message(self, mensagem): enviados.append(mensagem["To"])

    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)
    monkeypatch.setattr(
        cli, "varrer_entrada",
        lambda *_a, **_k: SimpleNamespace(identificados={cenario.boleto_id: object()}),
    )

    codigo, texto = rodar(cenario, ["aprovar", "--enviar"], respostas=["a"])

    assert codigo == 0 and "ENVIADO" in texto
    assert enviados == ["cobranca@example.com"]
    aviso = cenario.estado.obter_aviso(cenario.boleto_id, "D+1")
    assert aviso["status"] == "enviado" and aviso["aprovado_por"]


def test_enviar_nao_envia_se_o_arquivo_sumiu_da_entrada(cenario, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("nao deveria enviar"))
    monkeypatch.setattr(cli, "varrer_entrada", lambda *_a, **_k: SimpleNamespace(identificados={}))

    _, texto = rodar(cenario, ["aprovar", "--enviar"], respostas=["a"])

    assert "removido antes do envio" in texto
    assert _status(cenario) == "aprovado"


def test_rejeitar_cancela_apaga_o_email_da_fila_e_guarda_o_motivo(cenario):
    fila = Path(cenario.estado.obter_aviso(cenario.boleto_id, "D+1")["arquivo_fila"])
    assert fila.exists()

    _, texto = rodar(cenario, ["aprovar"], respostas=["r", "cliente ja pagou"])

    assert "Rejeitado" in texto
    aviso = cenario.estado.obter_aviso(cenario.boleto_id, "D+1")
    assert aviso["status"] == "cancelado" and aviso["motivo"] == "rejeitado: cliente ja pagou"
    assert not fila.exists()


def test_pular_e_sair_nao_alteram_nada(cenario):
    rodar(cenario, ["aprovar"], respostas=["p"])
    assert _status(cenario) == "aguardando_aprovacao"
    rodar(cenario, ["aprovar"], respostas=["s"])
    assert _status(cenario) == "aguardando_aprovacao"


def test_opcao_invalida_pergunta_de_novo(cenario):
    _, texto = rodar(cenario, ["aprovar"], respostas=["x", "?", "p"])

    assert texto.count("Opcao invalida") == 2


def test_config_invalida_retorna_erro(cenario):
    p = cenario.tmp
    (p / "config.yaml").write_text("entrada: x\n", encoding="utf-8")

    codigo, texto = rodar(cenario, ["aprovar"])

    assert codigo == 2 and "Erro de configuracao" in texto


def test_reabrir_boleto_baixado(cenario):
    cenario.estado.marcar_pago(cenario.boleto_id, HOJE, "arquivo removido da pasta")

    codigo, texto = rodar(cenario, ["reabrir", cenario.boleto_id[-8:]])

    assert codigo == 0 and "reaberto" in texto
    boleto = cenario.estado.obter_boleto(cenario.boleto_id)
    assert boleto["status"] == "aberto" and boleto["requer_revisao"] == 1


def test_reabrir_exige_identificador_longo_e_unico(cenario):
    codigo, texto = rodar(cenario, ["reabrir", "123"])
    assert codigo == 2 and "6 caracteres" in texto

    codigo, texto = rodar(cenario, ["reabrir", "000000"])
    assert codigo == 2 and "exatamente um boleto" in texto


class _SMTPTeste:
    enviados: list = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def starttls(self, context=None): pass
    def login(self, *a): pass
    def send_message(self, mensagem): _SMTPTeste.enviados.append((mensagem["To"], mensagem["Subject"]))


def _env(cenario, conteudo):
    (cenario.tmp / ".env").write_text(conteudo, encoding="utf-8")


def test_testar_email_envia_um_unico_email_so_para_o_endereco_pedido(cenario, monkeypatch):
    _SMTPTeste.enviados = []
    monkeypatch.setattr(smtplib, "SMTP", _SMTPTeste)
    _env(cenario, "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nEMAIL_REMETENTE=financeiro@example.com\n")

    codigo, texto = rodar(cenario, ["testar-email", "--para", "dono@example.com"])

    assert codigo == 0
    assert _SMTPTeste.enviados == [("dono@example.com", "Teste do agente de recebiveis")]
    assert "do***@example.com" in texto and "dono@example.com" not in texto


def test_testar_email_nao_toca_na_fila_nem_nos_clientes(cenario, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _SMTPTeste)
    _env(cenario, "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nEMAIL_REMETENTE=financeiro@example.com\n")

    rodar(cenario, ["testar-email", "--para", "dono@example.com"])

    assert _status(cenario) == "aguardando_aprovacao"


def test_testar_email_exige_configuracao_e_endereco_valido(cenario, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("nao deveria enviar"))
    _env(cenario, "SMTP_PORT=587\n")

    codigo, texto = rodar(cenario, ["testar-email", "--para", "dono@example.com"])
    assert codigo == 2 and "SMTP_HOST" in texto and "EMAIL_REMETENTE" in texto

    codigo, texto = rodar(cenario, ["testar-email", "--para", "sem-arroba"])
    assert codigo == 2 and "invalido" in texto


def test_testar_email_com_smtp_indisponivel_nao_vaza_o_endereco(cenario, monkeypatch):
    def falha(*_a, **_k):
        raise ConnectionRefusedError("recusado")

    monkeypatch.setattr(smtplib, "SMTP", falha)
    _env(cenario, "SMTP_HOST=smtp.example.com\nSMTP_PORT=587\nEMAIL_REMETENTE=financeiro@example.com\n")

    codigo, texto = rodar(cenario, ["testar-email", "--para", "dono@example.com"])

    assert codigo == 1 and "Falha no envio" in texto and "dono@example.com" not in texto


def test_reabrir_pelo_id_curto_mostrado_na_tela(cenario):
    from recebiveis.fila import id_curto

    cenario.estado.marcar_pago(cenario.boleto_id, HOJE, "arquivo removido da pasta")

    codigo, texto = rodar(cenario, ["reabrir", id_curto(cenario.boleto_id)])

    assert codigo == 0 and f"#{id_curto(cenario.boleto_id)}" in texto


def test_id_curto_distingue_boletos_com_o_mesmo_final():
    from recebiveis.fila import id_curto
    from recebiveis.reconciliacao import mascarar_id

    a = "23791" + "1" * 33 + "555460"
    b = "23791" + "2" * 33 + "555460"  # mesmo final, como nos boletos reais do mesmo beneficiario

    assert id_curto(a) != id_curto(b)
    assert mascarar_id(a) != mascarar_id(b)


def test_reabrir_boleto_que_nao_esta_pago(cenario):
    codigo, texto = rodar(cenario, ["reabrir", cenario.boleto_id[-8:]])

    assert codigo == 1 and "nao esta pago" in texto
