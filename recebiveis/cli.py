"""Linha de comando: python -m recebiveis <comando>.

Comandos:
  run [--enviar] [--sem-llm]      execucao completa (compativel com cron e com o
                                  Agendador de Tarefas do Windows)
  aprovar [--listar] [--enviar]   revisar a fila de aprovacao
  reabrir <id>                    reabrir um boleto baixado (decisao humana)

Sem --enviar o resultado e sempre dry-run: o .eml vai para saida/ e nada e
enviado. O envio real so acontece com --enviar explicito.
"""

import argparse
import getpass
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from recebiveis.clientes import ErroClientes, buscar_cliente, carregar_clientes, email_valido
from recebiveis.config import ErroConfig, carregar_config
from recebiveis.emailer import (
    Aviso,
    ErroEnvio,
    carregar_env,
    enviar_smtp,
    formatar_data,
    formatar_valor,
    montar_mensagem,
)
from recebiveis.estado import Estado, ErroEstado
from recebiveis.execucao import executar, gerar_relatorio, gravar_relatorio
from recebiveis.fila import entregar, id_curto
from recebiveis.logs import LogJson
from recebiveis.versao import versao_codigo, versao_prompt
from recebiveis.mascara import mascarar_cnpj, mascarar_email
from recebiveis.reconciliacao import ErroReconciliacao, leitor_padrao, remover_da_fila, varrer_entrada

Saida = Callable[[str], None]
Entrada = Callable[[str], str]


def _historico(estado: Estado, boleto_id: str) -> list[str]:
    linhas = []
    for aviso in estado.historico_avisos(boleto_id):
        detalhe = {
            "enviado": f"enviado em {aviso['data_envio']}",
            "cancelado": f"cancelado ({aviso['motivo'] or 'sem motivo'})",
            "em_revisao": f"em revisao ({aviso['motivo'] or 'sem motivo'})",
            "aguardando_aprovacao": "aguardando aprovacao",
            "aprovado": "aprovado, ainda nao enviado",
            "pendente": f"pendente (previsto para {aviso['data_prevista']})",
        }[aviso["status"]]
        linhas.append(f"{aviso['tipo_aviso']}: {detalhe}")
    return linhas


def descrever(estado: Estado, aviso: dict, clientes: dict, hoje: date) -> list[str]:
    """Texto mostrado ao aprovador: cliente, valor, vencimento, dias na pasta e historico."""
    boleto = estado.obter_boleto(aviso["boleto_id"])
    cliente = buscar_cliente(clientes, boleto["cnpj_pagador"])
    dias_na_pasta = (hoje - date.fromisoformat(boleto["criado_em"])).days
    valor = formatar_valor(Decimal(boleto["valor"])) if boleto["valor"] else "-"
    vencimento = formatar_data(date.fromisoformat(boleto["vencimento"])) if boleto["vencimento"] else "-"
    historico = _historico(estado, aviso["boleto_id"])
    return [
        f"Aviso {aviso['tipo_aviso']} (previsto para {aviso['data_prevista']}), boleto #{id_curto(aviso['boleto_id'])}",
        f"  Cliente:        {cliente.nome if cliente else '(nao cadastrado)'}",
        f"  CNPJ:           {mascarar_cnpj(boleto['cnpj_pagador']) or '-'}",
        f"  E-mail:         {mascarar_email(cliente.email_cobranca) if cliente and cliente.email_cobranca else '(sem e-mail)'}",
        f"  Valor:          {valor}",
        f"  Vencimento:     {vencimento}",
        f"  Dias na pasta:  {dias_na_pasta}",
        f"  Historico:      {'; '.join(historico) if historico else '-'}",
        f"  E-mail pronto:  {aviso['arquivo_fila'] or '-'}",
    ]


def _presenca(config, enviar: bool) -> Callable[[str], bool]:
    """Confere se o boleto ainda esta em entrada/ (so quando vai enviar de verdade)."""
    if not enviar:
        return lambda _boleto_id: True
    cache: dict[str, set[str]] = {}

    def presente(boleto_id: str) -> bool:
        if "ids" not in cache:
            try:
                cache["ids"] = set(
                    varrer_entrada(config.entrada, leitor_padrao(config.idioma_ocr)).identificados
                )
            except ErroReconciliacao:
                cache["ids"] = set()  # na duvida, nao enviar
        return boleto_id in cache["ids"]

    return presente


