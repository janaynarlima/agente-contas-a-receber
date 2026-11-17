from pathlib import Path

import pytest

from recebiveis.clientes import ErroClientes, buscar_cliente, carregar_clientes
from recebiveis.validador import codigo_barras_para_linha, linha_para_codigo_barras

CABECALHO = "cnpj,nome,email_cobranca\n"


def _csv(tmp_path: Path, conteudo: str) -> Path:
    arquivo = tmp_path / "clientes.csv"
    arquivo.write_text(conteudo, encoding="utf-8")
    return arquivo


def test_carrega_e_busca_por_cnpj_formatado_ou_numerico(tmp_path):
    arquivo = _csv(tmp_path, CABECALHO + "12.345.678/0001-95,Cliente Sintetico Ltda,cobranca@example.com\n")

    clientes = carregar_clientes(arquivo)

    assert buscar_cliente(clientes, "12345678000195").nome == "Cliente Sintetico Ltda"
    assert buscar_cliente(clientes, "12.345.678/0001-95").email_cobranca == "cobranca@example.com"
    assert buscar_cliente(clientes, "11222333000181") is None
    assert buscar_cliente(clientes, None) is None


def test_cnpj_de_15_digitos_com_zero_a_esquerda_e_aceito_e_normalizado(tmp_path):
    arquivo = _csv(tmp_path, CABECALHO + "012.345.678/0001-95,Cliente Sintetico,a@example.com\n")

    assert "12345678000195" in carregar_clientes(arquivo)


def test_cnpj_de_15_digitos_ainda_exige_digito_verificador(tmp_path):
    arquivo = _csv(tmp_path, CABECALHO + "012.345.678/0001-90,Cliente Sintetico,a@example.com\n")

    with pytest.raises(ErroClientes, match="CNPJ invalido"):
        carregar_clientes(arquivo)


def test_cnpj_de_15_digitos_sem_zero_inicial_e_rejeitado(tmp_path):
    arquivo = _csv(tmp_path, CABECALHO + "112.345.678/0001-95,Cliente Sintetico,a@example.com\n")

    with pytest.raises(ErroClientes, match="CNPJ invalido"):
        carregar_clientes(arquivo)


def test_cliente_sem_email_e_carregado_com_email_none(tmp_path):
    arquivo = _csv(tmp_path, CABECALHO + "12345678000195,Cliente Sintetico,\n")

    assert carregar_clientes(arquivo)["12345678000195"].email_cobranca is None


def test_aceita_bom_do_excel(tmp_path):
    arquivo = tmp_path / "clientes.csv"
    arquivo.write_bytes(b"\xef\xbb\xbf" + (CABECALHO + "12345678000195,Cliente,a@example.com\n").encode())

    assert "12345678000195" in carregar_clientes(arquivo)


@pytest.mark.parametrize(
    "linha,mensagem",
    [
        ("12345678000190,Cliente,a@example.com", "CNPJ invalido"),
        ("123,Cliente,a@example.com", "CNPJ invalido"),
        ("12345678000195,,a@example.com", "nome"),
        ("12345678000195,Cliente,sem-arroba", "e-mail invalido"),
        ("12345678000195,Cliente,a@example.com\n12345678000195,Outro,b@example.com", "repetido"),
    ],
)
def test_rejeita_linhas_invalidas_sem_expor_dados(tmp_path, linha, mensagem):
    arquivo = _csv(tmp_path, CABECALHO + linha + "\n")

    with pytest.raises(ErroClientes, match=mensagem) as erro:
        carregar_clientes(arquivo)

    assert "linha" in str(erro.value)
    assert "example.com" not in str(erro.value)
    assert "12345678" not in str(erro.value)


def test_cabecalho_incorreto_e_arquivo_ausente(tmp_path):
    with pytest.raises(ErroClientes, match="cabecalho"):
        carregar_clientes(_csv(tmp_path, "cnpj,nome\n"))
    with pytest.raises(ErroClientes, match="nao encontrado"):
        carregar_clientes(tmp_path / "nao_existe.csv")


def test_exemplo_do_projeto_so_tem_cabecalho():
    exemplo = Path(__file__).resolve().parent.parent / "clientes.exemplo.csv"

    assert carregar_clientes(exemplo) == {}


def test_codigo_de_barras_volta_a_linha_digitavel():
    from tests.test_febraban import gerar  # gerador sintetico independente

    barras, linha = gerar(1666, 123456)

    assert codigo_barras_para_linha(barras) == linha
    assert linha_para_codigo_barras(codigo_barras_para_linha(barras)) == barras
