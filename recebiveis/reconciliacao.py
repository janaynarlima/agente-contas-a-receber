"""Reconciliacao de baixa: boleto pago = arquivo nao esta mais em entrada/.

Roda ANTES do calculo de avisos. Principios:
  - o boleto e identificado pelo codigo de barras (linha digitavel), nunca
    pelo nome do arquivo: renomear ou mover para subpasta nao e pagamento;
  - na duvida, NAO baixar. Pasta inexistente/inacessivel, falha de leitura de
    qualquer arquivo ou arquivo sem identificador confiavel bloqueiam as baixas;
  - baixa acima de max_percentual_baixas interrompe e pede confirmacao humana;
  - boleto em revisao nunca e baixado automaticamente;
  - boleto pago que reaparece volta a aberto, marcado para revisao humana.
"""

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from recebiveis.config import Config
from recebiveis.estado import STATUS_AVISO_ABERTOS, Estado
from recebiveis.extrator import (
    EXTENSOES_IMAGEM,
    DadosBoleto,
    extrair_dados_boleto,
    extrair_texto,
)

EXTENSOES_ACEITAS = frozenset(EXTENSOES_IMAGEM | {".pdf"})
MOTIVO_BAIXA = "arquivo removido da pasta"
PREFIXO_SEM_ID = "doc:"

Leitor = Callable[[Path], DadosBoleto]


class ErroReconciliacao(RuntimeError):
    """Entrada inacessivel ou ilegivel: a reconciliacao deve ser abortada."""


@dataclass
class Documento:
    caminho: Path
    dados: DadosBoleto
    hash_conteudo: str

    @property
    def boleto_id(self) -> str:
        # Sem codigo de barras valido nao ha identificador confiavel; o hash do
        # conteudo serve so para a fila de revisao, nunca para baixa ou aviso.
        return self.dados.codigo_barras or PREFIXO_SEM_ID + self.hash_conteudo


@dataclass
class Varredura:
    documentos: list[Documento] = field(default_factory=list)

    @property
    def identificados(self) -> dict[str, Documento]:
        return {d.boleto_id: d for d in self.documentos if d.dados.codigo_barras}

    @property
    def sem_identificador(self) -> list[Documento]:
        return [d for d in self.documentos if not d.dados.codigo_barras]

    @property
    def duplicados(self) -> list[str]:
        vistos: set[str] = set()
        repetidos: list[str] = []
        for documento in self.documentos:
            if documento.dados.codigo_barras:
                if documento.boleto_id in vistos:
                    repetidos.append(documento.boleto_id)
                vistos.add(documento.boleto_id)
        return repetidos


@dataclass
class Resultado:
    abortada: bool = False
    motivo_abortar: str = ""
    novos: list[str] = field(default_factory=list)
    reabertos: list[str] = field(default_factory=list)
    baixados: list[str] = field(default_factory=list)
    baixas_bloqueadas: list[str] = field(default_factory=list)
    motivo_bloqueio: str = ""
    precisa_confirmacao: bool = False
    avisos_cancelados: int = 0
    duplicados: list[str] = field(default_factory=list)
    sem_identificador: list[str] = field(default_factory=list)
    eventos: list[dict] = field(default_factory=list)
    documentos_lidos: int = 0
    segundos_leitura: float = 0.0


def mascarar_id(boleto_id: str) -> str:
    """Mascara o identificador para logs: final + hash curto (sem colisao).

    Boletos do mesmo beneficiario costumam terminar igual, entao so o final nao
    distingue um boleto do outro; o hash permite correlacionar linhas do log.
    """
    resumo = hashlib.sha256(boleto_id.encode()).hexdigest()[:4]
    return f"***{boleto_id[-6:]}#{resumo}"


def leitor_padrao(idioma_ocr: str = "por", referencia: date | None = None) -> Leitor:
    def ler(caminho: Path) -> DadosBoleto:
        texto = extrair_texto(caminho, idioma_ocr=idioma_ocr)
        return extrair_dados_boleto(texto, referencia=referencia)

    return ler


def _hash_arquivo(caminho: Path) -> str:
    resumo = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            resumo.update(bloco)
    return resumo.hexdigest()


def _erro_de_varredura(erro: OSError) -> None:
    raise ErroReconciliacao(f"falha ao percorrer a pasta de entrada: {erro}") from erro


def varrer_entrada(pasta: Path, ler: Leitor) -> Varredura:
    """Le entrada/ recursivamente. Qualquer falha levanta ErroReconciliacao."""
    pasta = Path(pasta)
    if not pasta.is_dir():
        raise ErroReconciliacao(f"pasta de entrada inexistente ou inacessivel: {pasta}")

    varredura = Varredura()
    for diretorio, _subpastas, arquivos in os.walk(pasta, onerror=_erro_de_varredura):
        for nome in sorted(arquivos):
            if nome.startswith(("~$", ".")) or Path(nome).suffix.lower() not in EXTENSOES_ACEITAS:
                continue
            caminho = Path(diretorio) / nome
            try:
                dados = ler(caminho)
                hash_conteudo = _hash_arquivo(caminho)
            except Exception as erro:  # qualquer falha de leitura bloqueia as baixas
                raise ErroReconciliacao(
                    f"falha de leitura em {caminho.name}: {type(erro).__name__}"
                ) from erro
            varredura.documentos.append(Documento(caminho, dados, hash_conteudo))
    return varredura


def boleto_ainda_na_entrada(pasta: Path, boleto_id: str, ler: Leitor) -> bool:
    """Verificacao imediatamente antes de enviar um e-mail.

    Qualquer duvida (pasta inacessivel, falha de leitura) retorna False: nao
    enviar.
    """
    try:
        return boleto_id in varrer_entrada(pasta, ler).identificados
    except ErroReconciliacao:
        return False