def cmd_aprovar(args, entrada: Entrada, saida: Saida, hoje: date) -> int:
    config = carregar_config(args.config)
    env = carregar_env(args.env)
    try:
        clientes = carregar_clientes(config.clientes)
    except ErroClientes as erro:
        saida(f"Aviso: {erro}. Os dados do cliente nao aparecerao e nada podera ser entregue.")
        clientes = {}

    with Estado(config.banco) as estado:
        fila = estado.listar_avisos("aguardando_aprovacao")
        if not fila:
            saida("Nenhum aviso aguardando aprovacao.")
            return 0

        saida(f"{len(fila)} aviso(s) aguardando aprovacao.\n")
        usuario = getpass.getuser()
        presenca = _presenca(config, args.enviar)
        aprovados = rejeitados = 0

        for indice, aviso in enumerate(fila, start=1):
            saida(f"[{indice}/{len(fila)}]")
            for linha in descrever(estado, aviso, clientes, hoje):
                saida(linha)
            if args.listar:
                saida("")
                continue

            while True:
                resposta = entrada("  [a]provar, [r]ejeitar, [p]ular, [s]air? ").strip().lower()
                if resposta in ("a", "r", "p", "s"):
                    break
                saida("  Opcao invalida.")

            if resposta == "s":
                break
            if resposta == "p":
                saida("")
                continue
            if resposta == "r":
                motivo = entrada("  Motivo da rejeicao: ").strip() or "sem motivo informado"
                if estado.rejeitar_aviso(aviso["boleto_id"], aviso["tipo_aviso"], hoje, usuario, motivo):
                    remover_da_fila(aviso["arquivo_fila"], config.fila_aprovacao)
                    rejeitados += 1
                    saida("  Rejeitado e cancelado.\n")
                continue

            if not estado.aprovar_aviso(aviso["boleto_id"], aviso["tipo_aviso"], hoje, usuario):
                saida("  O aviso mudou de estado; nada foi feito.\n")
                continue
            aprovados += 1
            resultado = entregar(
                estado, config, aviso["boleto_id"], aviso["tipo_aviso"], hoje, clientes, env,
                enviar=args.enviar, de="aprovado", arquivo_presente=presenca,
            )
            if resultado.situacao == "enviado":
                saida("  Aprovado e ENVIADO.\n")
            elif resultado.situacao == "dry-run":
                saida(f"  Aprovado. Dry-run: e-mail gerado em {resultado.arquivo} (nada enviado; use --enviar).\n")
            else:
                saida(f"  Aprovado, mas nao entregue ({resultado.situacao}): {resultado.motivo}\n")

        saida(f"Resumo: {aprovados} aprovado(s), {rejeitados} rejeitado(s).")
    return 0


def cmd_run(args, saida: Saida, hoje: date) -> int:
    config = carregar_config(args.config)
    env = carregar_env(args.env)
    log = LogJson(
        config.logs / f"execucao-{hoje.isoformat()}.jsonl",
        versao_codigo=versao_codigo(), versao_prompt=versao_prompt(),
    )

    modo = "ENVIO REAL" if args.enviar else "DRY-RUN (nada sera enviado)"
    saida(f"Execucao {log.execucao_id} em {hoje.strftime('%d/%m/%Y')} - modo: {modo}")
    if args.sem_llm:
        saida("Opcao --sem-llm: nenhum LLM sera usado (o fallback ainda nao existe).")

    execucao = executar(
        config, env, hoje, enviar=args.enviar, sem_llm=args.sem_llm,
        leitor=leitor_padrao(config.idioma_ocr, hoje),
        presenca=_presenca(config, args.enviar), log=log,
    )
    relatorio = gravar_relatorio(gerar_relatorio(execucao, config), config.relatorio)

    rec = execucao.reconciliacao
    if rec:
        saida(f"Lidos: {rec.documentos_lidos} | novos: {len(rec.novos)} | baixados: {len(rec.baixados)} "
              f"| reabertos: {len(rec.reabertos)} | baixas bloqueadas: {len(rec.baixas_bloqueadas)}")
    if execucao.avisos_executados:
        saida(f"Decisoes: {dict(execucao.decisoes) or 'nenhuma'} | fila: {dict(execucao.fila) or '-'} "
              f"| entregas: {dict(execucao.entregas) or '-'}")
    else:
        saida(f"Avisos nao executados: {execucao.avisos_nao_executados_motivo}")
    for erro in execucao.erros:
        saida(f"ERRO: {erro}")
    saida(f"Relatorio: {relatorio}")
    saida(f"Log: {log.caminho}")
    return execucao.codigo_saida


