"""Testes do front-end (Flask). Arquivos sinteticos; leitor falso, sem OCR."""

import io
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import recebiveis.web as web
from recebiveis.config import Config
from recebiveis.extrator import DadosBoleto

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


def pdf(numero: int) -> bytes:
    return b"%PDF-1.4\nID:" + str(numero).encode() + b"\n"


def ler(caminho: Path) -> DadosBoleto:
    numero = int(caminho.read_bytes().split(b"ID:")[1].split()[0])
    return DadosBoleto(
        "12345678000195", "Cliente Sintetico", None, date(2026, 11, 20),
        f"23791{numero:039d}",
    )


@pytest.fixture
def cliente(tmp_path):
    config = replace(CONFIG, entrada=tmp_path / "entrada", banco=tmp_path / "dados" / "estado.db")
    app = web.criar_app(config, leitor=ler)
    app.config["TESTING"] = True
    with app.test_client() as c:
        with c.session_transaction() as sessao:
            sessao["csrf"] = "token-de-teste"
        c.entrada = config.entrada
        yield c


def enviar(cliente, arquivos, processar=True, **extras):
    dados = {"csrf": "token-de-teste", "arquivos": arquivos, **extras}
    if processar:
        dados["processar"] = "1"
    return cliente.post("/enviar", data=dados, content_type="multipart/form-data",
                        follow_redirects=True)


def arquivo(nome: str, conteudo: bytes):
    return (io.BytesIO(conteudo), nome)


def test_pagina_inicial_e_cabecalhos_de_seguranca(cliente):
    resposta = cliente.get("/")

    assert resposta.status_code == 200
    assert "Enviar boletos".encode() in resposta.data
    assert "default-src 'self'" in resposta.headers["Content-Security-Policy"]
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["X-Frame-Options"] == "DENY"
    assert resposta.headers["Cache-Control"] == "no-store"
    assert b"<script>" not in resposta.data  # sem script inline (CSP)


def test_tela_mostra_avisos_de_cada_boleto_e_resumo_da_fila(cliente):
    from recebiveis.estado import Estado

    enviar(cliente, [arquivo("a.pdf", pdf(1))])  # registra o boleto 1
    cfg = cliente.application.extensions["recebiveis_config"]
    boleto_id = f"23791{1:039d}"
    with Estado(cfg.banco) as estado:
        estado.registrar_aviso(boleto_id, "D-7", date(2026, 11, 13))
        estado.registrar_aviso(boleto_id, "D+1", date(2026, 11, 23))
        estado.confirmar_envio(boleto_id, "D-7", date(2026, 11, 13))
        estado.enfileirar_aviso(boleto_id, "D+1", "fila/x.eml", date(2026, 11, 23))

    texto = cliente.get("/").get_data(as_text=True)

    assert "D-7 · enviado" in texto
    assert "D+1 · aguardando aprovação" in texto
    assert "Fila de aprovação" in texto and "python -m recebiveis aprovar" in texto
    assert "Nada é enviado por esta tela" in texto


def test_tela_distingue_boletos_com_o_mesmo_final_do_codigo(cliente):
    from recebiveis.fila import id_curto

    enviar(cliente, [arquivo("a.pdf", pdf(1)), arquivo("b.pdf", pdf(2))])
    texto = cliente.get("/").get_data(as_text=True)

    ids = {id_curto(f"23791{n:039d}") for n in (1, 2)}
    assert len(ids) == 2 and all(f"#{i}" in texto for i in ids)


def test_sem_avisos_a_tela_nao_mostra_a_fila_de_aprovacao(cliente):
    enviar(cliente, [arquivo("a.pdf", pdf(1))])

    texto = cliente.get("/").get_data(as_text=True)

    assert "Fila de aprovação" not in texto


def test_saude(cliente):
    assert cliente.get("/saude").get_json() == {"status": "ok"}


def test_upload_salva_em_entrada_e_processa(cliente):
    resposta = enviar(cliente, [arquivo("boleto1.pdf", pdf(1)), arquivo("boleto2.pdf", pdf(2))])

    assert (cliente.entrada / "boleto1.pdf").read_bytes() == pdf(1)
    assert (cliente.entrada / "boleto2.pdf").exists()
    texto = resposta.get_data(as_text=True)
    assert "2 arquivo(s) enviado(s)" in texto
    assert "2 novo(s)" in texto
    assert "Cliente Sintetico" in texto
    assert "12.345.***/****-95" in texto  # CNPJ mascarado
    assert "12345678000195" not in texto
    assert "12.345.678/0001-95" not in texto


def test_upload_sem_processar_nao_registra_boletos(cliente):
    resposta = enviar(cliente, [arquivo("a.pdf", pdf(1))], processar=False)

    assert (cliente.entrada / "a.pdf").exists()
    assert "Nenhum boleto registrado" in resposta.get_data(as_text=True)


def test_rejeita_extensao_nao_aceita(cliente):
    resposta = enviar(cliente, [arquivo("malware.exe", b"MZ....")])

    assert "formato não aceito" in resposta.get_data(as_text=True)
    assert list(cliente.entrada.iterdir()) == []


def test_rejeita_conteudo_que_nao_corresponde_a_extensao(cliente):
    resposta = enviar(cliente, [arquivo("falso.pdf", b"MZ\x90\x00executavel")])

    assert "conteúdo não corresponde" in resposta.get_data(as_text=True)
    assert list(cliente.entrada.iterdir()) == []


