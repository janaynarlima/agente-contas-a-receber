"""Gerador do golden set: boletos sinteticos com resposta esperada conhecida.

Cada caso tem um arquivo (PDF com texto, PDF escaneado, PNG ou JPG) e uma
entrada no gabarito (`gabarito.json`). Os layouts sao APROXIMACOES sinteticas
de estilos comuns (rotulo acima do valor, tudo na mesma linha, blocos
rotulados, terminologia antiga "Sacado/Cedente"); nao reproduzem o leiaute de
nenhum banco real. Os resultados da medicao, portanto, nao generalizam para
boletos reais.

Resultado esperado de cada caso:
  aceitar     -> todos os campos corretos e SEM revisao;
  revisao     -> o agente DEVE enviar para revisao (nao pode aceitar);
  indiferente -> correto ou revisao servem; aceitar com dado errado e falha
                 (usado para OCR ruim).
"""

import io
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pymupdf
from PIL import Image, ImageFilter

from recebiveis.avaliacao.sintetico import (
    cnpj_com_dv_errado,
    cnpj_valido,
    formatar_cnpj,
    formatar_valor,
    gerar_boleto,
    livre_aleatorio,
)

REFERENCIA = date(2026, 11, 17)
SEMENTE = 20261117

BENEFICIARIOS = [
    ("Transportadora Rio Claro Ltda", 11222333),
    ("Industria Alfa de Embalagens SA", 22333444),
    ("Comercial Boa Vista Eireli", 33444555),
]
PAGADORES = [
    "Comercial Aurora Ltda", "Metalurgica Pinheiro SA", "Distribuidora Serra Azul Ltda",
    "Grafica Horizonte Eireli", "Industria Nova Era Ltda", "Servicos Delta ME",
    "Laboratorio Santa Clara Ltda", "Cooperativa Vale Verde", "Auto Pecas Planalto Ltda",
    "Moveis Cedro Ltda", "Padaria Estrela do Sul Ltda", "Tecnologia Prisma SA",
]
BANCOS = [("237", "BANCO ALFA"), ("341", "BANCO BETA"), ("001", "BANCO GAMA"), ("104", "BANCO DELTA")]
LAYOUTS = ("coluna", "linha", "blocos", "sacado")
FORMATOS = ("pdf_texto", "pdf_escaneado", "png")


@dataclass
class Caso:
    id: str
    arquivo: str
    formato: str
    layout: str
    descricao: str
    esperado: str                       # aceitar | revisao | indiferente
    cnpj: str | None = None             # CNPJ do pagador (14 digitos) ou None
    nome: str | None = None
    valor: str | None = None            # Decimal como texto
    vencimento: str | None = None       # AAAA-MM-DD
    referencia: str = REFERENCIA.isoformat()
    campos_nao_avaliados: list[str] = field(default_factory=list)


@dataclass
class Dados:
    """Conteudo de um boleto antes de virar arquivo."""
    banco: tuple[str, str]
    beneficiario: tuple[str, str]       # (nome, cnpj formatado)
    pagador: tuple[str, str] | None     # (nome, documento formatado) ou None
    vencimento_texto: date
    valor_texto: int                    # centavos mostrados nos rotulos
    linha: str | None
    mostrar_linha: bool = True


# --- layouts -------------------------------------------------------------------


def _linhas_coluna(d: Dados) -> list[str]:
    nome_b, cnpj_b = d.beneficiario
    linhas = [f"{d.banco[1]}  {d.banco[0]}-X", d.linha or "", "Local de Pagamento",
              "Pagavel em qualquer banco ate o vencimento.", "Vencimento",
              d.vencimento_texto.strftime("%d/%m/%Y"), "Beneficiario", nome_b,
              f"CPF/CNPJ: {cnpj_b}", "Agencia/Codigo Beneficiario", "0001/123456-7",
              "(=) Valor do documento", formatar_valor(d.valor_texto)]
    if d.pagador:
        linhas += ["Pagador:", d.pagador[0], f"CNPJ/CPF: {d.pagador[1]}"]
    linhas += ["Beneficiario final:", "Nao informado", "Recibo do Pagador", "Autenticacao Mecanica"]
    return [x for x in linhas if x != ""] if d.mostrar_linha else [x for x in linhas if x != d.linha]


