"""Leitura de PDFs e imagens, com OCR local para paginas sem texto."""

from pathlib import Path

import fitz
from PIL import Image
import pytesseract


def ler_pdf(caminho: str | Path, idioma_ocr: str = "por") -> str:
    """Extrai texto selecionavel do PDF e aplica OCR nas paginas sem texto."""
    arquivo = Path(caminho)
    if not arquivo.is_file():
        raise FileNotFoundError(f"Arquivo PDF nao encontrado: {arquivo}")

    paginas: list[str] = []
    with fitz.open(arquivo) as documento:
        for pagina in documento:
            texto = pagina.get_text("text").strip()
            if texto:
                paginas.append(texto)
                continue

            pixmap = pagina.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            imagem = Image.frombytes(
                "RGB",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            paginas.append(pytesseract.image_to_string(imagem, lang=idioma_ocr).strip())

    return "\n".join(texto for texto in paginas if texto)


def ler_imagem(caminho: str | Path, idioma_ocr: str = "por") -> str:
    """Aplica OCR local a uma imagem."""
    arquivo = Path(caminho)
    if not arquivo.is_file():
        raise FileNotFoundError(f"Imagem nao encontrada: {arquivo}")

    with Image.open(arquivo) as imagem:
        return pytesseract.image_to_string(imagem.convert("RGB"), lang=idioma_ocr).strip()
