"""Fixtures compartilhadas para os testes de fila/CLI. Tudo sintetico."""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from recebiveis.clientes import Cliente
from recebiveis.config import Config
from recebiveis.estado import Estado

from tests.test_febraban import fator_de, gerar

RAIZ = Path(__file__).resolve().parent.parent
CNPJ = "12345678000195"
VENCIMENTO = date(2026, 11, 20)


@dataclass
class Ambiente:
    config: Config
    estado: Estado
    clientes: dict
    env: dict
    boleto_id: str
    tmp: Path


def criar_config(tmp: Path) -> Config:
    return Config(
        entrada=tmp / "entrada",
        idioma_ocr="por",
        avisos=(-7, 1, 3),
        confianca_minima=0.8,
        aprovacao_vencidos=True,
        fase_validacao_concluida=False,
        max_percentual_baixas=30,
        max_envios_por_execucao=50,
        feriados=frozenset(),
        banco=tmp / "estado.db",
        saida=tmp / "saida",
        fila_aprovacao=tmp / "fila_aprovacao",
        logs=tmp / "logs",
        relatorio=tmp / "relatorio.md",
        templates=RAIZ / "templates",
        clientes=tmp / "clientes.csv",
        bcc_financeiro="controle@example.com",
    )


@pytest.fixture
def ambiente(tmp_path):
    config = criar_config(tmp_path)
    config.entrada.mkdir()
    barras, _ = gerar(fator_de(VENCIMENTO), 123456)
    with Estado(config.banco) as estado:
        estado.registrar_boleto(
            barras, hoje=date(2026, 11, 10), cnpj_pagador=CNPJ, nome_pagador="Texto do Boleto",
            valor=Decimal("1234.56"), vencimento=VENCIMENTO,
        )
        for tipo, previsto in (("D-7", date(2026, 11, 13)), ("D+1", date(2026, 11, 23)), ("D+3", date(2026, 11, 23))):
            estado.registrar_aviso(barras, tipo, previsto)
        yield Ambiente(
            config=config,
            estado=estado,
            clientes={CNPJ: Cliente(CNPJ, "Cliente Sintetico Ltda", "cobranca@example.com")},
            env={"EMAIL_REMETENTE": "financeiro@example.com", "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587"},
            boleto_id=barras,
            tmp=tmp_path,
        )


def com_cliente_sem_email(ambiente: Ambiente) -> None:
    ambiente.clientes[CNPJ] = Cliente(CNPJ, "Cliente Sintetico Ltda", None)