def _linhas_linha(d: Dados) -> list[str]:
    nome_b, cnpj_b = d.beneficiario
    linhas = [f"{d.banco[1]} | {d.banco[0]}-X | {d.linha or ''}".rstrip(" |")]
    if d.pagador:
        linhas.append(f"Pagador: {d.pagador[0]}  CNPJ/CPF: {d.pagador[1]}")
    linhas += [f"Beneficiario: {nome_b}  CNPJ: {cnpj_b}",
               f"Data de vencimento: {d.vencimento_texto.strftime('%d/%m/%Y')}",
               f"Valor do documento: R$ {formatar_valor(d.valor_texto)}", "Autenticacao mecanica"]
    return linhas


def _linhas_blocos(d: Dados) -> list[str]:
    nome_b, cnpj_b = d.beneficiario
    linhas = [d.banco[1], d.linha or ""]
    if d.pagador:
        linhas += ["PAGADOR", f"Nome: {d.pagador[0]}", f"CNPJ/CPF: {d.pagador[1]}"]
    linhas += ["BENEFICIARIO", f"Nome: {nome_b}", f"CNPJ/CPF: {cnpj_b}",
               f"Vencimento: {d.vencimento_texto.strftime('%d/%m/%Y')}",
               f"Valor cobrado: R$ {formatar_valor(d.valor_texto)}"]
    return [x for x in linhas if x != ""]


def _linhas_sacado(d: Dados) -> list[str]:
    nome_b, cnpj_b = d.beneficiario
    linhas = [d.banco[1], d.linha or "", "Cedente", f"{nome_b}  CNPJ: {cnpj_b}"]
    if d.pagador:
        linhas += ["Sacado", d.pagador[0], f"CNPJ/CPF: {d.pagador[1]}"]
    linhas += ["Data de Vencimento", d.vencimento_texto.strftime("%d/%m/%Y"),
               "Valor do Documento", f"R$ {formatar_valor(d.valor_texto)}"]
    return [x for x in linhas if x != ""]


_LAYOUTS = {"coluna": _linhas_coluna, "linha": _linhas_linha, "blocos": _linhas_blocos,
            "sacado": _linhas_sacado}


# --- renderizacao ----------------------------------------------------------------


def _pagina_pdf(linhas: list[str]) -> pymupdf.Document:
    doc = pymupdf.open()
    pagina = doc.new_page(width=595, height=842)
    y = 60
    for linha in linhas:
        pagina.insert_text((44, y), linha, fontsize=9.5, fontname="helv")
        y += 14
    return doc


def _png(doc: pymupdf.Document, dpi: int = 130) -> bytes:
    # Escala de cinza e pagina recortada na area util: arquivos pequenos.
    area = pymupdf.Rect(0, 0, 595, 420)
    return doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=area).tobytes("png")


def _pdf_escaneado(png: bytes) -> bytes:
    doc = pymupdf.open()
    pagina = doc.new_page(width=595, height=420)
    pagina.insert_image(pagina.rect, stream=png)
    return doc.tobytes(deflate=True)


