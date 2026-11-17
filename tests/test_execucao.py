"""Execucao completa (run). Arquivos e dados sinteticos; SMTP simulado."""

import json
import smtplib
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import recebiveis.cli as cli
from recebiveis.cli import main
from recebiveis.execucao import executar, gerar_relatorio, gravar_relatorio
from recebiveis.extrator import DadosBoleto
from recebiveis.logs import LogJson, mascarar_texto
from recebiveis.versao import versao_codigo, versao_prompt
from tests.conftest import CNPJ, criar_config
from tests.test_febraban import fator_de, gerar

VENCIMENTO = date(2026, 11, 20)
D_MENOS_7 = date(2026, 11, 13)  # primeiro dia do aviso D-7
EMAIL = "cobranca@example.com"


def barras(n: int) -> str:
    return gerar(fator_de(VENCIMENTO), 1000 * n)[0]


def ler(caminho: Path) -> DadosBoleto:
    numero = int(caminho.read_text(encoding="utf-8").split(":")[1])
    return DadosBoleto(CNPJ, "Texto do Boleto", Decimal(10 * numero), VENCIMENTO, barras(numero))


class SMTPFalso:
    enviados: list = []

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def starttls(self, context=None): pass
    def login(self, *a): pass
    def send_message(self, mensagem): SMTPFalso.enviados.append(mensagem["To"])


@pytest.fixture
def cena(tmp_path, monkeypatch):
    SMTPFalso.enviados = []
    config = criar_config(tmp_path)
    config.entrada.mkdir()
    for n in (1, 2, 3, 4):
        (config.entrada / f"b{n}.pdf").write_text(f"ID:{n}", encoding="utf-8")
    config.clientes.write_text(f"cnpj,nome,email_cobranca\n{CNPJ},Cliente Sintetico Ltda,{EMAIL}\n", encoding="utf-8")
    env = {"EMAIL_REMETENTE": "financeiro@example.com", "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587"}
    monkeypatch.setattr(smtplib, "SMTP", SMTPFalso)

    class Cena:
        pass

    c = Cena()
    c.config, c.env, c.tmp = config, env, tmp_path
    return c


def rodar(cena, hoje=D_MENOS_7, *, enviar=False, presenca=lambda _id: True, config=None):
    cfg = config or cena.config
    log = LogJson(cfg.logs / f"execucao-{hoje}.jsonl", versao_codigo="t", versao_prompt="t")
    execucao = executar(cfg, cena.env, hoje, enviar=enviar, sem_llm=True, leitor=ler,
                        presenca=presenca, log=log)
    return execucao, log


def liberar_d7(cena):
    return replace(cena.config, fase_validacao_concluida=True)


def test_primeiro_dia_registra_boletos_e_enfileira_o_d_menos_7(cena):
    execucao, _ = rodar(cena)

    assert len(execucao.reconciliacao.novos) == 4
    assert execucao.decisoes["fila_aprovacao"] == 4
    assert execucao.fila["enfileirado"] == 4
    assert execucao.codigo_saida == 0
    assert len(list(cena.config.fila_aprovacao.glob("*.eml"))) == 4
    assert execucao.entregas == {}  # nada foi entregue


def test_execucao_repetida_no_mesmo_dia_nao_enfileira_de_novo(cena):
    rodar(cena)

    segunda, _ = rodar(cena)

    assert segunda.decisoes == {} and segunda.fila == {}
    assert len(list(cena.config.fila_aprovacao.glob("*.eml"))) == 4


def test_dry_run_com_d7_liberado_gera_eml_e_nao_envia(cena):
    execucao, _ = rodar(cena, config=liberar_d7(cena))

    assert execucao.entregas["dry-run"] == 4
    assert len(list(cena.config.saida.glob("*.eml"))) == 4
    assert SMTPFalso.enviados == []


def test_envio_real_so_com_enviar_e_nao_duplica(cena):
    config = liberar_d7(cena)

    primeira, _ = rodar(cena, enviar=True, config=config)
    segunda, _ = rodar(cena, enviar=True, config=config)

    assert primeira.entregas["enviado"] == 4
    assert SMTPFalso.enviados == [EMAIL] * 4
    assert segunda.entregas == {}  # execucao repetida: nada novo a enviar


