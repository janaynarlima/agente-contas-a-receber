"""Log estruturado em JSON (um objeto por linha).

Cada linha traz o id da execucao (rastreio), a versao do codigo e a versao do
prompt. Os chamadores ja passam dados mascarados; como defesa adicional, todo
texto passa por um filtro que mascara CNPJ, e-mail e codigo de barras.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from recebiveis.mascara import mascarar_cnpj, mascarar_email

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_CODIGO_BARRAS = re.compile(r"(?<!\d)\d{44}(?!\d)")
_CNPJ_FORMATADO = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_CNPJ_NUMERICO = re.compile(r"(?<!\d)\d{14}(?!\d)")


def mascarar_texto(texto: str) -> str:
    texto = _EMAIL.sub(lambda m: mascarar_email(m.group()), texto)
    texto = _CODIGO_BARRAS.sub(lambda m: "***" + m.group()[-6:], texto)
    texto = _CNPJ_FORMATADO.sub(lambda m: mascarar_cnpj(m.group()), texto)
    return _CNPJ_NUMERICO.sub(lambda m: mascarar_cnpj(m.group()), texto)


def _limpar(valor):
    if isinstance(valor, str):
        return mascarar_texto(valor)
    if isinstance(valor, dict):
        return {chave: _limpar(item) for chave, item in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_limpar(item) for item in valor]
    return valor


class LogJson:
    def __init__(self, caminho: Path | None, *, versao_codigo: str, versao_prompt: str):
        self.caminho = Path(caminho) if caminho else None
        self.execucao_id = uuid.uuid4().hex[:12]
        self.versao_codigo = versao_codigo
        self.versao_prompt = versao_prompt
        self.linhas: list[dict] = []
        if self.caminho:
            self.caminho.parent.mkdir(parents=True, exist_ok=True)

    def registrar(self, evento: str, **campos) -> dict:
        linha = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "execucao_id": self.execucao_id,
            "versao_codigo": self.versao_codigo,
            "versao_prompt": self.versao_prompt,
            "evento": evento,
            **_limpar(campos),
        }
        self.linhas.append(linha)
        if self.caminho:
            with self.caminho.open("a", encoding="utf-8") as arquivo:
                arquivo.write(json.dumps(linha, ensure_ascii=False) + "\n")
        return linha
