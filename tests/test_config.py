from datetime import date
from pathlib import Path

import pytest

from recebiveis.config import ErroConfig, carregar_config, nome_tipo_aviso

BASE = """entrada: entrada
avisos:
  - -7
  - 1
  - 3
confianca_minima: 0.8
aprovacao_vencidos: true
fase_validacao_concluida: false
max_percentual_baixas: 30
max_envios_por_execucao: 50
feriados:
  - 2026-12-25
banco: "{banco}"
"""


def _escrever(tmp_path: Path, texto: str) -> Path:
    arquivo = tmp_path / "config.yaml"
    arquivo.write_text(texto, encoding="utf-8")
    return arquivo


def _valido(tmp_path: Path, **substituicoes) -> str:
    texto = BASE.format(banco=(tmp_path / "e.db").as_posix())
    for antigo, novo in substituicoes.items():
        texto = texto.replace(antigo.replace("__", " "), novo)
    return texto


def test_carrega_configuracao_valida(tmp_path):
    config = carregar_config(_escrever(tmp_path, _valido(tmp_path)))

    assert config.avisos == (-7, 1, 3)
    assert config.confianca_minima == 0.8
    assert config.aprovacao_vencidos is True
    assert config.fase_validacao_concluida is False
    assert config.max_percentual_baixas == 30
    assert config.max_envios_por_execucao == 50
    assert config.feriados == frozenset({date(2026, 12, 25)})


def test_dias_de_aviso_vem_do_arquivo_e_nao_do_codigo(tmp_path):
    texto = _valido(tmp_path).replace("  - -7\n  - 1\n  - 3\n", "  - -2\n  - 5\n")

    assert carregar_config(_escrever(tmp_path, texto)).avisos == (-2, 5)


def test_nome_do_tipo_de_aviso():
    assert nome_tipo_aviso(-7) == "D-7"
    assert nome_tipo_aviso(1) == "D+1"
    assert nome_tipo_aviso(3) == "D+3"


@pytest.mark.parametrize(
    "antigo,novo,mensagem",
    [
        ("  - 1\n", "  - 0\n", "nao pode conter 0"),
        ("  - 1\n", "  - -7\n", "repetidos"),
        ("  - 1\n", "  - 1.5\n", "apenas inteiros"),
        ("confianca_minima: 0.8", "confianca_minima: 1.5", "confianca_minima"),
        ("aprovacao_vencidos: true", "aprovacao_vencidos: sim", "aprovacao_vencidos"),
        ("max_percentual_baixas: 30", "max_percentual_baixas: 130", "max_percentual_baixas"),
        ("max_envios_por_execucao: 50", "max_envios_por_execucao: 0", "max_envios_por_execucao"),
        ("2026-12-25", "25/12/2026", "feriado invalido"),
    ],
)
def test_rejeita_valores_invalidos(tmp_path, antigo, novo, mensagem):
    texto = _valido(tmp_path).replace(antigo, novo)

    with pytest.raises(ErroConfig, match=mensagem):
        carregar_config(_escrever(tmp_path, texto))


def test_avisos_nao_tem_valor_padrao(tmp_path):
    texto = _valido(tmp_path).replace("avisos:\n  - -7\n  - 1\n  - 3\n", "")

    with pytest.raises(ErroConfig, match="avisos"):
        carregar_config(_escrever(tmp_path, texto))


def test_aprovacao_de_vencidos_e_obrigatoria_no_arquivo(tmp_path):
    texto = _valido(tmp_path).replace("aprovacao_vencidos: true\n", "")

    with pytest.raises(ErroConfig, match="aprovacao_vencidos"):
        carregar_config(_escrever(tmp_path, texto))


def test_arquivo_inexistente(tmp_path):
    with pytest.raises(ErroConfig, match="nao encontrado"):
        carregar_config(tmp_path / "nao_existe.yaml")


def test_expande_variavel_de_ambiente_no_banco(tmp_path, monkeypatch):
    monkeypatch.setenv("PASTA_TESTE", str(tmp_path))
    texto = _valido(tmp_path).replace((tmp_path / "e.db").as_posix(), "${PASTA_TESTE}/x.db")

    config = carregar_config(_escrever(tmp_path, texto))

    assert config.banco == Path(str(tmp_path) + "/x.db")


def test_variaveis_de_ambiente_sobrescrevem_todos_os_caminhos(tmp_path, monkeypatch):
    arquivo = _escrever(tmp_path, _valido(tmp_path))
    mapa = {"RECEBIVEIS_ENTRADA": "entrada", "RECEBIVEIS_BANCO": "banco", "RECEBIVEIS_SAIDA": "saida",
            "RECEBIVEIS_FILA": "fila_aprovacao", "RECEBIVEIS_LOGS": "logs",
            "RECEBIVEIS_RELATORIO": "relatorio", "RECEBIVEIS_TEMPLATES": "templates",
            "RECEBIVEIS_CLIENTES": "clientes"}
    for variavel, campo in mapa.items():
        monkeypatch.setenv(variavel, f"/container/{campo}")

    config = carregar_config(arquivo)

    for campo in mapa.values():
        assert getattr(config, campo) == Path(f"/container/{campo}")
    assert config.avisos == (-7, 1, 3)  # o resto do arquivo continua valendo


def test_sem_variaveis_de_ambiente_vale_o_config_yaml(tmp_path, monkeypatch):
    for variavel in ("RECEBIVEIS_ENTRADA", "RECEBIVEIS_BANCO", "RECEBIVEIS_CLIENTES"):
        monkeypatch.delenv(variavel, raising=False)

    config = carregar_config(_escrever(tmp_path, _valido(tmp_path)))

    assert config.entrada == Path("entrada") and config.banco == tmp_path / "e.db"


def test_config_yaml_do_projeto_e_valido():
    config = carregar_config(Path(__file__).resolve().parent.parent / "config.yaml")

    assert config.avisos == (-7, 1, 3)
    assert config.aprovacao_vencidos is True
    assert config.fase_validacao_concluida is False