def cmd_testar_email(args, saida: Saida) -> int:
    """Envia UM e-mail simples de teste para o endereco informado (envio REAL).

    Serve para validar o SMTP antes de usar o agente: nao envolve boletos nem
    clientes, e so envia para o endereco pedido explicitamente em --para.
    """
    env = carregar_env(args.env)
    if not email_valido(args.para):
        saida("Erro: endereco de destino invalido.")
        return 2
    faltando = [c for c in ("SMTP_HOST", "SMTP_PORT", "EMAIL_REMETENTE") if not env.get(c)]
    if faltando:
        saida(f"Erro: faltam no .env: {', '.join(faltando)}.")
        return 2
    if not email_valido(env["EMAIL_REMETENTE"]):
        saida("Erro: EMAIL_REMETENTE nao e um endereco valido.")
        return 2

    mensagem = montar_mensagem(
        Aviso(
            assunto="Teste do agente de recebiveis",
            corpo=("Este e um e-mail de teste da configuracao de envio do agente de recebiveis.\n"
                   "Se voce o recebeu, o SMTP esta funcionando. Nenhum boleto ou cliente esta envolvido.\n"),
        ),
        remetente=env["EMAIL_REMETENTE"], destinatario=args.para,
    )
    try:
        enviar_smtp(mensagem, env)
    except ErroEnvio as erro:
        saida(f"Falha no envio: {erro}")
        return 1
    saida(f"E-mail de teste enviado para {mascarar_email(args.para)}.")
    return 0


def _encontrar(estado: Estado, identificador: str) -> list[dict]:
    """Localiza boletos pelo id curto (hash, mostrado nas telas) ou pelo final do codigo."""
    ident = identificador.strip().lower()
    if len(ident) < 6 or not ident.isalnum():
        raise ErroEstado("informe ao menos 6 caracteres do identificador")
    return [
        b for b in estado.listar_boletos()
        if b["boleto_id"].endswith(ident) or id_curto(b["boleto_id"]).startswith(ident)
    ]


def cmd_reabrir(args, saida: Saida, hoje: date) -> int:
    config = carregar_config(args.config)
    with Estado(config.banco) as estado:
        try:
            candidatos = _encontrar(estado, args.id)
        except ErroEstado as erro:
            saida(f"Erro: {erro}")
            return 2
        if len(candidatos) != 1:
            saida("Erro: o identificador deve corresponder a exatamente um boleto "
                  f"({len(candidatos)} encontrado(s)).")
            return 2
        boleto = candidatos[0]
        motivo = f"reaberto manualmente por {getpass.getuser()}"
        if estado.reabrir(boleto["boleto_id"], hoje, motivo):
            saida(f"Boleto #{id_curto(boleto['boleto_id'])} reaberto e marcado para revisao humana.")
            return 0
        saida(f"Boleto #{id_curto(boleto['boleto_id'])} nao esta pago; nada foi feito.")
        return 1


def main(
    argv: list[str] | None = None,
    *,
    entrada: Entrada = input,
    saida: Saida = print,
    hoje: date | None = None,
) -> int:
    pai = argparse.ArgumentParser(add_help=False)
    pai.add_argument("--config", default="config.yaml", help="caminho do config.yaml")
    pai.add_argument("--env", default=".env", help="caminho do .env")

    analisador = argparse.ArgumentParser(prog="python -m recebiveis", description=__doc__)
    sub = analisador.add_subparsers(dest="comando", required=True)

    aprovar = sub.add_parser("aprovar", parents=[pai], help="revisar a fila de aprovacao")
    aprovar.add_argument("--listar", action="store_true", help="apenas mostrar, sem decidir")
    aprovar.add_argument("--enviar", action="store_true",
                         help="envio REAL por SMTP dos avisos aprovados (padrao: dry-run)")

    run = sub.add_parser("run", parents=[pai], help="execucao completa (reconciliar, avisar, enfileirar)")
    run.add_argument("--enviar", action="store_true",
                     help="envio REAL por SMTP (padrao: dry-run, so gera .eml em saida/)")
    run.add_argument("--sem-llm", action="store_true", dest="sem_llm",
                     help="nao usar LLM (o fallback ainda nao esta implementado)")

    teste = sub.add_parser("testar-email", parents=[pai],
                           help="envia UM e-mail de teste (envio REAL) para validar o SMTP")
    teste.add_argument("--para", required=True, help="endereco que recebera o e-mail de teste")

    reabrir = sub.add_parser("reabrir", parents=[pai], help="reabrir um boleto baixado")
    reabrir.add_argument("id", help="ultimos caracteres (>= 6) do identificador do boleto")

    args = analisador.parse_args(argv)
    dia = hoje or date.today()
    try:
        if args.comando == "aprovar":
            return cmd_aprovar(args, entrada, saida, dia)
        if args.comando == "run":
            return cmd_run(args, saida, dia)
        if args.comando == "testar-email":
            return cmd_testar_email(args, saida)
        return cmd_reabrir(args, saida, dia)
    except ErroConfig as erro:
        saida(f"Erro de configuracao: {erro}")
        return 2
