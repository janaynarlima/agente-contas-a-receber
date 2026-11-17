"""Front-end local para subir boletos e acompanhar o processamento.

Segurança e privacidade:
  - sem rota que sirva os arquivos de entrada/ (boleto so e lido, nunca exibido);
  - extensao E conteudo (bytes iniciais) validados; nome gerado/sanitizado;
  - nunca sobrescreve arquivo existente; conteudo identico e descartado;
  - token CSRF e verificacao de Origin nos POST; CSP restritiva;
  - CNPJ mascarado na tela; nenhum e-mail e enviado por aqui.

O processamento reusa `reconciliar` (mesmas travas do agente por linha de
comando). Baixa manual e confirmacao de baixas em massa NAO existem na
interface: continuam exigindo decisao humana por outro canal.
"""

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

from recebiveis.config import Config, carregar_config
from recebiveis.estado import Estado
from recebiveis.fila import id_curto
from recebiveis.mascara import mascarar_cnpj
from recebiveis.reconciliacao import EXTENSOES_ACEITAS, leitor_padrao, reconciliar

TAMANHO_MAXIMO_ARQUIVO = 20 * 1024 * 1024
TAMANHO_MAXIMO_REQUISICAO = 100 * 1024 * 1024

_ASSINATURAS = {
    ".pdf": (b"%PDF-",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
}


ROTULOS_AVISO = {
    "pendente": "pendente",
    "aguardando_aprovacao": "aguardando aprovação",
    "aprovado": "aprovado",
    "enviado": "enviado",
    "cancelado": "cancelado",
    "em_revisao": "em revisão",
}


def config_do_ambiente() -> Config:
    """Carrega o config.yaml e aplica sobrescritas de ambiente (Docker)."""
    # As sobrescritas RECEBIVEIS_* sao aplicadas por carregar_config, o que faz a
    # tela e o terminal usarem exatamente a mesma configuracao.
    return carregar_config(os.environ.get("RECEBIVEIS_CONFIG", "config.yaml"))


def validar_conteudo(nome: str, inicio: bytes) -> bool:
    """O conteudo precisa ser coerente com a extensao declarada."""
    extensao = Path(nome).suffix.lower()
    return any(inicio.startswith(assinatura) for assinatura in _ASSINATURAS.get(extensao, ()))


def _nome_livre(pasta: Path, nome: str) -> Path:
    destino = pasta / nome
    contador = 1
    while destino.exists():
        destino = pasta / f"{Path(nome).stem} ({contador}){Path(nome).suffix}"
        contador += 1
    return destino


def _hashes_existentes(pasta: Path) -> set[str]:
    resultado = set()
    for arquivo in pasta.rglob("*"):
        if arquivo.is_file() and arquivo.suffix.lower() in EXTENSOES_ACEITAS:
            resultado.add(hashlib.sha256(arquivo.read_bytes()).hexdigest())
    return resultado


def criar_app(config: Config | None = None, leitor=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAXIMO_REQUISICAO
    app.secret_key = os.environ.get("RECEBIVEIS_SEGREDO") or secrets.token_hex(32)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"

    cfg = config or config_do_ambiente()
    cfg.entrada.mkdir(parents=True, exist_ok=True)
    trava = threading.Lock()  # um processamento por vez (SQLite + OCR)
    ler = leitor or leitor_padrao(cfg.idioma_ocr)
    app.extensions["recebiveis_config"] = cfg

    # --- seguranca -------------------------------------------------------------

    def token_csrf() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def proteger_posts():
        if request.method != "POST":
            return
        enviado = request.form.get("csrf", "")
        if not hmac.compare_digest(enviado, session.get("csrf", "")):
            abort(400, "token CSRF invalido")
        origem = request.headers.get("Origin")
        if origem and urlparse(origem).netloc != request.host:
            abort(400, "origem nao permitida")

    @app.after_request
    def cabecalhos(resposta):
        resposta.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
        )
        resposta.headers["X-Content-Type-Options"] = "nosniff"
        resposta.headers["X-Frame-Options"] = "DENY"
        resposta.headers["Referrer-Policy"] = "no-referrer"
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    app.jinja_env.globals["csrf"] = token_csrf

    # --- dados de apoio -----------------------------------------------------------

    def listar_arquivos() -> list[dict]:
        arquivos = []
        for arquivo in sorted(cfg.entrada.rglob("*")):
            if arquivo.is_file() and arquivo.suffix.lower() in EXTENSOES_ACEITAS:
                info = arquivo.stat()
                arquivos.append(
                    {
                        "nome": str(arquivo.relative_to(cfg.entrada)),
                        "kb": max(1, round(info.st_size / 1024)),
                        "quando": datetime.fromtimestamp(info.st_mtime).strftime("%d/%m/%Y %H:%M"),
                    }
                )
        return arquivos

    def listar_boletos() -> list[dict]:
        with Estado(cfg.banco) as estado:
            boletos = estado.listar_boletos()
            for boleto in boletos:
                boleto["avisos"] = [
                    {"tipo": a["tipo_aviso"], "status": a["status"],
                     "rotulo": ROTULOS_AVISO.get(a["status"], a["status"])}
                    for a in estado.historico_avisos(boleto["boleto_id"])
                ]
        for boleto in boletos:
            boleto["cnpj_mascarado"] = mascarar_cnpj(boleto["cnpj_pagador"]) or "—"
            # Mesmo codigo curto (hash) do terminal e da fila: unico por boleto.
            boleto["id_curto"] = (
                "sem identificador" if boleto["boleto_id"].startswith("doc:")
                else "#" + id_curto(boleto["boleto_id"])
            )
        return boletos

    def resumo(resultado) -> list[tuple[str, str]]:
        if resultado.abortada:
            return [("erro", f"Reconciliação abortada, nada foi baixado: {resultado.motivo_abortar}")]
        linhas = [
            ("ok", f"{len(resultado.novos)} novo(s), {len(resultado.baixados)} baixado(s), "
                   f"{len(resultado.reabertos)} reaberto(s)."),
        ]
        if resultado.sem_identificador:
            linhas.append(("aviso", "Sem linha digitável válida (foram para revisão): "
                                    + ", ".join(resultado.sem_identificador)))
        if resultado.motivo_bloqueio:
            linhas.append(("aviso", f"Baixas bloqueadas: {resultado.motivo_bloqueio}. "
                                    "A confirmação exige decisão humana fora desta tela."))
        if resultado.duplicados:
            linhas.append(("aviso", f"{len(resultado.duplicados)} boleto(s) em mais de um arquivo."))
        return linhas

    def processar() -> None:
        with trava:
            with Estado(cfg.banco) as estado:
                resultado = reconciliar(estado, cfg, date.today(), ler)
        for nivel, texto in resumo(resultado):
            flash(texto, nivel)

    # --- rotas -----------------------------------------------------------------------

    @app.get("/saude")
    def saude():
        return {"status": "ok"}

    @app.get("/")
    def inicio():
        boletos = listar_boletos()
        em_aprovacao = sum(
            1 for b in boletos for a in b["avisos"] if a["status"] == "aguardando_aprovacao"
        )
        return render_template(
            "index.html",
            arquivos=listar_arquivos(),
            boletos=boletos,
            em_aprovacao=em_aprovacao,
            extensoes=", ".join(sorted(EXTENSOES_ACEITAS)),
            maximo_mb=TAMANHO_MAXIMO_ARQUIVO // (1024 * 1024),
        )

    @app.post("/enviar")
    def enviar():
        recebidos = [f for f in request.files.getlist("arquivos") if f and f.filename]
        if not recebidos:
            flash("Selecione ao menos um arquivo.", "aviso")
            return redirect(url_for("inicio"))

        conhecidos = _hashes_existentes(cfg.entrada)
        salvos = 0
        for arquivo in recebidos:
            nome = secure_filename(arquivo.filename or "")
            extensao = Path(nome).suffix.lower()
            if extensao not in EXTENSOES_ACEITAS:
                flash(f"{arquivo.filename}: formato não aceito (use {', '.join(sorted(EXTENSOES_ACEITAS))}).", "erro")
                continue
            conteudo = arquivo.read(TAMANHO_MAXIMO_ARQUIVO + 1)
            if len(conteudo) > TAMANHO_MAXIMO_ARQUIVO:
                flash(f"{nome}: maior que {TAMANHO_MAXIMO_ARQUIVO // (1024 * 1024)} MB.", "erro")
                continue
            if not validar_conteudo(nome, conteudo[:16]):
                flash(f"{nome}: o conteúdo não corresponde à extensão {extensao}.", "erro")
                continue
            resumo_hash = hashlib.sha256(conteudo).hexdigest()
            if resumo_hash in conhecidos:
                flash(f"{nome}: já existe um arquivo idêntico em entrada/ (ignorado).", "aviso")
                continue

            destino = _nome_livre(cfg.entrada, nome)
            temporario = destino.with_name(destino.name + ".parte")
            temporario.write_bytes(conteudo)
            temporario.replace(destino)  # gravacao atomica: nunca fica arquivo pela metade
            conhecidos.add(resumo_hash)
            salvos += 1

        if salvos:
            flash(f"{salvos} arquivo(s) enviado(s) para entrada/.", "ok")
            if request.form.get("processar"):
                processar()
        return redirect(url_for("inicio"))

    @app.post("/processar")
    def processar_entrada():
        processar()
        return redirect(url_for("inicio"))

    return app


def criar_app_do_ambiente() -> Flask:
    """Ponto de entrada do gunicorn: `recebiveis.web:criar_app_do_ambiente()`."""
    return criar_app()