def remover_da_fila(arquivo: str | None, pasta_fila: Path) -> None:
    """Apaga o e-mail da fila, mas so se estiver dentro da pasta da fila."""
    if not arquivo:
        return
    caminho = Path(arquivo).resolve()
    if caminho.parent == Path(pasta_fila).resolve():
        caminho.unlink(missing_ok=True)


def _evento(resultado: Resultado, tipo: str, **campos) -> None:
    resultado.eventos.append({"evento": tipo, **campos})


def reconciliar(
    estado: Estado,
    config: Config,
    hoje: date,
    ler: Leitor,
    *,
    confirmar_baixas_acima_do_limite: bool = False,
) -> Resultado:
    resultado = Resultado()

    inicio_leitura = time.perf_counter()
    try:
        varredura = varrer_entrada(config.entrada, ler)
    except ErroReconciliacao as erro:
        resultado.abortada = True
        resultado.motivo_abortar = str(erro)
        _evento(resultado, "reconciliacao_abortada", motivo=str(erro))
        return resultado

    resultado.segundos_leitura = time.perf_counter() - inicio_leitura
    resultado.documentos_lidos = len(varredura.documentos)
    identificados = varredura.identificados
    resultado.duplicados = [mascarar_id(i) for i in varredura.duplicados]
    for duplicado in resultado.duplicados:
        _evento(resultado, "boleto_duplicado_na_entrada", boleto=duplicado)

    # 1. Registrar boletos novos e tratar reaparecimentos.
    for boleto_id, documento in identificados.items():
        dados = documento.dados
        existente = estado.obter_boleto(boleto_id)
        if existente is None:
            estado.registrar_boleto(
                boleto_id,
                hoje=hoje,
                cnpj_pagador=dados.cnpj_pagador,
                nome_pagador=dados.nome_pagador,
                valor=dados.valor,
                vencimento=dados.vencimento,
                motivos_revisao=dados.motivos_revisao,
            )
            resultado.novos.append(mascarar_id(boleto_id))
            _evento(resultado, "boleto_novo", boleto=mascarar_id(boleto_id))
        elif existente["status"] == "pago":
            if estado.reabrir(boleto_id, hoje, "boleto pago reapareceu em entrada/"):
                resultado.reabertos.append(mascarar_id(boleto_id))
                _evento(resultado, "boleto_reaberto", boleto=mascarar_id(boleto_id))

    # 2. Arquivos sem identificador confiavel: revisao por hash; bloqueiam baixas.
    hashes_presentes = {d.hash_conteudo for d in varredura.sem_identificador}
    for documento in varredura.sem_identificador:
        motivos = ("sem identificador FEBRABAN valido",) + documento.dados.motivos_revisao
        if estado.registrar_boleto(
            documento.boleto_id, hoje=hoje, motivos_revisao=motivos
        ):
            _evento(resultado, "documento_sem_identificador", documento=documento.caminho.name)
        resultado.sem_identificador.append(documento.caminho.name)

    # Registros provisorios cujo arquivo sumiu ou passou a ser identificavel.
    for registro in estado.listar_boletos("em_revisao"):
        if registro["boleto_id"].startswith(PREFIXO_SEM_ID):
            hash_registro = registro["boleto_id"][len(PREFIXO_SEM_ID):]
            if hash_registro not in hashes_presentes:
                estado.excluir_boleto_sem_avisos(registro["boleto_id"])

    # 3. Baixas.
    abertos = estado.listar_boletos("aberto")
    candidatos = [
        b["boleto_id"]
        for b in abertos
        if b["boleto_id"] not in identificados and not b["requer_revisao"]
    ]

    if varredura.sem_identificador:
        resultado.baixas_bloqueadas = [mascarar_id(i) for i in candidatos]
        resultado.motivo_bloqueio = (
            "arquivos sem identificador confiavel em entrada/: nao e possivel "
            "afirmar que algum boleto saiu da pasta"
        )
        if candidatos:
            _evento(resultado, "baixas_bloqueadas", motivo=resultado.motivo_bloqueio,
                    quantidade=len(candidatos))
        return resultado

    percentual = 100 * len(candidatos) / len(abertos) if abertos else 0
    if candidatos and percentual > config.max_percentual_baixas and not confirmar_baixas_acima_do_limite:
        resultado.baixas_bloqueadas = [mascarar_id(i) for i in candidatos]
        resultado.precisa_confirmacao = True
        resultado.motivo_bloqueio = (
            f"{len(candidatos)} de {len(abertos)} boletos abertos ({percentual:.0f}%) "
            f"excede max_percentual_baixas={config.max_percentual_baixas:g}%"
        )
        _evento(resultado, "baixas_bloqueadas", motivo=resultado.motivo_bloqueio,
                quantidade=len(candidatos))
        return resultado

    for boleto_id in candidatos:
        abertos_do_boleto = [
            aviso
            for aviso in estado.historico_avisos(boleto_id)
            if aviso["status"] in STATUS_AVISO_ABERTOS
        ]
        pendentes = len(abertos_do_boleto)
        if estado.marcar_pago(boleto_id, hoje, MOTIVO_BAIXA):
            resultado.baixados.append(mascarar_id(boleto_id))
            resultado.avisos_cancelados += pendentes
            # Aprovacoes pendentes tambem caem: remove o e-mail da fila.
            for aviso in abertos_do_boleto:
                remover_da_fila(aviso.get("arquivo_fila"), config.fila_aprovacao)
            _evento(resultado, "boleto_baixado", boleto=mascarar_id(boleto_id),
                    motivo=MOTIVO_BAIXA, avisos_cancelados=pendentes)

    return resultado

