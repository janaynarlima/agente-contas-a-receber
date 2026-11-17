"""Dados sinteticos para o golden set.

Reimplementa os digitos verificadores (FEBRABAN e CNPJ) de forma independente
de `recebiveis.validador`, para que um erro na regra de producao nao se repita
aqui e escape da avaliacao. CNPJs gerados tem DVs validos mas sao numeros
ficticios: nenhum dado real e usado.
"""

import random
from datetime import date

DATA_BASE = date(1997, 10, 7)


def _dv10(campo: str) -> str:
    soma = 0
    for posicao, digito in enumerate(reversed(campo)):
        produto = int(digito) * (2 if posicao % 2 == 0 else 1)
        soma += sum(int(c) for c in str(produto))
    return str((10 - soma % 10) % 10)


def _dv11(base43: str) -> str:
    total = sum(int(d) * ((i % 8) + 2) for i, d in enumerate(reversed(base43)))
    dv = 11 - total % 11
    return "1" if dv in (0, 10, 11) else str(dv)


def fator_de(vencimento: date) -> int:
    dias = (vencimento - DATA_BASE).days
    return dias if dias <= 9999 else dias - 9000


def gerar_boleto(vencimento: date, centavos: int, banco: str, livre: str) -> tuple[str, str]:
    """Retorna (codigo_barras, linha_digitavel) validos."""
    sem_dv = f"{banco}9{fator_de(vencimento):04d}{centavos:010d}{livre}"
    barras = sem_dv[:4] + _dv11(sem_dv) + sem_dv[4:]
    c1, c2, c3 = barras[:4] + barras[19:24], barras[24:34], barras[34:44]
    linha = (
        f"{c1[:5]}.{c1[5:]}{_dv10(c1)} {c2[:5]}.{c2[5:]}{_dv10(c2)} "
        f"{c3[:5]}.{c3[5:]}{_dv10(c3)} {barras[4]} {barras[5:19]}"
    )
    return barras, linha


def cnpj_valido(sequencia: int) -> str:
    """CNPJ ficticio com DVs corretos: 8 digitos de raiz + filial 0001."""
    base = f"{sequencia:08d}0001"
    soma1 = sum(int(d) * p for d, p in zip(base, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)))
    d1 = 0 if soma1 % 11 < 2 else 11 - soma1 % 11
    soma2 = sum(int(d) * p for d, p in zip(base + str(d1), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)))
    d2 = 0 if soma2 % 11 < 2 else 11 - soma2 % 11
    return f"{base}{d1}{d2}"


def formatar_cnpj(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def cnpj_com_dv_errado(cnpj: str) -> str:
    ultimo = (int(cnpj[-1]) + 1) % 10
    return cnpj[:-1] + str(ultimo)


def formatar_valor(centavos: int) -> str:
    reais, cent = divmod(centavos, 100)
    return f"{reais:,}".replace(",", ".") + f",{cent:02d}"


def livre_aleatorio(gerador: random.Random) -> str:
    return "".join(str(gerador.randrange(10)) for _ in range(25))
