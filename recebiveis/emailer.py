"""Geracao e envio dos e-mails de aviso.

Regras (ver Prompt):
  - somente templates Jinja em templates/, com as variaveis permitidas:
    nome_cliente, valor, vencimento, numero_boleto;
  - o LLM nao escreve e-mails;
  - proibido: ameacas, juros ou desconto inventados, promessas, linguagem
    agressiva (filtro de termos aplicado a cada template carregado);
  - avisos de vencido: tom cordial e a frase "caso ja tenha efetuado o
    pagamento, desconsidere esta mensagem.";
  - modo padrao e DRY-RUN: o .eml vai para saida/ e nada e enviado. O envio
    real (SMTP) so acontece quando o chamador pede explicitamente (--enviar).
"""

import os
import re
import smtplib
import ssl
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path

from jinja2 import StrictUndefined, TemplateSyntaxError, meta
from jinja2.sandbox import SandboxedEnvironment

VARIAVEIS_PERMITIDAS = frozenset({"nome_cliente", "valor", "vencimento", "numero_boleto"})
FRASE_VENCIDO = "caso já tenha efetuado o pagamento, desconsidere esta mensagem."
TEMPLATE_PREVIO = "aviso_previo.txt.j2"
TEMPLATE_VENCIDO = "aviso_vencido.txt.j2"

# Termos que nao podem aparecer nos templates (comparacao sem acento/caixa).
TERMOS_PROIBIDOS = (
    "juros", "multa", "desconto", "abatimento", "protest", "negativ", "serasa", "ameac",
    "spc", "judicial", "advogado", "processo", "sob pena", "ultima chance",
    "ultimato", "imediatamente", "inadimpl", "calote", "devedor", "cobranca judicial",
    "garantimos", "prometemos", "prazo final", "sem mais aviso",
)


class ErroTemplate(ValueError):
    """Template ausente ou fora das regras."""


class ErroEnvio(RuntimeError):
    """Falha ao entregar o e-mail (por exemplo, SMTP indisponivel)."""


@dataclass(frozen=True)
class Aviso:
    assunto: str
    corpo: str