def test_aprovados_pendentes_saem_no_run_com_enviar(cena):
    from recebiveis.estado import Estado

    rodar(cena)
    with Estado(cena.config.banco) as estado:
        for aviso in estado.listar_avisos("aguardando_aprovacao"):
            estado.aprovar_aviso(aviso["boleto_id"], aviso["tipo_aviso"], D_MENOS_7, "maria")

    dry, _ = rodar(cena)  # sem --enviar: so gera .eml
    real, _ = rodar(cena, enviar=True)

    assert dry.entregas["dry-run"] == 4 and SMTPFalso.enviados == [EMAIL] * 4 == SMTPFalso.enviados[:4]
    assert real.entregas["enviado"] == 4


def test_limite_de_envios_por_execucao(cena):
    config = replace(liberar_d7(cena), max_envios_por_execucao=1)

    execucao, _ = rodar(cena, config=config)

    assert execucao.entregas["dry-run"] == 1
    assert execucao.decisoes["adiado"] >= 1


def test_arquivo_removido_antes_do_envio_nao_envia(cena):
    execucao, _ = rodar(cena, enviar=True, presenca=lambda _id: False, config=liberar_d7(cena))

    assert SMTPFalso.enviados == []
    assert execucao.entregas["bloqueado"] == 4
    assert execucao.codigo_saida == 1


def test_smtp_indisponivel_registra_erro_e_mantem_o_aviso(cena, monkeypatch):
    def falha(*_a, **_k):
        raise ConnectionRefusedError("recusado")

    monkeypatch.setattr(smtplib, "SMTP", falha)

    execucao, _ = rodar(cena, enviar=True, config=liberar_d7(cena))

    assert execucao.entregas["falha"] == 4
    assert execucao.codigo_saida == 1
    assert any("falha no envio SMTP" in e for e in execucao.erros)
    assert EMAIL not in " ".join(execucao.erros)


def test_boleto_pago_e_baixado_antes_de_calcular_os_avisos(cena):
    rodar(cena)  # registra e enfileira os 4
    (cena.config.entrada / "b4.pdf").unlink()

    execucao, _ = rodar(cena, date(2026, 11, 14))

    assert len(execucao.reconciliacao.baixados) == 1
    assert execucao.reconciliacao.avisos_cancelados >= 1  # D-7 em aprovacao + pendentes
    assert len(list(cena.config.fila_aprovacao.glob("*.eml"))) == 3  # fila do pago foi limpa


def test_reconciliacao_abortada_nao_calcula_avisos(cena, tmp_path):
    config = replace(cena.config, entrada=tmp_path / "nao_existe")

    execucao, _ = rodar(cena, config=config)

    assert execucao.reconciliacao.abortada
    assert not execucao.avisos_executados
    assert execucao.decisoes == {}
    assert execucao.codigo_saida == 1
    assert "reconciliacao abortada" in execucao.avisos_nao_executados_motivo


def test_baixas_bloqueadas_nao_calculam_avisos(cena):
    rodar(cena)
    (cena.config.entrada / "b1.pdf").unlink()
    (cena.config.entrada / "b2.pdf").unlink()  # 50% > 30%

    execucao, _ = rodar(cena, date(2026, 11, 14))

    assert execucao.reconciliacao.precisa_confirmacao
    assert not execucao.avisos_executados
    assert "baixas bloqueadas" in execucao.avisos_nao_executados_motivo
    assert execucao.decisoes == {}


def test_cadastro_de_clientes_ausente_nao_manda_avisos_para_revisao(cena):
    cena.config.clientes.unlink()

    execucao, _ = rodar(cena)

    assert not execucao.avisos_executados
    assert execucao.codigo_saida == 1
    assert any("cadastro de clientes" in e for e in execucao.erros)
    assert len(execucao.reconciliacao.novos) == 4  # a reconciliacao continuou


