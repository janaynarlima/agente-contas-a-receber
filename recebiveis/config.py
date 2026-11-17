"""Carregamento e validacao do config.yaml."""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ruamel.yaml import YAML


class ErroConfig(ValueError):
    """Configuracao ausente ou invalida."""


@dataclass(frozen=True)
class Config:
    entrada: Path
    idioma_ocr: str
    avisos: tuple[int, ...]
    confianca_minima: float
    aprovacao_vencidos: bool
    fase_validacao_concluida: bool
    max_percentual_baixas: float
    max_envios_por_execucao: int
    feriados: frozenset[date]
    banco: Path
    # Opcionais (com padrao): nao alteram quem constroi Config sem eles.
    saida: Path = Path("saida")
    fila_aprovacao: Path = Path("fila_aprovacao")
    logs: Path = Path("logs")
    relatorio: Path = Path("relatorio.md")
    templates: Path = Path("templates")
    clientes: Path = Path("clientes.csv")
    bcc_financeiro: str = ""


def nome_tipo_aviso(dias: int) -> str:
    """-7 -> 'D-7'; 1 -> 'D+1'."""
    return f"D{dias:+d}"


def _exigir(dados: dict, chave: str):
    if chave not in dados:
        raise ErroConfig(f"config.yaml: chave obrigatoria ausente: {chave}")
    return dados[chave]


def _booleano(dados: dict, chave: str) -> bool:
    valor = _exigir(dados, chave)
    if not isinstance(valor, bool):
        raise ErroConfig(f"config.yaml: {chave} deve ser true ou false")
    return valor


def _numero(dados: dict, chave: str, minimo: float, maximo: float | None = None) -> float:
    valor = _exigir(dados, chave)
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErroConfig(f"config.yaml: {chave} deve ser numerico")
    if valor < minimo or (maximo is not None and valor > maximo):
        limite = f"entre {minimo} e {maximo}" if maximo is not None else f">= {minimo}"
        raise ErroConfig(f"config.yaml: {chave} deve estar {limite}")
    return valor


def _avisos(dados: dict) -> tuple[int, ...]:
    # Os dias de aviso NAO tem valor padrao no codigo: vem sempre do arquivo.
    bruto = _exigir(dados, "avisos")
    if not isinstance(bruto, list) or not bruto:
        raise ErroConfig("config.yaml: avisos deve ser uma lista nao vazia de inteiros")
    for item in bruto:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ErroConfig("config.yaml: avisos deve conter apenas inteiros")
        if item == 0:
            raise ErroConfig("config.yaml: avisos nao pode conter 0 (D0 nao gera aviso)")
    if len(set(bruto)) != len(bruto):
        raise ErroConfig("config.yaml: avisos nao pode conter dias repetidos")
    return tuple(sorted(bruto))


def _feriados(dados: dict) -> frozenset[date]:
    bruto = dados.get("feriados") or []
    if not isinstance(bruto, list):
        raise ErroConfig("config.yaml: feriados deve ser uma lista de datas AAAA-MM-DD")
    resultado = set()
    for item in bruto:
        try:
            resultado.add(item if isinstance(item, date) else date.fromisoformat(str(item)))
        except ValueError as erro:
            raise ErroConfig(f"config.yaml: feriado invalido: {item}") from erro
    return frozenset(resultado)


def carregar_config(caminho: str | Path = "config.yaml") -> Config:
    arquivo = Path(caminho)
    if not arquivo.is_file():
        raise ErroConfig(f"config.yaml nao encontrado: {arquivo}")
    dados = YAML(typ="safe").load(arquivo.read_text(encoding="utf-8"))
    if not isinstance(dados, dict):
        raise ErroConfig("config.yaml: conteudo invalido")

    config = Config(
        entrada=Path(str(_exigir(dados, "entrada"))),
        idioma_ocr=str(dados.get("idioma_ocr", "por")),
        avisos=_avisos(dados),
        confianca_minima=float(_numero(dados, "confianca_minima", 0, 1)),
        aprovacao_vencidos=_booleano(dados, "aprovacao_vencidos"),
        fase_validacao_concluida=_booleano(dados, "fase_validacao_concluida"),
        max_percentual_baixas=float(_numero(dados, "max_percentual_baixas", 0, 100)),
        max_envios_por_execucao=int(_numero(dados, "max_envios_por_execucao", 1)),
        feriados=_feriados(dados),
        banco=Path(os.path.expandvars(str(_exigir(dados, "banco")))),
        saida=Path(str(dados.get("saida", "saida"))),
        fila_aprovacao=Path(str(dados.get("fila_aprovacao", "fila_aprovacao"))),
        logs=Path(str(dados.get("logs", "logs"))),
        relatorio=Path(str(dados.get("relatorio", "relatorio.md"))),
        templates=Path(str(dados.get("templates", "templates"))),
        clientes=Path(str(dados.get("clientes", "clientes.csv"))),
        bcc_financeiro=str(dados.get("bcc_financeiro") or "").strip(),
    )
    return _aplicar_ambiente(config)


# Sobrescritas por variavel de ambiente: permitem rodar o MESMO config.yaml no
# Docker (caminhos do container) e no Windows, e fazem a tela web e o terminal
# usarem exatamente o mesmo banco, pasta de entrada, fila e modelos.
_AMBIENTE = {
    "RECEBIVEIS_ENTRADA": "entrada",
    "RECEBIVEIS_BANCO": "banco",
    "RECEBIVEIS_SAIDA": "saida",
    "RECEBIVEIS_FILA": "fila_aprovacao",
    "RECEBIVEIS_LOGS": "logs",
    "RECEBIVEIS_RELATORIO": "relatorio",
    "RECEBIVEIS_TEMPLATES": "templates",
    "RECEBIVEIS_CLIENTES": "clientes",
}


def _aplicar_ambiente(config: Config) -> Config:
    from dataclasses import replace

    mudancas = {
        campo: Path(os.environ[variavel])
        for variavel, campo in _AMBIENTE.items()
        if os.environ.get(variavel)
    }
    return replace(config, **mudancas) if mudancas else config
