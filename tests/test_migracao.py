"""Migracao do esquema do SQLite (user_version 0 -> 2), preservando dados."""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from recebiveis.estado import VERSAO_ESQUEMA, Estado

ID = "23791" + "1" * 39

ESQUEMA_V1 = """
CREATE TABLE boletos (
    boleto_id TEXT PRIMARY KEY, cnpj_pagador TEXT, nome_pagador TEXT, valor TEXT, vencimento TEXT,
    status TEXT NOT NULL CHECK (status IN ('aberto','pago','em_revisao')),
    requer_revisao INTEGER NOT NULL DEFAULT 0, motivos_revisao TEXT NOT NULL DEFAULT '[]',
    data_baixa TEXT, motivo_baixa TEXT, criado_em TEXT NOT NULL, atualizado_em TEXT NOT NULL
);
CREATE TABLE avisos (
    boleto_id TEXT NOT NULL REFERENCES boletos(boleto_id),
    tipo_aviso TEXT NOT NULL, data_prevista TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pendente','enviado','cancelado','em_revisao')),
    data_envio TEXT, motivo TEXT, PRIMARY KEY (boleto_id, tipo_aviso)
);
"""


def banco_v1(caminho):
    con = sqlite3.connect(caminho)
    con.executescript(ESQUEMA_V1)
    con.execute(
        "INSERT INTO boletos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ID, "12345678000195", "Cliente", "10.00", "2026-11-20", "aberto", 0, "[]", None, None,
         "2026-11-01", "2026-11-01"),
    )
    con.execute("INSERT INTO avisos VALUES (?,?,?,?,?,?)", (ID, "D-7", "2026-11-13", "enviado", "2026-11-13", None))
    con.execute("INSERT INTO avisos VALUES (?,?,?,?,?,?)", (ID, "D+1", "2026-11-23", "pendente", None, None))
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    con.close()


def test_banco_novo_ja_nasce_na_versao_atual(tmp_path):
    with Estado(tmp_path / "novo.db") as estado:
        assert estado._versao() == VERSAO_ESQUEMA


def test_banco_antigo_e_migrado_preservando_dados(tmp_path):
    caminho = tmp_path / "antigo.db"
    banco_v1(caminho)

    with Estado(caminho) as estado:
        assert estado._versao() == VERSAO_ESQUEMA
        assert estado.obter_boleto(ID)["valor"] == "10.00"
        assert estado.obter_aviso(ID, "D-7")["status"] == "enviado"
        assert estado.obter_aviso(ID, "D-7")["data_envio"] == "2026-11-13"
        assert estado.obter_aviso(ID, "D+1")["status"] == "pendente"
        # colunas novas existem e valem para os avisos antigos
        assert estado.obter_aviso(ID, "D+1")["arquivo_fila"] is None


def test_banco_migrado_aceita_os_novos_status_e_mantem_a_chave_unica(tmp_path):
    caminho = tmp_path / "antigo.db"
    banco_v1(caminho)

    with Estado(caminho) as estado:
        assert estado.enfileirar_aviso(ID, "D+1", "fila/x.eml", date(2026, 11, 23))
        assert estado.aprovar_aviso(ID, "D+1", date(2026, 11, 23), "maria")
        assert estado.registrar_aviso(ID, "D+1", date(2026, 11, 23)) is False
        with pytest.raises(sqlite3.IntegrityError):
            estado.registrar_aviso("23791" + "9" * 39, "D+1", date(2026, 11, 23))  # FK de boleto inexistente


def test_migracao_e_idempotente_e_reabrir_nao_altera_nada(tmp_path):
    caminho = tmp_path / "antigo.db"
    banco_v1(caminho)
    with Estado(caminho):
        pass

    with Estado(caminho) as estado:
        assert estado._versao() == VERSAO_ESQUEMA
        assert len(estado.historico_avisos(ID)) == 2


def test_banco_migrado_mantem_a_integridade(tmp_path):
    caminho = tmp_path / "antigo.db"
    banco_v1(caminho)

    with Estado(caminho) as estado:
        assert estado._con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert estado._con.execute("PRAGMA foreign_key_check").fetchall() == []
