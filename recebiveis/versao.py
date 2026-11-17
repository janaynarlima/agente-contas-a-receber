"""Versoes registradas nos logs (rastreabilidade de codigo e de prompt)."""

import hashlib
from pathlib import Path

VERSAO = "0.4.0"
_RAIZ = Path(__file__).resolve().parent


def versao_codigo() -> str:
    """Versao + hash curto do codigo-fonte: muda quando qualquer modulo muda."""
    resumo = hashlib.sha256()
    for arquivo in sorted(_RAIZ.rglob("*.py")):
        resumo.update(arquivo.relative_to(_RAIZ).as_posix().encode())
        resumo.update(arquivo.read_bytes())
    return f"{VERSAO}+{resumo.hexdigest()[:8]}"


def versao_prompt(caminho: str | Path | None = None) -> str:
    """Hash curto do arquivo Prompt (a especificacao). 'indisponivel' se ausente."""
    arquivo = Path(caminho) if caminho else _RAIZ.parent / "Prompt"
    if not arquivo.is_file():
        return "indisponivel"
    return hashlib.sha256(arquivo.read_bytes()).hexdigest()[:12]
