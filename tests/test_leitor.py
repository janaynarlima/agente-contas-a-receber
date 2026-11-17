from io import BytesIO

import fitz
from PIL import Image
import pytest

from recebiveis.extrator import extrair_texto
from recebiveis.leitor import ler_imagem, ler_pdf


def criar_pdf_textual(caminho):
    documento = fitz.open()
    pagina = documento.new_page()
    pagina.insert_text((72, 72), "Documento sintetico para teste")
    documento.save(caminho)
    documento.close()


def criar_pdf_escaneado(caminho):
    buffer = BytesIO()
    Image.new("RGB", (80, 40), "white").save(buffer, format="PNG")

    documento = fitz.open()
    pagina = documento.new_page()
    pagina.insert_image(pagina.rect, stream=buffer.getvalue())
    documento.save(caminho)
    documento.close()


def test_extrair_texto_de_pdf_com_texto_nativo(tmp_path):
    arquivo = tmp_path / "sintetico.pdf"
    criar_pdf_textual(arquivo)

    assert "Documento sintetico para teste" in ler_pdf(arquivo)


def test_aplicar_ocr_em_pagina_de_pdf_escaneado(tmp_path, monkeypatch):
    arquivo = tmp_path / "escaneado.pdf"
    criar_pdf_escaneado(arquivo)
    chamadas = []

    def ocr_falso(imagem, lang):
        chamadas.append((imagem.size, lang))
        return "Texto sintetico reconhecido"

    monkeypatch.setattr("recebiveis.leitor.pytesseract.image_to_string", ocr_falso)

    assert ler_pdf(arquivo, idioma_ocr="por") == "Texto sintetico reconhecido"
    assert chamadas and chamadas[0][1] == "por"


def test_aplicar_ocr_em_imagem(tmp_path, monkeypatch):
    arquivo = tmp_path / "sintetica.png"
    Image.new("RGB", (40, 20), "white").save(arquivo)
    chamadas = []

    def ocr_falso(imagem, lang):
        chamadas.append((imagem.mode, lang))
        return "Texto de imagem sintetica"

    monkeypatch.setattr("recebiveis.leitor.pytesseract.image_to_string", ocr_falso)

    assert ler_imagem(arquivo, idioma_ocr="por") == "Texto de imagem sintetica"
    assert chamadas == [("RGB", "por")]


def test_rotear_extensoes_de_imagem_sem_diferenciar_maiusculas(tmp_path, monkeypatch):
    arquivo = tmp_path / "imagem.JPEG"
    arquivo.touch()
    monkeypatch.setattr("recebiveis.extrator.ler_imagem", lambda *args, **kwargs: "texto")

    assert extrair_texto(arquivo) == "texto"


def test_rejeitar_formato_nao_suportado(tmp_path):
    arquivo = tmp_path / "documento.txt"
    arquivo.write_text("conteudo", encoding="utf-8")

    with pytest.raises(ValueError, match="Formato nao suportado"):
        extrair_texto(arquivo)
