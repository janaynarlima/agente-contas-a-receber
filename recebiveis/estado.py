"""Memoria do agente em SQLite: boletos e avisos.

Garantias principais:
  - boleto_id e o codigo de barras (nunca o nome do arquivo);
  - avisos tem chave unica (boleto_id, tipo_aviso): o banco impede aviso
    duplicado, mesmo com varias execucoes no mesmo dia;
  - o envio e "reservado" por UPDATE condicional atomico, de modo que duas
    execucoes concorrentes nao confirmam o mesmo aviso.

Ciclo de um aviso:
  pendente -> aguardando_aprovacao -> aprovado -> enviado
  pendente -> enviado                       (envio automatico permitido)
  aguardando_aprovacao -> cancelado         (rejeitado por uma pessoa)
  qualquer nao enviado -> cancelado         (boleto baixado)
  pendente -> em_revisao                    (atraso ou dado faltando)

O esquema e versionado por PRAGMA user_version (atual: 2). Bancos da versao
anterior (user_version 0 com a tabela avisos) sao migrados automaticamente,
preservando os dados.
"""

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

VERSAO_ESQUEMA = 2
STATUS_BOLETO = ("aberto", "pago", "em_revisao")
STATUS_AVISO = (
    "pendente",
    "aguardando_aprovacao",
    "aprovado",
    "enviado",
    "cancelado",
    "em_revisao",
)
# Avisos que ainda nao foram enviados e devem ser cancelados com a baixa.
STATUS_AVISO_ABERTOS = ("pendente", "aguardando_aprovacao", "aprovado", "em_revisao")

_BOLETOS = """
CREATE TABLE IF NOT EXISTS boletos (
    boleto_id        TEXT PRIMARY KEY,
    cnpj_pagador     TEXT,
    nome_pagador     TEXT,
    valor            TEXT,
    vencimento       TEXT,
    status           TEXT NOT NULL CHECK (status IN ('aberto','pago','em_revisao')),
    requer_revisao   INTEGER NOT NULL DEFAULT 0,
    motivos_revisao  TEXT NOT NULL DEFAULT '[]',
    data_baixa       TEXT,
    motivo_baixa     TEXT,
    criado_em        TEXT NOT NULL,
    atualizado_em    TEXT NOT NULL
);
"""

_AVISOS_COLUNAS_ANTIGAS = "boleto_id, tipo_aviso, data_prevista, status, data_envio, motivo"


def _ddl_avisos(nome: str) -> str:
    status = ",".join(f"'{s}'" for s in STATUS_AVISO)
    return f"""
CREATE TABLE {nome} (
    boleto_id      TEXT NOT NULL REFERENCES boletos(boleto_id),
    tipo_aviso     TEXT NOT NULL,
    data_prevista  TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ({status})),
    data_envio     TEXT,
    motivo         TEXT,
    arquivo_fila   TEXT,
    aprovado_por   TEXT,
    data_decisao   TEXT,
    PRIMARY KEY (boleto_id, tipo_aviso)
);
"""


class ErroEstado(RuntimeError):
    """Operacao invalida sobre o estado."""


