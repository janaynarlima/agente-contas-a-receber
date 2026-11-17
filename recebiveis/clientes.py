"""Cadastro de clientes (clientes.csv): cnpj,nome,email_cobranca.

Somente leitura: o agente nunca altera o cadastro (restricao operacional).
O e-mail NAO esta no boleto; o cliente e localizado pelo CNPJ do pagador.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from recebiveis.validador import validar_cnpj

CABECALHO = ("cnpj", "nome", "email_cobranca")
_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")


class ErroClientes(ValueError):
    """clientes.csv ausente ou invalido."""


@dataclass(frozen=True)
class Cliente:
    cnpj: str  # 14 digitos
    nome: str
    email_cobranca: str | None  # None = sem e-mail cadastrado


def email_valido(email: str) -> bool:
    return bool(_EMAIL.match(email)) and len(email) <= 254


def carregar_clientes(caminho: str | Path) -> dict[str, Cliente]:
    """Le o cadastro. CNPJ invalido, e-mail invalido ou CNPJ repetido sao erros.

    Mensagens de erro citam a linha, nunca o CNPJ nem o e-mail (LGPD).
    """
    arquivo = Path(caminho)
    if not arquivo.is_file():
        raise ErroClientes(f"cadastro de clientes nao encontrado: {arquivo}")

    clientes: dict[str, Cliente] = {}
    with arquivo.open(encoding="utf-8-sig", newline="") as leitura:
        leitor = csv.DictReader(leitura)
        if tuple(leitor.fieldnames or ()) != CABECALHO:
            raise ErroClientes(f"cabecalho do cadastro deve ser: {','.join(CABECALHO)}")

        for numero, linha in enumerate(leitor, start=2):
            cnpj = re.sub(r"\D", "", linha.get("cnpj") or "")
            # Alguns sistemas imprimem o CNPJ com 15 digitos (zero a esquerda).
            # So se remove o zero; o resultado ainda passa pelos digitos verificadores.
            if len(cnpj) == 15 and cnpj[0] == "0":
                cnpj = cnpj[1:]
            nome = (linha.get("nome") or "").strip()
            email = (linha.get("email_cobranca") or "").strip()

            if not validar_cnpj(cnpj):
                raise ErroClientes(f"linha {numero}: CNPJ invalido")
            if not nome or any(ch in nome for ch in "\r\n"):
                raise ErroClientes(f"linha {numero}: nome ausente ou invalido")
            if email and not email_valido(email):
                raise ErroClientes(f"linha {numero}: e-mail invalido")
            if cnpj in clientes:
                raise ErroClientes(f"linha {numero}: CNPJ repetido no cadastro")

            clientes[cnpj] = Cliente(cnpj=cnpj, nome=nome, email_cobranca=email or None)
    return clientes


def buscar_cliente(clientes: dict[str, Cliente], cnpj: str | None) -> Cliente | None:
    if not cnpj:
        return None
    return clientes.get(re.sub(r"\D", "", cnpj))
