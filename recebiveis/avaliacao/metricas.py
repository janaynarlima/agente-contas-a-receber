"""Medicao do agente sobre o golden set.

Metricas (todas sobre o conjunto medido, que o relatorio sempre identifica):
  - acerto de CNPJ, nome, valor e vencimento (so casos em que o gabarito
    define o campo);
  - percentual enviado para revisao;
  - FALSO ACEITE: caso aceito (sem revisao) que deveria ir para revisao ou que
    tem algum campo errado. E a metrica mais importante: um falso aceite
    pode gerar cobranca indevida (falso positivo em acao);
  - REVISAO DESNECESSARIA: caso que podia ser aceito e foi para revisao
    (custo operacional, nao de risco);
  - tempo por boleto (leitura + extracao, incluindo OCR quando houver);
  - custo por boleto: nao se aplica (nenhum LLM e usado).
"""

import statistics
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from recebiveis.avaliacao.gerador import Caso, carregar_gabarito
from recebiveis.extrator import extrair_dados_boleto, extrair_texto

CAMPOS = ("cnpj", "nome", "valor", "vencimento")


@dataclass
class ResultadoCaso:
    caso: Caso
    extraido: dict = field(default_factory=dict)
    motivos_revisao: tuple[str, ...] = ()
    erro_leitura: str | None = None
    segundos: float = 0.0
    acertos: dict = field(default_factory=dict)   # campo -> bool (so campos avaliados)
    indevidos: list = field(default_factory=list)  # campos com dado quando o gabarito e None

    @property
    def em_revisao(self) -> bool:
        return self.erro_leitura is not None or bool(self.motivos_revisao)

    @property
    def aceito(self) -> bool:
        return not self.em_revisao

    @property
    def todos_corretos(self) -> bool:
        return all(self.acertos.values()) and not self.indevidos

    @property
    def falso_aceite(self) -> bool:
        if not self.aceito:
            return False
        if self.caso.esperado == "revisao":
            return True
        return not self.todos_corretos

    @property
    def revisao_desnecessaria(self) -> bool:
        return self.caso.esperado == "aceitar" and self.em_revisao


def normalizar_nome(texto: str | None) -> str | None:
    if texto is None:
        return None
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return " ".join(sem_acento.casefold().split())


def _comparar(caso: Caso, extraido: dict) -> tuple[dict, list]:
    verdade = {
        "cnpj": caso.cnpj,
        "nome": normalizar_nome(caso.nome),
        "valor": Decimal(caso.valor) if caso.valor is not None else None,
        "vencimento": date.fromisoformat(caso.vencimento) if caso.vencimento else None,
    }
    obtido = {
        "cnpj": extraido.get("cnpj"),
        "nome": normalizar_nome(extraido.get("nome")),
        "valor": extraido.get("valor"),
        "vencimento": extraido.get("vencimento"),
    }
    acertos, indevidos = {}, []
    for campo in CAMPOS:
        if campo in caso.campos_nao_avaliados:
            continue
        if verdade[campo] is None:
            # O gabarito diz que o dado nao deve ser aceito (por exemplo, so ha o
            # CNPJ do beneficiario): qualquer valor extraido e um erro.
            if obtido[campo] is not None:
                indevidos.append(campo)
            continue
        acertos[campo] = obtido[campo] == verdade[campo]
    return acertos, indevidos