class Estado:
    """Acesso ao banco. Use como gerenciador de contexto ou chame fechar()."""

    def __init__(self, caminho: str | Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.caminho, timeout=30)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")
        self._preparar_esquema()

    # --- esquema e migracao -----------------------------------------------------

    def _versao(self) -> int:
        return self._con.execute("PRAGMA user_version").fetchone()[0]

    def _tabela_existe(self, nome: str) -> bool:
        return (
            self._con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)
            ).fetchone()
            is not None
        )

    def _preparar_esquema(self) -> None:
        if self._versao() >= VERSAO_ESQUEMA:
            return
        # BEGIN IMMEDIATE serializa processos concorrentes; a versao e relida
        # dentro da transacao para que so um deles migre.
        self._con.isolation_level = None
        try:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                if self._versao() < VERSAO_ESQUEMA:
                    self._con.execute(_BOLETOS.strip().rstrip(";"))
                    if self._tabela_existe("avisos"):
                        self._migrar_avisos_v1_para_v2()
                    else:
                        self._con.execute(_ddl_avisos("avisos").strip().rstrip(";"))
                    self._con.execute(f"PRAGMA user_version = {VERSAO_ESQUEMA}")
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise
        finally:
            self._con.isolation_level = ""

    def _migrar_avisos_v1_para_v2(self) -> None:
        """Recria avisos com os novos status (CHECK nao pode ser alterado)."""
        self._con.execute(_ddl_avisos("avisos_v2").strip().rstrip(";"))
        self._con.execute(
            f"INSERT INTO avisos_v2 ({_AVISOS_COLUNAS_ANTIGAS}) "
            f"SELECT {_AVISOS_COLUNAS_ANTIGAS} FROM avisos"
        )
        self._con.execute("DROP TABLE avisos")
        self._con.execute("ALTER TABLE avisos_v2 RENAME TO avisos")

    def __enter__(self) -> "Estado":
        return self

    def __exit__(self, *_) -> None:
        self.fechar()

    def fechar(self) -> None:
        self._con.close()

    # --- boletos -----------------------------------------------------------

    def registrar_boleto(
        self,
        boleto_id: str,
        *,
        hoje: date,
        cnpj_pagador: str | None = None,
        nome_pagador: str | None = None,
        valor: Decimal | None = None,
        vencimento: date | None = None,
        motivos_revisao: tuple[str, ...] = (),
    ) -> bool:
        """Insere o boleto se for novo. Retorna False se ja existia.

        Um boleto ja conhecido nunca e sobrescrito aqui: baixa, reabertura e
        revisao so mudam por operacoes explicitas.
        """
        status = "em_revisao" if motivos_revisao else "aberto"
        agora = hoje.isoformat()
        with self._con:
            cursor = self._con.execute(
                """INSERT OR IGNORE INTO boletos
                   (boleto_id, cnpj_pagador, nome_pagador, valor, vencimento,
                    status, requer_revisao, motivos_revisao, criado_em, atualizado_em)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    boleto_id,
                    cnpj_pagador,
                    nome_pagador,
                    str(valor) if valor is not None else None,
                    vencimento.isoformat() if vencimento else None,
                    status,
                    1 if motivos_revisao else 0,
                    json.dumps(list(motivos_revisao), ensure_ascii=False),
                    agora,
                    agora,
                ),
            )
        return cursor.rowcount == 1

    def obter_boleto(self, boleto_id: str) -> dict | None:
        linha = self._con.execute(
            "SELECT * FROM boletos WHERE boleto_id = ?", (boleto_id,)
        ).fetchone()
        return self._como_dict(linha)

    def listar_boletos(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in STATUS_BOLETO:
            raise ErroEstado(f"status invalido: {status}")
        consulta = "SELECT * FROM boletos"
        parametros: tuple = ()
        if status is not None:
            consulta += " WHERE status = ?"
            parametros = (status,)
        consulta += " ORDER BY boleto_id"
        return [self._como_dict(linha) for linha in self._con.execute(consulta, parametros)]

    def encontrar_boleto_por_sufixo(self, sufixo: str) -> list[dict]:
        """Boletos cujo identificador termina em `sufixo` (para comandos manuais)."""
        if len(sufixo) < 6 or not sufixo.isalnum():
            raise ErroEstado("informe ao menos 6 caracteres do identificador")
        linhas = self._con.execute(
            "SELECT * FROM boletos WHERE boleto_id LIKE ? ORDER BY boleto_id", (f"%{sufixo}",)
        )
        return [self._como_dict(linha) for linha in linhas]

    def marcar_pago(self, boleto_id: str, hoje: date, motivo: str) -> bool:
        """Baixa um boleto aberto e cancela seus avisos nao enviados.

        Boleto em revisao nao e baixado automaticamente. Retorna False se nao
        houve baixa.
        """
        marcadores = ",".join("?" * len(STATUS_AVISO_ABERTOS))
        with self._con:
            cursor = self._con.execute(
                """UPDATE boletos
                   SET status='pago', data_baixa=?, motivo_baixa=?, atualizado_em=?
                   WHERE boleto_id=? AND status='aberto' AND requer_revisao=0""",
                (hoje.isoformat(), motivo, hoje.isoformat(), boleto_id),
            )
            if cursor.rowcount != 1:
                return False
            self._con.execute(
                f"""UPDATE avisos SET status='cancelado', motivo=?
                    WHERE boleto_id=? AND status IN ({marcadores})""",
                ("cancelado por baixa do boleto", boleto_id, *STATUS_AVISO_ABERTOS),
            )
        return True

    def reabrir(self, boleto_id: str, hoje: date, motivo: str) -> bool:
        """Reabre um boleto pago: volta a aberto e exige revisao humana.

        Avisos nao sao enviados automaticamente enquanto requer_revisao=1.
        """
        with self._con:
            cursor = self._con.execute(
                """UPDATE boletos
                   SET status='aberto', requer_revisao=1, data_baixa=NULL,
                       motivo_baixa=NULL, motivos_revisao=?, atualizado_em=?
                   WHERE boleto_id=? AND status='pago'""",
                (json.dumps([motivo], ensure_ascii=False), hoje.isoformat(), boleto_id),
            )
        return cursor.rowcount == 1

    def liberar_revisao(self, boleto_id: str, hoje: date) -> bool:
        """Registra a decisao humana de que o boleto foi revisado."""
        with self._con:
            cursor = self._con.execute(
                """UPDATE boletos
                   SET status='aberto', requer_revisao=0, motivos_revisao='[]', atualizado_em=?
                   WHERE boleto_id=? AND status IN ('aberto','em_revisao')
                     AND (requer_revisao=1 OR status='em_revisao')""",
                (hoje.isoformat(), boleto_id),
            )
        return cursor.rowcount == 1

    def excluir_boleto_sem_avisos(self, boleto_id: str) -> bool:
        """Remove um registro provisorio (sem avisos). Nunca apaga historico."""
        with self._con:
            cursor = self._con.execute(
                """DELETE FROM boletos WHERE boleto_id=? AND status='em_revisao'
                   AND NOT EXISTS (SELECT 1 FROM avisos WHERE avisos.boleto_id = boletos.boleto_id)""",
                (boleto_id,),
            )
        return cursor.rowcount == 1

    def pode_receber_aviso(self, boleto_id: str) -> bool:
        boleto = self.obter_boleto(boleto_id)
        return bool(
            boleto
            and boleto["status"] == "aberto"
            and not boleto["requer_revisao"]
        )

    # --- avisos ------------------------------------------------------------

    def registrar_aviso(self, boleto_id: str, tipo_aviso: str, data_prevista: date) -> bool:
        """Cria o aviso pendente. Retorna False se (boleto_id, tipo) ja existe."""
        with self._con:
            cursor = self._con.execute(
                """INSERT OR IGNORE INTO avisos (boleto_id, tipo_aviso, data_prevista, status)
                   VALUES (?,?,?, 'pendente')""",
                (boleto_id, tipo_aviso, data_prevista.isoformat()),
            )
        return cursor.rowcount == 1

    def obter_aviso(self, boleto_id: str, tipo_aviso: str) -> dict | None:
        linha = self._con.execute(
            "SELECT * FROM avisos WHERE boleto_id=? AND tipo_aviso=?",
            (boleto_id, tipo_aviso),
        ).fetchone()
        return self._como_dict(linha)

    def historico_avisos(self, boleto_id: str) -> list[dict]:
        linhas = self._con.execute(
            "SELECT * FROM avisos WHERE boleto_id=? ORDER BY data_prevista, tipo_aviso",
            (boleto_id,),
        )
        return [self._como_dict(linha) for linha in linhas]

    def listar_avisos(self, status: str) -> list[dict]:
        if status not in STATUS_AVISO:
            raise ErroEstado(f"status de aviso invalido: {status}")
        linhas = self._con.execute(
            "SELECT * FROM avisos WHERE status=? ORDER BY data_prevista, boleto_id, tipo_aviso",
            (status,),
        )
        return [self._como_dict(linha) for linha in linhas]

    def _transicao(
        self, boleto_id: str, tipo_aviso: str, de: str, para: str, **campos: str | None
    ) -> bool:
        """UPDATE condicional atomico: so muda se o aviso estiver em `de`."""
        atribuicoes = ", ".join(["status=?"] + [f"{nome}=?" for nome in campos])
        with self._con:
            cursor = self._con.execute(
                f"UPDATE avisos SET {atribuicoes} WHERE boleto_id=? AND tipo_aviso=? AND status=?",
                (para, *campos.values(), boleto_id, tipo_aviso, de),
            )
        return cursor.rowcount == 1

    def confirmar_envio(
        self, boleto_id: str, tipo_aviso: str, hoje: date, *, de: str = "pendente"
    ) -> bool:
        """Marca o aviso como enviado de forma atomica.

        So um chamador consegue a transicao `de` -> enviado (padrao: pendente;
        use de="aprovado" para avisos aprovados); os demais recebem False e NAO
        devem enviar. Chame antes do envio real e desfaca com reverter_envio()
        se o envio falhar.
        """
        if de not in ("pendente", "aprovado"):
            raise ErroEstado("so avisos pendentes ou aprovados podem ser enviados")
        return self._transicao(boleto_id, tipo_aviso, de, "enviado", data_envio=hoje.isoformat())

    def reverter_envio(
        self, boleto_id: str, tipo_aviso: str, motivo: str, *, para: str = "pendente"
    ) -> bool:
        """Devolve ao estado anterior um aviso reservado cujo envio falhou."""
        if para not in ("pendente", "aprovado"):
            raise ErroEstado("so e possivel voltar a pendente ou aprovado")
        return self._transicao(
            boleto_id, tipo_aviso, "enviado", para, data_envio=None, motivo=motivo
        )

    def encaminhar_aviso_para_revisao(self, boleto_id: str, tipo_aviso: str, motivo: str) -> bool:
        return self._transicao(boleto_id, tipo_aviso, "pendente", "em_revisao", motivo=motivo)

    def enfileirar_aviso(
        self, boleto_id: str, tipo_aviso: str, arquivo_fila: str, hoje: date
    ) -> bool:
        """pendente -> aguardando_aprovacao (atomico: nao enfileira duas vezes)."""
        return self._transicao(
            boleto_id, tipo_aviso, "pendente", "aguardando_aprovacao",
            arquivo_fila=arquivo_fila, data_decisao=hoje.isoformat(),
        )

    def aprovar_aviso(self, boleto_id: str, tipo_aviso: str, hoje: date, por: str) -> bool:
        """aguardando_aprovacao -> aprovado, registrando quem e quando (auditoria)."""
        return self._transicao(
            boleto_id, tipo_aviso, "aguardando_aprovacao", "aprovado",
            aprovado_por=por, data_decisao=hoje.isoformat(),
        )

    def rejeitar_aviso(
        self, boleto_id: str, tipo_aviso: str, hoje: date, por: str, motivo: str
    ) -> bool:
        """aguardando_aprovacao -> cancelado, registrando quem rejeitou e por que."""
        return self._transicao(
            boleto_id, tipo_aviso, "aguardando_aprovacao", "cancelado",
            aprovado_por=por, data_decisao=hoje.isoformat(), motivo=f"rejeitado: {motivo}",
        )

    def cancelar_avisos_pendentes(self, boleto_id: str, motivo: str) -> int:
        marcadores = ",".join("?" * len(STATUS_AVISO_ABERTOS))
        with self._con:
            cursor = self._con.execute(
                f"""UPDATE avisos SET status='cancelado', motivo=?
                    WHERE boleto_id=? AND status IN ({marcadores})""",
                (motivo, boleto_id, *STATUS_AVISO_ABERTOS),
            )
        return cursor.rowcount

    @staticmethod
    def _como_dict(linha: sqlite3.Row | None) -> dict | None:
        if linha is None:
            return None
        dados = dict(linha)
        if "motivos_revisao" in dados:
            dados["motivos_revisao"] = json.loads(dados["motivos_revisao"])
        return dados
