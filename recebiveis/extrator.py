"""Leitura de documentos e extracao inicial dos campos do boleto."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from recebiveis.leitor import ler_imagem, ler_pdf
from recebiveis.validador import (
    DadosFebraban,
    ErroFebraban,
    interpretar_codigo_barras,
    interpretar_linha_digitavel,
    validar_cnpj,
)

# Linha digitavel de 47 digitos; tolera pontos e espacos (inclusive os que o
# OCR insere ou omite). Qualquer candidato ainda precisa passar pelos DVs.
_LINHA_DIGITAVEL = re.compile(
    r"(?<!\d)(\d{5}[.\s]?\d{5}\s+\d{5}[.\s]?\d{6}\s+\d{5}[.\s]?\d{6}\s+\d\s*\d{14})(?!\d)"
)
_CODIGO_BARRAS = re.compile(r"(?<!\d)(\d{44})(?!\d)")


EXTENSOES_IMAGEM = {".png", ".jpg", ".jpeg"}
# "Sacado" e o termo antigo de "Pagador"; "Cedente" ja encerra o trecho abaixo.
_ROTULO_PAGADOR = re.compile(r"^\s*(?:dados\s+)?(?:pagador|sacado)\b", re.IGNORECASE)
# Separa "Nome do cliente  CNPJ/CPF: 00..." quando nome e documento vem na mesma linha.
_SEPARADOR_DOCUMENTO = re.compile(r"[\s\-–|]+(?=(?:cnpj|cpf|cnp|cnh)\b)", re.IGNORECASE)
_ROTULOS_FIM_PAGADOR = re.compile(
    r"^\s*(?:dados\s+)?(?:beneficiario|cedente)\b",
    re.IGNORECASE,
)
# Tolera erros comuns de OCR no rotulo (CNP, CNH). O numero continua sendo
# validado pelos digitos verificadores, entao um rotulo lido errado nao permite
# aceitar um CNPJ incorreto.
_ROTULO_CNPJ = re.compile(r"\bcn(?:pj|p|h)\s*/\s*cpf\s*[:\-]?\s*(.*)|\bcnpj\s*[:\-]?\s*(.*)", re.IGNORECASE)
_ROTULO_NOME = re.compile(r"^\s*nome(?:\s+do\s+pagador)?\s*[:\-]\s*(.+?)\s*$", re.IGNORECASE)
_ROTULO_VENCIMENTO = re.compile(
    r"^\s*(?:data\s+de\s+)?vencimento\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
    re.IGNORECASE,
)
_ROTULO_VALOR = re.compile(
    r"^\s*(?:\(=\)\s*)?(?:valor(?:\s+(?:do\s+documento|do\s+boleto|cobrado|nominal))?)"
    r"\s*[:\-]?\s*(?:r\$\s*)?(\d[\d.]*,\d{2}|\d+\.\d{2})\b",
    re.IGNORECASE,
)
# Layout com rotulo e valor em linhas separadas (comum em boletos em PDF).
_ROTULO_VALOR_ISOLADO = re.compile(
    r"^\s*(?:\(=\)\s*)?valor\s+(?:do\s+documento|do\s+boleto|cobrado|nominal)\s*[:\-]?\s*$",
    re.IGNORECASE,
)
_VALOR_ISOLADO = re.compile(r"^\s*(?:r\$\s*)?(\d[\d.]*,\d{2}|\d+\.\d{2})\s*$", re.IGNORECASE)
_ROTULO_VENCIMENTO_ISOLADO = re.compile(
    r"^\s*(?:data\s+de\s+)?vencimento\s*[:\-]?\s*$", re.IGNORECASE
)
_DATA_ISOLADA = re.compile(r"^\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})\s*$")


@dataclass(frozen=True)
class DadosBoleto:
    """Campos extraidos sem inferir valores ausentes."""

    cnpj_pagador: str | None
    nome_pagador: str | None
    valor: Decimal | None
    vencimento: date | None
    codigo_barras: str | None = None
    motivos_revisao: tuple[str, ...] = ()


def _sem_acentos(texto: str) -> str:
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(caractere)
    )


def _adicionar_nome(trecho: list[str], texto: str) -> None:
    """Acrescenta o nome; se o documento vier na mesma linha, vira uma linha propria."""
    separado = _SEPARADOR_DOCUMENTO.search(texto)
    if separado and separado.start() > 0:
        trecho.append(f"Nome: {texto[:separado.start()].strip(' :-')}")
        trecho.append(texto[separado.end():].strip())
    else:
        trecho.append(f"Nome: {texto.strip()}")


def _linhas_pagador(linhas: list[str]) -> list[str]:
    trecho: list[str] = []
    encontrou_pagador = False
    aguardando_nome = False

    for linha in linhas:
        normalizada = _sem_acentos(linha)
        if _ROTULO_PAGADOR.match(normalizada):
            encontrou_pagador = True
            restante = _ROTULO_PAGADOR.sub("", linha, count=1).strip(" :-")
            if restante:
                if not re.match(r"(?:nome|cnpj)\b", restante, re.IGNORECASE):
                    _adicionar_nome(trecho, restante)
                else:
                    trecho.append(restante)
            else:
                # Rotulo sozinho: o nome vem na primeira linha util seguinte.
                aguardando_nome = True
            continue
        if encontrou_pagador and _ROTULOS_FIM_PAGADOR.match(normalizada):
            break
        if encontrou_pagador:
            if aguardando_nome and linha.strip():
                aguardando_nome = False
                if not re.match(r"\s*(?:nome|cnpj|cpf)\b", normalizada, re.IGNORECASE):
                    _adicionar_nome(trecho, linha)
                    continue
            trecho.append(linha)

    return trecho


def _extrair_cnpj_e_nome(linhas: list[str]) -> tuple[str | None, str | None]:
    cnpj_pagador = None
    nome_pagador = None

    for linha in linhas:
        linha_normalizada = _sem_acentos(linha)
        rotulo_cnpj = _ROTULO_CNPJ.search(linha_normalizada)
        if rotulo_cnpj and cnpj_pagador is None:
            digitos = re.sub(r"\D", "", rotulo_cnpj.group(1) or rotulo_cnpj.group(2) or "")
            # Alguns bancos imprimem o CNPJ com 15 digitos (zero a esquerda).
            # So se remove o zero; o resultado ainda passa pelos digitos
            # verificadores, entao nada e adivinhado.
            if len(digitos) == 15 and digitos[0] == "0":
                digitos = digitos[1:]
            if len(digitos) == 14:
                if not validar_cnpj(digitos):
                    raise ValueError("CNPJ do pagador invalido")
                cnpj_pagador = digitos
            elif digitos and len(digitos) != 11:
                raise ValueError("CNPJ do pagador deve conter 14 digitos")

        rotulo_nome = _ROTULO_NOME.match(linha)
        if rotulo_nome and nome_pagador is None:
            nome_pagador = rotulo_nome.group(1).strip()

    return cnpj_pagador, nome_pagador


def _proxima_linha_util(linhas: list[str], indice: int) -> str:
    for linha in linhas[indice + 1 :]:
        if linha.strip():
            return linha
    return ""


def _extrair_valor_e_vencimento(linhas: list[str]) -> tuple[Decimal | None, date | None]:
    valor = None
    vencimento = None

    for indice, linha in enumerate(linhas):
        proxima = _proxima_linha_util(linhas, indice)

        if valor is None:
            rotulo_valor = _ROTULO_VALOR.match(_sem_acentos(linha))
            numero = rotulo_valor.group(1) if rotulo_valor else None
            if numero is None and _ROTULO_VALOR_ISOLADO.match(_sem_acentos(linha)):
                valor_proximo = _VALOR_ISOLADO.match(proxima)
                numero = valor_proximo.group(1) if valor_proximo else None
            if numero is not None:
                numero_normalizado = (
                    numero.replace(".", "").replace(",", ".")
                    if "," in numero
                    else numero
                )
                valor = Decimal(numero_normalizado)

        if vencimento is None:
            rotulo_vencimento = _ROTULO_VENCIMENTO.match(_sem_acentos(linha))
            data_texto = rotulo_vencimento.group(1) if rotulo_vencimento else None
            if data_texto is None and _ROTULO_VENCIMENTO_ISOLADO.match(_sem_acentos(linha)):
                data_proxima = _DATA_ISOLADA.match(proxima)
                data_texto = data_proxima.group(1) if data_proxima else None
            if data_texto is not None:
                dia, mes, ano = re.split(r"[/-]", data_texto)
                try:
                    vencimento = date(int(ano), int(mes), int(dia))
                except ValueError as erro:
                    raise ValueError("Data de vencimento invalida") from erro

    return valor, vencimento


def extrair_texto(caminho: str | Path, idioma_ocr: str = "por") -> str:
    """Extrai texto de PDF ou imagem."""
    arquivo = Path(caminho)
    extensao = arquivo.suffix.lower()

    if extensao == ".pdf":
        return ler_pdf(arquivo, idioma_ocr=idioma_ocr)
    if extensao in EXTENSOES_IMAGEM:
        return ler_imagem(arquivo, idioma_ocr=idioma_ocr)

    formatos = ", ".join(sorted(EXTENSOES_IMAGEM | {".pdf"}))
    raise ValueError(f"Formato nao suportado: {extensao or '(sem extensao)'}. Use {formatos}.")


def _extrair_febraban(
    texto: str, referencia: date | None
) -> tuple[DadosFebraban | None, list[str]]:
    """Localiza e valida a linha digitavel (ou o codigo de barras) no texto."""
    candidatos = [("linha digitavel", _LINHA_DIGITAVEL, interpretar_linha_digitavel)]
    candidatos.append(("codigo de barras", _CODIGO_BARRAS, interpretar_codigo_barras))

    for nome, padrao, interpretar in candidatos:
        validos: dict[str, DadosFebraban] = {}
        motivos: list[str] = []
        for correspondencia in padrao.finditer(texto):
            try:
                dados = interpretar(correspondencia.group(1), referencia)
                validos[dados.codigo_barras] = dados
            except ErroFebraban as erro:
                motivos.append(f"{nome} invalida: {erro}")

        if len(validos) > 1:
            return None, [f"{nome}s divergentes no mesmo documento"]
        if validos:
            # Recibo e ficha de compensacao repetem o mesmo codigo: aceitar
            # um valido ja basta, erros de OCR nas copias nao o invalidam.
            return next(iter(validos.values())), []
        if motivos:
            return None, list(dict.fromkeys(motivos))

    return None, ["campo ausente: linha_digitavel"]


def extrair_dados_boleto(texto: str, referencia: date | None = None) -> DadosBoleto:
    """Extrai campos textualmente identificados, sem usar dados do beneficiario.

    Dado invalido ou ausente nao levanta excecao: o campo fica None e o motivo
    entra em `motivos_revisao`, para o boleto seguir para revisao humana.
    """
    linhas = texto.splitlines()
    linhas_pagador = _linhas_pagador(linhas)
    motivos: list[str] = []

    try:
        cnpj_pagador, nome_pagador = _extrair_cnpj_e_nome(linhas_pagador)
    except ValueError as erro:
        cnpj_pagador, nome_pagador = None, _extrair_cnpj_e_nome_sem_cnpj(linhas_pagador)
        motivos.append(str(erro))

    try:
        valor, vencimento = _extrair_valor_e_vencimento(linhas)
    except ValueError as erro:
        valor, vencimento = None, None
        motivos.append(str(erro))

    febraban, motivos_febraban = _extrair_febraban(texto, referencia)
    motivos.extend(motivos_febraban)
    codigo_barras = febraban.codigo_barras if febraban else None
    if febraban:
        # A linha digitavel e a fonte primaria; o texto so confirma. Divergencia
        # entre os dois envia o boleto para revisao, sem escolher sozinha.
        if febraban.valor is not None:
            if valor is not None and valor != febraban.valor:
                motivos.append("divergencia de valor entre texto e linha digitavel")
            valor = febraban.valor
        if febraban.vencimento is not None:
            if vencimento is not None and vencimento != febraban.vencimento:
                motivos.append("divergencia de vencimento entre texto e linha digitavel")
            vencimento = febraban.vencimento

    for campo, conteudo in (
        ("cnpj_pagador", cnpj_pagador),
        ("nome_pagador", nome_pagador),
        ("valor", valor),
        ("vencimento", vencimento),
    ):
        if conteudo is None:
            motivos.append(f"campo ausente: {campo}")

    return DadosBoleto(
        cnpj_pagador=cnpj_pagador,
        nome_pagador=nome_pagador,
        valor=valor,
        vencimento=vencimento,
        codigo_barras=codigo_barras,
        motivos_revisao=tuple(dict.fromkeys(motivos)),
    )


def _extrair_cnpj_e_nome_sem_cnpj(linhas: list[str]) -> str | None:
    """Recupera so o nome quando o CNPJ do pagador foi rejeitado."""
    for linha in linhas:
        rotulo_nome = _ROTULO_NOME.match(linha)
        if rotulo_nome:
            return rotulo_nome.group(1).strip()
    return None
