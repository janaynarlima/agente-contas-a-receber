"""python -m recebiveis.avaliacao gerar|medir"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from recebiveis.avaliacao.gerador import SEMENTE, gerar_conjunto
from recebiveis.avaliacao.metricas import gerar_relatorio_md, medir_conjunto, resumir
from recebiveis.versao import versao_codigo


def _tesseract() -> str:
    try:
        import pytesseract
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "indisponivel"


def main(argv: list[str] | None = None) -> int:
    analisador = argparse.ArgumentParser(prog="python -m recebiveis.avaliacao")
    sub = analisador.add_subparsers(dest="comando", required=True)
    gerar = sub.add_parser("gerar", help="gera o golden set sintetico")
    gerar.add_argument("--destino", default="golden")
    medir = sub.add_parser("medir", help="mede o agente e grava o relatorio")
    medir.add_argument("--golden", default="golden")
    medir.add_argument("--saida", default="docs/avaliacao_golden.md")
    args = analisador.parse_args(argv)

    if args.comando == "gerar":
        casos = gerar_conjunto(Path(args.destino))
        print(f"{len(casos)} boletos sinteticos gerados em {args.destino}/ (gabarito.json incluido)")
        return 0

    pasta = Path(args.golden)
    if not (pasta / "gabarito.json").is_file():
        print(f"golden set nao encontrado em {pasta}/ (rode: gerar)")
        return 2
    resultados = medir_conjunto(pasta)
    meta = {
        "semente": json.loads((pasta / "gabarito.json").read_text(encoding="utf-8")).get("semente", SEMENTE),
        "data": date.today().strftime("%d/%m/%Y"),
        "versao_codigo": versao_codigo(),
        "tesseract": _tesseract(),
    }
    saida = Path(args.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(gerar_relatorio_md(resultados, meta), encoding="utf-8")

    r = resumir(resultados)
    print(f"{r.total} casos | falsos aceites: {len(r.falsos_aceites)} | em revisao: {r.em_revisao} "
          f"| falhas de leitura: {r.erros_leitura}")
    for campo, (ok, n) in r.por_campo.items():
        print(f"  {campo}: {ok}/{n}")
    print(f"Relatorio: {saida}")
    return 1 if r.falsos_aceites else 0


if __name__ == "__main__":
    sys.exit(main())