def test_log_json_tem_rastreio_versoes_e_nao_tem_dados_pessoais(cena):
    _, log = rodar(cena, config=liberar_d7(cena), enviar=True)

    texto = log.caminho.read_text(encoding="utf-8")
    linhas = [json.loads(linha) for linha in texto.splitlines()]
    eventos = {linha["evento"] for linha in linhas}

    assert {"entrada", "extracao", "validacao", "decisao", "envio", "fim"} <= eventos
    assert len({linha["execucao_id"] for linha in linhas}) == 1
    assert all(linha["versao_codigo"] == "t" and linha["versao_prompt"] == "t" for linha in linhas)
    assert EMAIL not in texto and CNPJ not in texto and barras(1) not in texto
    assert "Cliente Sintetico" not in texto and "Texto do Boleto" not in texto


def test_relatorio_tem_as_secoes_e_nao_afirma_desempenho_sem_medicao(cena):
    rodar(cena)
    (cena.config.entrada / "b4.pdf").unlink()
    execucao, _ = rodar(cena, date(2026, 11, 14))

    texto = gerar_relatorio(execucao, cena.config)

    for secao in ("Boletos processados", "Baixas", "Avisos", "Revisões humanas pendentes", "Erros", "Métricas"):
        assert f"## {secao}" in texto
    assert "DRY-RUN" in texto
    assert "Boletos baixados (arquivo removido da pasta): 1" in texto
    assert "Avisos cancelados por pagamento:" in texto
    assert "Tempo médio de leitura por boleto" in texto
    assert "golden set" in texto
    assert EMAIL not in texto and CNPJ not in texto
    caminho = gravar_relatorio(texto, cena.tmp / "relatorio.md")
    assert caminho.read_text(encoding="utf-8") == texto


def test_mascara_de_defesa_no_log():
    assert "12.345.***/****-95" in mascarar_texto("cnpj 12.345.678/0001-95")
    assert "12.345.***/****-95" in mascarar_texto("cnpj 12345678000195")
    assert "jo***@empresa.com.br" in mascarar_texto("contato joao@empresa.com.br")
    assert "***" + "9" * 6 in mascarar_texto("id " + "7" * 38 + "9" * 6) and "7" * 38 not in mascarar_texto("id " + "7" * 38 + "9" * 6)


def test_versoes():
    assert versao_codigo().startswith("0.4.0+") and len(versao_codigo()) == len("0.4.0+") + 8
    assert len(versao_prompt()) == 12
    assert versao_prompt(Path("nao_existe")) == "indisponivel"


def test_cli_run_dry_run_por_padrao(cena, monkeypatch):
    p = cena.tmp
    c = cena.config
    (p / ".env").write_text("EMAIL_REMETENTE=financeiro@example.com\n", encoding="utf-8")
    (p / "config.yaml").write_text("\n".join([
        f'entrada: "{c.entrada.as_posix()}"', "avisos: [-7, 1, 3]", "confianca_minima: 0.8",
        "aprovacao_vencidos: true", "fase_validacao_concluida: false", "max_percentual_baixas: 30",
        "max_envios_por_execucao: 50", "feriados: []", f'banco: "{c.banco.as_posix()}"',
        f'saida: "{c.saida.as_posix()}"', f'fila_aprovacao: "{c.fila_aprovacao.as_posix()}"',
        f'logs: "{c.logs.as_posix()}"', f'relatorio: "{(p / "relatorio.md").as_posix()}"',
        f'templates: "{c.templates.as_posix()}"', f'clientes: "{c.clientes.as_posix()}"', 'bcc_financeiro: ""',
    ]), encoding="utf-8")
    monkeypatch.setattr(cli, "leitor_padrao", lambda *_a, **_k: ler)
    linhas: list[str] = []

    codigo = main(["run", "--sem-llm", "--config", str(p / "config.yaml"), "--env", str(p / ".env")],
                  saida=linhas.append, hoje=D_MENOS_7)

    texto = "\n".join(linhas)
    assert codigo == 0
    assert "DRY-RUN" in texto and "--sem-llm" in texto
    assert "novos: 4" in texto
    assert (p / "relatorio.md").exists()
    assert any(c.logs.glob("execucao-*.jsonl"))
    assert SMTPFalso.enviados == []
