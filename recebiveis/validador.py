"""Validacoes deterministicas dos dados extraidos do boleto.

Regra FEBRABAN para boletos de cobranca bancaria
-------------------------------------------------
Codigo de barras (44 digitos), posicoes:

    1-3   banco | 4 moeda | 5 DV geral | 6-9 fator de vencimento
    10-19 valor (10 digitos, em centavos) | 20-44 campo livre (25)

Linha digitavel (47 digitos) = o mesmo conteudo reorganizado em 5 campos:

    campo 1: banco+moeda+5 primeiros do campo livre + DV1   (10 digitos)
    campo 2: posicoes 6-15 do campo livre + DV2             (11 digitos)
    campo 3: posicoes 16-25 do campo livre + DV3            (11 digitos)
    campo 4: DV geral do codigo de barras                   (1 digito)
    campo 5: fator de vencimento (4) + valor (10)           (14 digitos)

Digitos verificadores:
  - DV1, DV2 e DV3: modulo 10 (pesos 2,1,2,1... da direita para a esquerda;
    produtos com dois digitos somam seus algarismos).
  - DV geral: modulo 11 sobre os 43 digitos restantes do codigo de barras
    (pesos 2 a 9 da direita para a esquerda); resto 0, 10 ou 11 vira 1.

Fator de vencimento: dias corridos desde a data-base 07/10/1997 (fator 0000).
O fator tem 4 digitos e, ao chegar a 9999 (21/02/2025), o ciclo recomeca em
1000 (22/02/2025). Logo, o mesmo fator corresponde a datas separadas por
9000 dias. A data correta e a mais proxima de uma data de referencia
(por padrao, hoje); o fator 0000 significa "sem vencimento". Cobranca de
arrecadacao/convenio (48 digitos) nao e tratada aqui.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


_PESOS_CNPJ = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
_DATA_BASE_FATOR = date(1997, 10, 7)
_CICLO_FATOR = 9000
_FATOR_MINIMO = 1000


class ErroFebraban(ValueError):
    """Linha digitavel ou codigo de barras invalido."""


@dataclass(frozen=True)
class DadosFebraban:
    """Dados derivados de um codigo de barras valido."""

    codigo_barras: str
    valor: Decimal | None
    vencimento: date | None


def validar_cnpj(cnpj: str) -> bool:
    """Valida um CNPJ numerico pelos dois digitos verificadores."""
    digitos = re.sub(r"\D", "", cnpj)
    if len(digitos) != 14 or len(set(digitos)) == 1:
        return False

    def calcular_digito(base: str, pesos: tuple[int, ...]) -> str:
        resto = sum(int(digito) * peso for digito, peso in zip(base, pesos)) % 11
        return str(0 if resto < 2 else 11 - resto)

    primeiro = calcular_digito(digitos[:12], _PESOS_CNPJ)
    segundo = calcular_digito(digitos[:12] + primeiro, (6,) + _PESOS_CNPJ)
    return digitos[-2:] == primeiro + segundo


def _modulo10(campo: str) -> int:
    soma = 0
    peso = 2
    for digito in reversed(campo):
        produto = int(digito) * peso
        soma += produto - 9 if produto > 9 else produto
        peso = 1 if peso == 2 else 2
    return (10 - soma % 10) % 10


def _modulo11_codigo_barras(base: str) -> int:
    soma = 0
    peso = 2
    for digito in reversed(base):
        soma += int(digito) * peso
        peso = 2 if peso == 9 else peso + 1
    dv = 11 - soma % 11
    return 1 if dv in (0, 10, 11) else dv


def _so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def validar_codigo_barras(codigo: str) -> bool:
    """Valida o DV geral de um codigo de barras de 44 digitos."""
    digitos = _so_digitos(codigo)
    if len(digitos) != 44:
        return False
    return int(digitos[4]) == _modulo11_codigo_barras(digitos[:4] + digitos[5:])


def validar_linha_digitavel(linha: str) -> bool:
    """Valida os tres DVs de campo e o DV geral de uma linha de 47 digitos."""
    try:
        linha_para_codigo_barras(linha)
    except ErroFebraban:
        return False
    return True


def linha_para_codigo_barras(linha: str) -> str:
    """Converte uma linha digitavel valida no codigo de barras de 44 digitos."""
    digitos = _so_digitos(linha)
    if len(digitos) != 47:
        raise ErroFebraban("linha digitavel deve conter 47 digitos")

    campos = (digitos[0:9], digitos[10:20], digitos[21:31])
    dvs = (digitos[9], digitos[20], digitos[31])
    for numero, (campo, dv) in enumerate(zip(campos, dvs), start=1):
        if int(dv) != _modulo10(campo):
            raise ErroFebraban(f"digito verificador do campo {numero} da linha digitavel invalido")

    codigo = digitos[0:4] + digitos[32] + digitos[33:47] + digitos[4:9] + digitos[10:20] + digitos[21:31]
    if not validar_codigo_barras(codigo):
        raise ErroFebraban("digito verificador geral da linha digitavel invalido")
    return codigo


def codigo_barras_para_linha(codigo: str) -> str:
    """Converte um codigo de barras valido de 44 digitos na linha digitavel.

    E a operacao inversa de linha_para_codigo_barras; serve para mostrar ao
    cliente o numero que ele usa para pagar.
    """
    digitos = _so_digitos(codigo)
    if len(digitos) != 44 or not validar_codigo_barras(digitos):
        raise ErroFebraban("codigo de barras invalido")

    livre = digitos[19:44]
    campo1 = digitos[0:4] + livre[0:5]
    campo2 = livre[5:15]
    campo3 = livre[15:25]
    return (
        f"{campo1[:5]}.{campo1[5:]}{_modulo10(campo1)} "
        f"{campo2[:5]}.{campo2[5:]}{_modulo10(campo2)} "
        f"{campo3[:5]}.{campo3[5:]}{_modulo10(campo3)} "
        f"{digitos[4]} {digitos[5:19]}"
    )


def fator_para_data(fator: int, referencia: date | None = None) -> date | None:
    """Converte o fator de vencimento em data, tratando o reinicio de 2025.

    Retorna None para o fator 0000 (sem vencimento). Escolhe, entre as datas
    possiveis (uma a cada 9000 dias), a mais proxima da data de referencia.
    """
    if fator == 0:
        return None
    if not _FATOR_MINIMO <= fator <= 9999:
        raise ErroFebraban("fator de vencimento fora do intervalo 1000-9999")

    referencia = referencia or date.today()
    candidatas = [
        _DATA_BASE_FATOR + timedelta(days=fator + _CICLO_FATOR * ciclo) for ciclo in range(0, 6)
    ]
    return min(candidatas, key=lambda data: abs((data - referencia).days))


def interpretar_codigo_barras(codigo: str, referencia: date | None = None) -> DadosFebraban:
    """Valida um codigo de barras e deriva valor e vencimento."""
    digitos = _so_digitos(codigo)
    if len(digitos) != 44:
        raise ErroFebraban("codigo de barras deve conter 44 digitos")
    if not validar_codigo_barras(digitos):
        raise ErroFebraban("digito verificador do codigo de barras invalido")

    centavos = int(digitos[9:19])
    return DadosFebraban(
        codigo_barras=digitos,
        valor=Decimal(centavos) / 100 if centavos else None,
        vencimento=fator_para_data(int(digitos[5:9]), referencia),
    )


def interpretar_linha_digitavel(linha: str, referencia: date | None = None) -> DadosFebraban:
    """Valida uma linha digitavel e deriva codigo de barras, valor e vencimento."""
    return interpretar_codigo_barras(linha_para_codigo_barras(linha), referencia)