def medir_caso(pasta: Path, caso: Caso, *, idioma_ocr: str = "por") -> ResultadoCaso:
    resultado = ResultadoCaso(caso=caso)
    inicio = time.perf_counter()
    try:
        texto = extrair_texto(Path(pasta) / caso.arquivo, idioma_ocr=idioma_ocr)
        dados = extrair_dados_boleto(texto, referencia=date.fromisoformat(caso.referencia))
    except Exception as erro:  # falha de leitura/OCR: o boleto iria para revisao
        resultado.segundos = time.perf_counter() - inicio
        resultado.erro_leitura = type(erro).__name__
        resultado.acertos = {c: False for c in CAMPOS if c not in caso.campos_nao_avaliados
                             and getattr(caso, c) is not None}
        return resultado
    resultado.segundos = time.perf_counter() - inicio
    resultado.extraido = {
        "cnpj": dados.cnpj_pagador, "nome": dados.nome_pagador,
        "valor": dados.valor, "vencimento": dados.vencimento,
    }
    resultado.motivos_revisao = dados.motivos_revisao
    resultado.acertos, resultado.indevidos = _comparar(caso, resultado.extraido)
    return resultado


@dataclass
class Resumo:
    total: int
    por_campo: dict          # campo -> (acertos, avaliados)
    em_revisao: int
    falsos_aceites: list[str]
    revisoes_desnecessarias: list[str]
    revisao_necessaria_total: int
    revisao_necessaria_capturada: int
    tempos: dict             # grupo -> lista de segundos
    erros_leitura: int


def resumir(resultados: list[ResultadoCaso]) -> Resumo:
    por_campo = {}
    for campo in CAMPOS:
        avaliados = [r for r in resultados if campo in r.acertos]
        por_campo[campo] = (sum(r.acertos[campo] for r in avaliados), len(avaliados))

    obrigatorios = [r for r in resultados if r.caso.esperado == "revisao"]
    tempos = {"todos": [r.segundos for r in resultados]}
    for grupo, filtro in (("pdf_texto", lambda r: r.caso.formato == "pdf_texto"),
                          ("ocr (imagem e pdf escaneado)", lambda r: r.caso.formato != "pdf_texto")):
        selecionados = [r.segundos for r in resultados if filtro(r)]
        if selecionados:
            tempos[grupo] = selecionados

    return Resumo(
        total=len(resultados),
        por_campo=por_campo,
        em_revisao=sum(r.em_revisao for r in resultados),
        falsos_aceites=[r.caso.id for r in resultados if r.falso_aceite],
        revisoes_desnecessarias=[r.caso.id for r in resultados if r.revisao_desnecessaria],
        revisao_necessaria_total=len(obrigatorios),
        revisao_necessaria_capturada=sum(r.em_revisao for r in obrigatorios),
        tempos=tempos,
        erros_leitura=sum(r.erro_leitura is not None for r in resultados),
    )


def medir_conjunto(pasta: Path, *, idioma_ocr: str = "por") -> list[ResultadoCaso]:
    return [medir_caso(pasta, caso, idioma_ocr=idioma_ocr) for caso in carregar_gabarito(pasta)]


def _pct(parte: int, todo: int) -> str:
    return f"{100 * parte / todo:.0f}% ({parte}/{todo})" if todo else "n/d"


def _tempo(valores: list[float]) -> str:
    ordenados = sorted(valores)
    p95 = ordenados[min(len(ordenados) - 1, int(0.95 * len(ordenados)))]
    return (f"média {statistics.mean(valores):.2f} s · mediana {statistics.median(valores):.2f} s · "
            f"p95 {p95:.2f} s · máx {max(valores):.2f} s")