def _degradar(png: bytes, gerador: random.Random, *, ruido: bool, escala: float, giro: float) -> bytes:
    imagem = Image.open(io.BytesIO(png)).convert("L")
    if escala < 1:
        pequeno = (max(1, int(imagem.width * escala)), max(1, int(imagem.height * escala)))
        imagem = imagem.resize(pequeno).resize(imagem.size)
    if ruido:
        imagem = imagem.filter(ImageFilter.GaussianBlur(1.1))
        pixels = imagem.load()
        for _ in range(imagem.width * imagem.height // 40):
            x, y = gerador.randrange(imagem.width), gerador.randrange(imagem.height)
            pixels[x, y] = gerador.choice((0, 255))
    if giro:
        imagem = imagem.rotate(giro, expand=True, fillcolor=255)
    saida = io.BytesIO()
    imagem.save(saida, "PNG")
    return saida.getvalue()


def _gravar(destino: Path, nome: str, formato: str, doc: pymupdf.Document,
            gerador: random.Random, degradacao: dict | None = None) -> str:
    if formato == "pdf_texto":
        arquivo = f"{nome}.pdf"
        (destino / arquivo).write_bytes(doc.tobytes())
        return arquivo
    imagem = _png(doc, dpi=60 if degradacao and degradacao.get("baixa_resolucao") else 150)
    if degradacao:
        imagem = _degradar(imagem, gerador, ruido=degradacao.get("ruido", False),
                           escala=degradacao.get("escala", 1.0), giro=degradacao.get("giro", 0.0))
    if formato == "pdf_escaneado":
        arquivo = f"{nome}.pdf"
        (destino / arquivo).write_bytes(_pdf_escaneado(imagem))
    elif formato == "jpg":
        arquivo = f"{nome}.jpg"
        saida = io.BytesIO()
        Image.open(io.BytesIO(imagem)).convert("RGB").save(saida, "JPEG", quality=82)
        (destino / arquivo).write_bytes(saida.getvalue())
    else:
        arquivo = f"{nome}.png"
        (destino / arquivo).write_bytes(imagem)
    return arquivo


# --- conjunto ----------------------------------------------------------------------


def gerar_conjunto(destino: Path) -> list[Caso]:
    """Gera os arquivos e o gabarito em `destino`. Deterministico (semente fixa)."""
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    for antigo in destino.iterdir():
        if antigo.suffix.lower() in (".pdf", ".png", ".jpg", ".json"):
            antigo.unlink()
    gerador = random.Random(SEMENTE)
    casos: list[Caso] = []
    contador = {"n": 0}

    def novo_dados(*, vencimento: date | None = None, pagador_doc: str | None = None,
                   com_pagador: bool = True, banco_idx: int | None = None):
        contador["n"] += 1
        n = contador["n"]
        banco = BANCOS[(n if banco_idx is None else banco_idx) % len(BANCOS)]
        nome_b, base_b = BENEFICIARIOS[n % len(BENEFICIARIOS)]
        cnpj_pagador = cnpj_valido(50000000 + n * 7919)
        venc = vencimento or (REFERENCIA + timedelta(days=5 + (n * 3) % 40))
        centavos = 15000 + (n * 48731) % 900000
        barras, linha = gerar_boleto(venc, centavos, banco[0], livre_aleatorio(gerador))
        nome_p = PAGADORES[n % len(PAGADORES)]
        pagador = None
        if com_pagador:
            pagador = (nome_p, pagador_doc or formatar_cnpj(cnpj_pagador))
        dados = Dados(banco, (nome_b, formatar_cnpj(cnpj_valido(base_b))), pagador, venc, centavos, linha)
        return dados, cnpj_pagador, nome_p, centavos, venc, barras

    def adicionar(id_, layout, formato, descricao, esperado, dados, *, cnpj, nome, valor, venc,
                  referencia=REFERENCIA, degradacao=None, nao_avaliados=()):
        doc = _pagina_pdf(_LAYOUTS[layout](dados))
        arquivo = _gravar(destino, id_, formato, doc, gerador, degradacao)
        casos.append(Caso(
            id=id_, arquivo=arquivo, formato=formato, layout=layout, descricao=descricao,
            esperado=esperado, cnpj=cnpj, nome=nome,
            valor=str(valor) if valor is not None else None,
            vencimento=venc.isoformat() if venc else None, referencia=referencia.isoformat(),
            campos_nao_avaliados=list(nao_avaliados),
        ))

    # 1) Casos validos: 4 layouts x 3 formatos.
    for layout in LAYOUTS:
        for formato in FORMATOS:
            d, cnpj, nome, cent, venc, _ = novo_dados()
            adicionar(f"{formato}_{layout}", layout, formato, f"boleto valido, layout {layout}", "aceitar",
                      d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc)

    # 2) JPEG.
    for layout in ("coluna", "linha"):
        d, cnpj, nome, cent, venc, _ = novo_dados()
        adicionar(f"jpg_{layout}", layout, "jpg", f"boleto valido em JPEG, layout {layout}", "aceitar",
                  d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc)

    # 3) CNPJ com 15 digitos (zero a esquerda).
    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.pagador = (d.pagador[0], "0" + formatar_cnpj(cnpj))
    adicionar("pdf_texto_cnpj15", "coluna", "pdf_texto", "CNPJ do pagador impresso com 15 digitos",
              "aceitar", d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc)

    # 4) Vencimento depois do reinicio do fator (2025).
    pos2025 = date(2025, 3, 10)
    d, cnpj, nome, cent, venc, _ = novo_dados(vencimento=pos2025)
    adicionar("pdf_texto_pos2025", "blocos", "pdf_texto", "vencimento em 10/03/2025 (fator reiniciado)",
              "aceitar", d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc, referencia=date(2025, 3, 1))

    # 5) Negativos: o agente DEVE mandar para revisao.
    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.pagador = (nome, formatar_cnpj(cnpj_com_dv_errado(cnpj)))
    adicionar("neg_cnpj_invalido", "coluna", "pdf_texto", "CNPJ do pagador com digito verificador errado",
              "revisao", d, cnpj=None, nome=nome, valor=_dec(cent), venc=venc)

    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.vencimento_texto = venc + timedelta(days=1)
    adicionar("neg_vencimento_divergente", "coluna", "pdf_texto", "vencimento do texto difere da linha digitavel",
              "revisao", d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=None,
              nao_avaliados=["vencimento"])

    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.valor_texto = cent + 10000
    adicionar("neg_valor_divergente", "coluna", "pdf_texto", "valor do texto difere da linha digitavel",
              "revisao", d, cnpj=cnpj, nome=nome, valor=None, venc=venc, nao_avaliados=["valor"])

    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.linha = d.linha[:-1] + str((int(d.linha[-1]) + 1) % 10)
    adicionar("neg_linha_dv_invalido", "coluna", "pdf_texto", "linha digitavel com digito verificador errado",
              "revisao", d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc)

    d, cnpj, nome, cent, venc, _ = novo_dados()
    d.mostrar_linha, d.linha = False, None
    adicionar("neg_sem_linha_digitavel", "coluna", "pdf_texto", "boleto sem linha digitavel",
              "revisao", d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc)

    d, cnpj, nome, cent, venc, _ = novo_dados(com_pagador=False)
    adicionar("neg_sem_pagador", "coluna", "pdf_texto",
              "sem bloco de pagador: so o CNPJ do beneficiario aparece (nao pode ser usado)",
              "revisao", d, cnpj=None, nome=None, valor=_dec(cent), venc=venc)

    d, cnpj, nome, cent, venc, _ = novo_dados(pagador_doc="123.456.789-09")
    adicionar("neg_pagador_cpf", "coluna", "pdf_texto", "pagador pessoa fisica (CPF, sem CNPJ)",
              "revisao", d, cnpj=None, nome=nome, valor=_dec(cent), venc=venc)

    # 6) OCR ruim: correto ou revisao servem; aceitar com dado errado nao.
    d, cnpj, nome, cent, venc, _ = novo_dados()
    adicionar("ocr_ruim_ruido", "coluna", "png", "imagem com ruido, desfoque e inclinacao", "indiferente",
              d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc,
              degradacao={"ruido": True, "giro": 1.8})
    d, cnpj, nome, cent, venc, _ = novo_dados()
    adicionar("ocr_ruim_baixa_resolucao", "linha", "png", "imagem em baixa resolucao", "indiferente",
              d, cnpj=cnpj, nome=nome, valor=_dec(cent), venc=venc,
              degradacao={"baixa_resolucao": True, "escala": 0.6})

    (destino / "gabarito.json").write_text(
        json.dumps({"semente": SEMENTE, "casos": [asdict(c) for c in casos]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return casos


def _dec(centavos: int):
    from decimal import Decimal
    return Decimal(centavos) / 100


def carregar_gabarito(pasta: Path) -> list[Caso]:
    dados = json.loads((Path(pasta) / "gabarito.json").read_text(encoding="utf-8"))
    return [Caso(**caso) for caso in dados["casos"]]