def _normalizar(texto: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    return sem_acento.casefold()


def _ambiente() -> SandboxedEnvironment:
    # StrictUndefined: variavel ausente vira erro, nunca texto vazio.
    return SandboxedEnvironment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


def validar_template(texto: str, *, vencido: bool, nome: str = "template") -> None:
    """Garante variaveis permitidas, ausencia de termos proibidos e a frase obrigatoria."""
    ambiente = _ambiente()
    try:
        arvore = ambiente.parse(texto)
    except TemplateSyntaxError as erro:
        raise ErroTemplate(f"{nome}: sintaxe invalida ({erro.message})") from erro

    desconhecidas = meta.find_undeclared_variables(arvore) - VARIAVEIS_PERMITIDAS
    if desconhecidas:
        raise ErroTemplate(
            f"{nome}: variaveis nao permitidas: {', '.join(sorted(desconhecidas))}"
        )

    normalizado = _normalizar(texto)
    for termo in TERMOS_PROIBIDOS:
        if termo in normalizado:
            raise ErroTemplate(f"{nome}: termo proibido no e-mail: '{termo}'")

    if vencido and _normalizar(FRASE_VENCIDO) not in normalizado:
        raise ErroTemplate(f"{nome}: aviso de vencido deve conter a frase '{FRASE_VENCIDO}'")
    if not re.match(r"\s*Assunto:\s*\S", texto):
        raise ErroTemplate(f"{nome}: a primeira linha deve ser 'Assunto: ...'")


def nome_do_template(dias: int) -> str:
    """Aviso previo (dias < 0) ou de vencido (dias > 0)."""
    if dias == 0:
        raise ErroTemplate("D0 nao gera aviso")
    return TEMPLATE_VENCIDO if dias > 0 else TEMPLATE_PREVIO


def formatar_valor(valor: Decimal) -> str:
    """1234.5 -> 'R$ 1.234,50'."""
    inteiro, _, decimal = f"{Decimal(valor):.2f}".partition(".")
    com_milhar = f"{int(inteiro):,}".replace(",", ".")
    return f"R$ {com_milhar},{decimal}"


def formatar_data(data: date) -> str:
    return data.strftime("%d/%m/%Y")


def renderizar_aviso(
    templates: Path,
    dias: int,
    *,
    nome_cliente: str,
    valor: Decimal,
    vencimento: date,
    numero_boleto: str,
) -> Aviso:
    """Renderiza o aviso a partir do template; so as 4 variaveis permitidas."""
    arquivo = Path(templates) / nome_do_template(dias)
    if not arquivo.is_file():
        raise ErroTemplate(f"template nao encontrado: {arquivo.name}")
    texto = arquivo.read_text(encoding="utf-8")
    validar_template(texto, vencido=dias > 0, nome=arquivo.name)

    renderizado = _ambiente().from_string(texto).render(
        nome_cliente=nome_cliente,
        valor=formatar_valor(valor),
        vencimento=formatar_data(vencimento),
        numero_boleto=numero_boleto,
    )
    primeira, _, corpo = renderizado.partition("\n")
    assunto = re.sub(r"^\s*Assunto:\s*", "", primeira).strip()
    if not assunto or any(c in assunto for c in "\r\n"):
        raise ErroTemplate(f"{arquivo.name}: assunto invalido")
    return Aviso(assunto=assunto, corpo=corpo.lstrip("\n"))


def montar_mensagem(
    aviso: Aviso, *, remetente: str, destinatario: str, bcc: str = ""
) -> EmailMessage:
    mensagem = EmailMessage()
    mensagem["From"] = remetente
    mensagem["To"] = destinatario
    if bcc:
        mensagem["Bcc"] = bcc
    mensagem["Subject"] = aviso.assunto
    mensagem.set_content(aviso.corpo)
    return mensagem


def _nome_seguro(nome: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", nome)[:120]


def gravar_eml(mensagem: EmailMessage, saida: Path, nome_base: str) -> Path:
    """Grava o .eml em saida/ (dry-run). Nunca escreve fora dessa pasta."""
    saida = Path(saida)
    saida.mkdir(parents=True, exist_ok=True)
    destino = (saida / f"{_nome_seguro(nome_base)}.eml").resolve()
    if destino.parent != saida.resolve():
        raise ErroEnvio("caminho de saida invalido")
    temporario = destino.with_name(destino.name + ".parte")
    temporario.write_bytes(bytes(mensagem))
    temporario.replace(destino)
    return destino


def carregar_env(caminho: str | Path = ".env") -> dict[str, str]:
    """Le um .env simples (CHAVE=valor); ambiente do processo tem precedencia."""
    valores: dict[str, str] = {}
    arquivo = Path(caminho)
    if arquivo.is_file():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            valores[chave.strip()] = valor.strip().strip('"').strip("'")
    for chave in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_REMETENTE"):
        if os.environ.get(chave):
            valores[chave] = os.environ[chave]
    return valores


def enviar_smtp(mensagem: EmailMessage, env: dict[str, str], *, timeout: float = 30) -> None:
    """Envia via SMTP. SO deve ser chamada com --enviar explicito.

    Porta 465 usa SSL; as demais usam STARTTLS. Qualquer falha vira ErroEnvio,
    para o chamador devolver o aviso a pendente e tentar numa proxima execucao.
    """
    host = env.get("SMTP_HOST")
    porta_texto = env.get("SMTP_PORT", "587")
    if not host or not porta_texto.isdigit():
        raise ErroEnvio("SMTP_HOST/SMTP_PORT nao configurados")
    porta = int(porta_texto)
    contexto = ssl.create_default_context()

    try:
        if porta == 465:
            servidor = smtplib.SMTP_SSL(host, porta, timeout=timeout, context=contexto)
        else:
            servidor = smtplib.SMTP(host, porta, timeout=timeout)
        with servidor:
            if porta != 465:
                servidor.starttls(context=contexto)
            if env.get("SMTP_USER"):
                servidor.login(env["SMTP_USER"], env.get("SMTP_PASS", ""))
            servidor.send_message(mensagem)
    except (smtplib.SMTPException, OSError) as erro:
        # A mensagem de erro nao inclui destinatario nem credenciais.
        raise ErroEnvio(f"falha no envio SMTP: {type(erro).__name__}") from erro