def gerar_relatorio_md(resultados: list[ResultadoCaso], meta: dict) -> str:
    r = resumir(resultados)
    composicao = {}
    for res in resultados:
        composicao[res.caso.formato] = composicao.get(res.caso.formato, 0) + 1

    linhas = [
        "# Avaliação do agente no golden set sintético",
        "",
        "## Conjunto medido",
        f"- **Conjunto:** `golden/` — {r.total} boletos **sintéticos** gerados com semente fixa "
        f"({meta.get('semente')}). Nenhum dado real.",
        "- **Composição por formato:** " + ", ".join(f"{k}: {v}" for k, v in sorted(composicao.items())),
        "- **Layouts:** 4 aproximações sintéticas (rótulo acima do valor, tudo na mesma linha, blocos "
        "rotulados, terminologia antiga Sacado/Cedente). Não reproduzem o leiaute de nenhum banco real.",
        f"- **Medido em:** {meta.get('data')} · código `{meta.get('versao_codigo')}` · "
        f"Tesseract {meta.get('tesseract')}",
        "",
        "## Resultados",
        "",
        "| Métrica | Resultado |",
        "|---|---|",
    ]
    nomes = {"cnpj": "Acerto de CNPJ do pagador", "nome": "Acerto de nome do pagador",
             "valor": "Acerto de valor", "vencimento": "Acerto de vencimento"}
    for campo in CAMPOS:
        linhas.append(f"| {nomes[campo]} | {_pct(*r.por_campo[campo])} |")
    linhas += [
        f"| **Falso aceite** (aceito sem revisão, mas errado ou obrigatório em revisão) | "
        f"**{_pct(len(r.falsos_aceites), r.total)}**" + (f" — {', '.join(r.falsos_aceites)}" if r.falsos_aceites else "") + " |",
        f"| Casos obrigatórios em revisão que foram capturados | {_pct(r.revisao_necessaria_capturada, r.revisao_necessaria_total)} |",
        f"| Enviados para revisão (total) | {_pct(r.em_revisao, r.total)} |",
        f"| Revisão desnecessária (podia ser aceito) | {_pct(len(r.revisoes_desnecessarias), sum(x.caso.esperado == 'aceitar' for x in resultados))}"
        + (f" — {', '.join(r.revisoes_desnecessarias)}" if r.revisoes_desnecessarias else "") + " |",
        f"| Falhas de leitura/OCR | {r.erros_leitura} |",
        "| Custo por boleto (LLM) | não se aplica: nenhum LLM é usado |",
    ]
    for grupo, valores in r.tempos.items():
        linhas.append(f"| Tempo por boleto — {grupo} | {_tempo(valores)} |")

    linhas += ["", "## Por caso", "",
               "| Caso | Formato | Esperado | CNPJ | Nome | Valor | Venc. | Revisão | Falso aceite |",
               "|---|---|---|---|---|---|---|---|---|"]

    def marca(res, campo):
        if campo in res.acertos:
            return "✓" if res.acertos[campo] else "✗"
        return "—"

    for res in resultados:
        indevido = " (indevido)" if res.indevidos else ""
        linhas.append(
            f"| {res.caso.id} | {res.caso.formato} | {res.caso.esperado} | {marca(res, 'cnpj')} | "
            f"{marca(res, 'nome')} | {marca(res, 'valor')} | {marca(res, 'vencimento')} | "
            f"{'sim' if res.em_revisao else 'não'}{indevido} | {'**SIM**' if res.falso_aceite else 'não'} |"
        )

    linhas += [
        "", "## Como ler", "",
        "- ✓ campo correto · ✗ campo errado ou ausente · — campo sem gabarito (não avaliado).",
        "- *Esperado*: `aceitar` = tudo correto e sem revisão; `revisao` = o agente deve revisar; "
        "`indiferente` = correto ou revisão servem (OCR ruim).",
        "- Quando o gabarito diz que o CNPJ não deve ser aceito (por exemplo, só existe o CNPJ do "
        "beneficiário), qualquer CNPJ extraído conta como erro.",
        "",
        "## Limites desta medição",
        "",
        "- São boletos sintéticos de poucos layouts; o resultado **não** estima o acerto em boletos reais.",
        f"- {r.total} casos é um conjunto pequeno: um caso muda a taxa em {100 / r.total:.0f} ponto(s) percentual(is).",
        "- O conjunto foi montado pelo mesmo projeto que o mede; quando uma falha é corrigida, o conjunto "
        "deixa de mostrá-la. Compare sempre com o relatório anterior.",
        "",
    ]
    return "\n".join(linhas)
