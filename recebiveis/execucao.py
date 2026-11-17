"""Execucao completa: python -m recebiveis run [--enviar] [--sem-llm].

Ordem (ver Prompt): reconciliacao de baixa ANTES de calcular avisos; depois o
plano de avisos, a fila de aprovacao e a entrega. Modo padrao: DRY-RUN.

Trava de seguranca: se a reconciliacao for abortada ou tiver baixas bloqueadas,
a fase de avisos NAO roda. Sem saber o que ja foi pago, o agente poderia cobrar
quem pagou.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from recebiveis.avisos import ADIADO, ENVIAR, FILA_APROVACAO, REVISAO, Decisao, planejar_execucao
from recebiveis.clientes import ErroClientes, carregar_clientes
from recebiveis.config import Config
from recebiveis.estado import Estado
from recebiveis.fila import enfileirar, entregar
from recebiveis.logs import LogJson
from recebiveis.reconciliacao import Leitor, Resultado, mascarar_id, reconciliar


@dataclass
class Execucao:
    hoje: date
    enviar: bool
    sem_llm: bool
    reconciliacao: Resultado | None = None
    avisos_executados: bool = False
    avisos_nao_executados_motivo: str = ""
    erros: list[str] = field(default_factory=list)
    decisoes: Counter = field(default_factory=Counter)        # acao -> quantidade
    fila: Counter = field(default_factory=Counter)             # situacao do enfileiramento
    entregas: Counter = field(default_factory=Counter)         # situacao da entrega
    motivos_revisao: Counter = field(default_factory=Counter)  # motivo -> boletos
    avisos_em_revisao: int = 0
    segundos_total: float = 0.0
    execucao_id: str = ""
    versao_codigo: str = ""
    versao_prompt: str = ""

    @property
    def codigo_saida(self) -> int:
        return 1 if self.erros or (self.reconciliacao and self.reconciliacao.abortada) else 0


def executar(
    config: Config,
    env: dict[str, str],
    hoje: date,
    *,
    enviar: bool,
    sem_llm: bool,
    leitor: Leitor,
    presenca: Callable[[str], bool],
    log: LogJson,
    relogio: Callable[[], float] = time.perf_counter,
) -> Execucao:
    execucao = Execucao(
        hoje=hoje, enviar=enviar, sem_llm=sem_llm, execucao_id=log.execucao_id,
        versao_codigo=log.versao_codigo, versao_prompt=log.versao_prompt,
    )
    inicio = relogio()
    log.registrar("entrada", modo="envio_real" if enviar else "dry_run", sem_llm=sem_llm,
                  data=hoje.isoformat())

    try:
        with Estado(config.banco) as estado:
            _reconciliar(execucao, estado, config, hoje, leitor, log)
            if execucao.avisos_executados is False and not execucao.avisos_nao_executados_motivo:
                _avisos(execucao, estado, config, env, hoje, enviar, presenca, log, relogio)
            _fotografar_revisoes(execucao, estado)
    except Exception as erro:  # nenhuma falha pode passar em silencio
        execucao.erros.append(f"erro inesperado: {type(erro).__name__}")
        log.registrar("erro", tipo=type(erro).__name__)

    execucao.segundos_total = relogio() - inicio
    log.registrar(
        "fim", segundos=round(execucao.segundos_total, 3), erros=len(execucao.erros),
        decisoes=dict(execucao.decisoes), entregas=dict(execucao.entregas),
    )
    return execucao


def _reconciliar(execucao, estado, config, hoje, leitor, log) -> None:
    resultado = reconciliar(estado, config, hoje, leitor)
    execucao.reconciliacao = resultado
    for evento in resultado.eventos:
        campos = {k: v for k, v in evento.items() if k != "evento"}
        log.registrar(_nome_evento(evento["evento"]), **campos)
    log.registrar(
        "validacao", documentos=resultado.documentos_lidos, novos=len(resultado.novos),
        sem_identificador=len(resultado.sem_identificador),
        segundos_leitura=round(resultado.segundos_leitura, 3),
    )

    if resultado.abortada:
        execucao.erros.append(f"reconciliacao abortada: {resultado.motivo_abortar}")
        execucao.avisos_nao_executados_motivo = "reconciliacao abortada"
    elif resultado.motivo_bloqueio:
        execucao.avisos_nao_executados_motivo = (
            f"baixas bloqueadas ({resultado.motivo_bloqueio}); sem saber o que ja foi pago, "
            "nenhum aviso foi calculado"
        )
    if execucao.avisos_nao_executados_motivo:
        log.registrar("avisos_nao_executados", motivo=execucao.avisos_nao_executados_motivo)


def _nome_evento(evento: str) -> str:
    return {
        "boleto_novo": "extracao",
        "documento_sem_identificador": "validacao",
        "boleto_baixado": "baixa",
        "baixas_bloqueadas": "baixa_bloqueada",
        "boleto_reaberto": "reabertura",
        "reconciliacao_abortada": "erro",
    }.get(evento, evento)


def _avisos(execucao, estado, config, env, hoje, enviar, presenca, log, relogio) -> None:
    try:
        clientes = carregar_clientes(config.clientes)
    except ErroClientes as erro:
        execucao.erros.append(f"cadastro de clientes indisponivel: {erro}")
        execucao.avisos_nao_executados_motivo = "cadastro de clientes indisponivel"
        log.registrar("avisos_nao_executados", motivo=str(erro))
        return

    execucao.avisos_executados = True
    decisoes = planejar_execucao(estado, config, hoje)
    envios_feitos = 0

    def pode_enviar_mais() -> bool:
        return envios_feitos < config.max_envios_por_execucao

    def registrar_entrega(d_boleto: str, tipo: str, resultado) -> None:
        nonlocal envios_feitos
        execucao.entregas[resultado.situacao] += 1
        if resultado.situacao in ("enviado", "dry-run"):
            envios_feitos += 1
        log.registrar("envio", boleto=mascarar_id(d_boleto), tipo_aviso=tipo,
                      situacao=resultado.situacao, motivo=resultado.motivo)
        if resultado.situacao in ("falha", "bloqueado"):
            execucao.erros.append(f"entrega {tipo} {mascarar_id(d_boleto)}: {resultado.motivo}")

    for decisao in decisoes:
        execucao.decisoes[decisao.acao] += 1
        log.registrar("decisao", boleto=mascarar_id(decisao.boleto_id), tipo_aviso=decisao.tipo_aviso,
                      acao=decisao.acao, motivo=decisao.motivo, dias_atraso=decisao.dias_atraso)

        if decisao.acao == REVISAO:
            execucao.avisos_em_revisao += 1
        elif decisao.acao == FILA_APROVACAO:
            resultado = enfileirar(estado, config, decisao, clientes, hoje, env)
            execucao.fila[resultado.situacao] += 1
            if resultado.situacao == "revisao":
                execucao.avisos_em_revisao += 1
            log.registrar("aprovacao", boleto=mascarar_id(decisao.boleto_id), tipo_aviso=decisao.tipo_aviso,
                          situacao=resultado.situacao, motivo=resultado.motivo)
        elif decisao.acao == ENVIAR:
            if not pode_enviar_mais():
                execucao.decisoes[ADIADO] += 1
                continue
            registrar_entrega(
                decisao.boleto_id, decisao.tipo_aviso,
                entregar(estado, config, decisao.boleto_id, decisao.tipo_aviso, hoje, clientes, env,
                         enviar=enviar, de="pendente", arquivo_presente=presenca),
            )

    # Avisos aprovados por uma pessoa e ainda nao enviados (por exemplo, aprovados
    # em dry-run ou cujo envio falhou antes).
    for aviso in estado.listar_avisos("aprovado"):
        if not pode_enviar_mais():
            execucao.decisoes[ADIADO] += 1
            continue
        registrar_entrega(
            aviso["boleto_id"], aviso["tipo_aviso"],
            entregar(estado, config, aviso["boleto_id"], aviso["tipo_aviso"], hoje, clientes, env,
                     enviar=enviar, de="aprovado", arquivo_presente=presenca),
        )


def _fotografar_revisoes(execucao: Execucao, estado: Estado) -> None:
    for boleto in estado.listar_boletos():
        if boleto["status"] == "em_revisao" or boleto["requer_revisao"]:
            for motivo in boleto["motivos_revisao"] or ["sem motivo registrado"]:
                execucao.motivos_revisao[motivo] += 1


def gerar_relatorio(execucao: Execucao, config: Config) -> str:
    rec = execucao.reconciliacao or Resultado()
    modo = "ENVIO REAL" if execucao.enviar else "DRY-RUN (nenhum e-mail foi enviado)"
    media = (rec.segundos_leitura / rec.documentos_lidos) if rec.documentos_lidos else None
    linhas = [
        f"# Relatório da execução de {execucao.hoje.strftime('%d/%m/%Y')}",
        "",
        f"- Execução: `{execucao.execucao_id}`",
        f"- Modo: **{modo}**",
        f"- Versão do código: `{execucao.versao_codigo}` · versão do prompt: `{execucao.versao_prompt}`",
        "",
        "## Boletos processados",
        f"- Arquivos lidos em `entrada/`: {rec.documentos_lidos}",
        f"- Boletos novos: {len(rec.novos)}",
        f"- Arquivos sem identificador (linha digitável inválida): {len(rec.sem_identificador)}",
        f"- Boletos em mais de um arquivo: {len(rec.duplicados)}",
        "",
        "## Baixas",
        f"- Boletos baixados (arquivo removido da pasta): {len(rec.baixados)}",
        f"- Boletos reabertos: {len(rec.reabertos)}",
        f"- Baixas bloqueadas: {len(rec.baixas_bloqueadas)}"
        + (f" — {rec.motivo_bloqueio}" if rec.motivo_bloqueio else ""),
        f"- Avisos cancelados por pagamento: {rec.avisos_cancelados}",
    ]
    if rec.precisa_confirmacao:
        linhas.append("- **Atenção:** a baixa em massa excede o limite e exige confirmação humana.")

    linhas += ["", "## Avisos"]
    if execucao.avisos_executados:
        linhas += [
            f"- Decisões: enviar {execucao.decisoes[ENVIAR]}, fila de aprovação "
            f"{execucao.decisoes[FILA_APROVACAO]}, revisão {execucao.decisoes[REVISAO]}, "
            f"adiados {execucao.decisoes[ADIADO]}",
            f"- Fila de aprovação: {dict(execucao.fila) or 'nada novo'}",
            f"- Entregas: {dict(execucao.entregas) or 'nenhuma'}",
        ]
    else:
        linhas.append(f"- **Não executados:** {execucao.avisos_nao_executados_motivo or 'sem motivo registrado'}")

    linhas += ["", "## Revisões humanas pendentes"]
    if execucao.motivos_revisao:
        linhas += [f"- {motivo}: {quantidade} boleto(s)"
                   for motivo, quantidade in execucao.motivos_revisao.most_common()]
    else:
        linhas.append("- Nenhuma.")
    linhas.append(f"- Avisos encaminhados à revisão nesta execução: {execucao.avisos_em_revisao}")

    linhas += ["", "## Erros"]
    linhas += [f"- {erro}" for erro in execucao.erros] or ["- Nenhum."]

    linhas += [
        "",
        "## Métricas desta execução",
        f"- Tempo total: {execucao.segundos_total:.2f} s",
        f"- Tempo médio de leitura por boleto: "
        + (f"{media:.2f} s ({rec.documentos_lidos} arquivos)" if media is not None else "n/d"),
        "- Custo por boleto (LLM): R$ 0,00 — nenhum LLM foi usado "
        + ("(opção --sem-llm)" if execucao.sem_llm else "(o fallback com LLM ainda não está implementado)"),
        "",
        "> Não há métrica de acerto de extração nesta execução: o golden set de 20 boletos "
        "ainda não existe. Qualquer afirmação de desempenho exige o conjunto e a métrica.",
        "",
    ]
    return "\n".join(linhas)


def gravar_relatorio(texto: str, caminho: Path) -> Path:
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".parte")
    temporario.write_text(texto, encoding="utf-8")
    temporario.replace(caminho)
    return caminho