def test_aceita_png_e_jpeg_pelos_bytes_iniciais(cliente):
    png = b"\x89PNG\r\n\x1a\n" + b"dados"
    jpg = b"\xff\xd8\xff\xe0" + b"dados"

    enviar(cliente, [arquivo("a.png", png), arquivo("b.jpg", jpg)], processar=False)

    assert (cliente.entrada / "a.png").exists() and (cliente.entrada / "b.jpg").exists()


def test_nome_com_caminho_nao_escapa_da_pasta_de_entrada(cliente):
    enviar(cliente, [arquivo("../../fora.pdf", pdf(1))], processar=False)

    salvos = list(cliente.entrada.iterdir())
    assert len(salvos) == 1
    assert salvos[0].parent == cliente.entrada
    assert ".." not in salvos[0].name
    assert not (cliente.entrada.parent / "fora.pdf").exists()


def test_nao_sobrescreve_arquivo_com_mesmo_nome_e_conteudo_diferente(cliente):
    enviar(cliente, [arquivo("b.pdf", pdf(1))], processar=False)
    enviar(cliente, [arquivo("b.pdf", pdf(2))], processar=False)

    assert (cliente.entrada / "b.pdf").read_bytes() == pdf(1)
    assert (cliente.entrada / "b (1).pdf").read_bytes() == pdf(2)


def test_conteudo_identico_e_ignorado(cliente):
    enviar(cliente, [arquivo("b.pdf", pdf(1))], processar=False)
    resposta = enviar(cliente, [arquivo("outro nome.pdf", pdf(1))], processar=False)

    assert "arquivo idêntico" in resposta.get_data(as_text=True)
    assert len(list(cliente.entrada.iterdir())) == 1


def test_rejeita_arquivo_acima_do_limite(cliente, monkeypatch):
    monkeypatch.setattr(web, "TAMANHO_MAXIMO_ARQUIVO", 10)

    resposta = enviar(cliente, [arquivo("grande.pdf", pdf(1) + b"x" * 100)])

    assert "maior que" in resposta.get_data(as_text=True)
    assert list(cliente.entrada.iterdir()) == []


def test_envio_sem_arquivo(cliente):
    resposta = cliente.post("/enviar", data={"csrf": "token-de-teste"}, follow_redirects=True)

    assert "Selecione ao menos um arquivo" in resposta.get_data(as_text=True)


def test_post_sem_token_csrf_e_rejeitado(cliente):
    resposta = cliente.post("/enviar", data={"arquivos": arquivo("a.pdf", pdf(1))},
                            content_type="multipart/form-data")

    assert resposta.status_code == 400
    assert list(cliente.entrada.iterdir()) == []


def test_post_com_token_errado_e_rejeitado(cliente):
    resposta = cliente.post("/processar", data={"csrf": "errado"})

    assert resposta.status_code == 400


def test_post_de_outra_origem_e_rejeitado(cliente):
    resposta = cliente.post("/processar", data={"csrf": "token-de-teste"},
                            headers={"Origin": "http://site-malicioso.example"})

    assert resposta.status_code == 400


def test_arquivos_de_entrada_nao_sao_servidos(cliente):
    enviar(cliente, [arquivo("a.pdf", pdf(1))], processar=False)

    for caminho in ("/entrada/a.pdf", "/static/../entrada/a.pdf", "/a.pdf"):
        assert cliente.get(caminho).status_code == 404


def test_processar_com_baixas_acima_do_limite_bloqueia_e_nao_oferece_confirmacao(cliente):
    enviar(cliente, [arquivo("a.pdf", pdf(1)), arquivo("b.pdf", pdf(2))])
    (cliente.entrada / "a.pdf").unlink()
    (cliente.entrada / "b.pdf").unlink()

    resposta = cliente.post("/processar", data={"csrf": "token-de-teste"}, follow_redirects=True)
    texto = resposta.get_data(as_text=True)

    assert "Baixas bloqueadas" in texto
    assert "decisão humana" in texto
    assert "confirmar" not in texto.lower().replace("confirmação exige", "")


def test_pasta_inexistente_mostra_erro_sem_baixar(cliente, tmp_path):
    enviar(cliente, [arquivo("a.pdf", pdf(1))])
    cfg = cliente.application.extensions["recebiveis_config"]
    for arq in cfg.entrada.iterdir():
        arq.unlink()
    cfg.entrada.rmdir()

    resposta = cliente.post("/processar", data={"csrf": "token-de-teste"}, follow_redirects=True)

    assert "abortada" in resposta.get_data(as_text=True)


def test_config_do_ambiente_aplica_sobrescritas(tmp_path, monkeypatch):
    yaml_cfg = Path(__file__).resolve().parent.parent / "config.yaml"
    monkeypatch.setenv("RECEBIVEIS_CONFIG", str(yaml_cfg))
    monkeypatch.setenv("RECEBIVEIS_ENTRADA", str(tmp_path / "in"))
    monkeypatch.setenv("RECEBIVEIS_BANCO", str(tmp_path / "db" / "e.db"))

    config = web.config_do_ambiente()

    assert config.entrada == tmp_path / "in"
    assert config.banco == tmp_path / "db" / "e.db"
    assert config.avisos == (-7, 1, 3)


def test_validar_conteudo():
    assert web.validar_conteudo("a.pdf", b"%PDF-1.7")
    assert not web.validar_conteudo("a.pdf", b"\x89PNG\r\n\x1a\n")
    assert not web.validar_conteudo("a.txt", b"%PDF-1.7")
