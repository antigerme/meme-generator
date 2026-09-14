"""Mede a qualidade da busca vetorial contra um gabarito.

Indexa os contratos-ouro escritos à mão junto com todos os outros templates
(que entram com a descrição crua do 9GAG, como distratores) e mede em que
posição o meme esperado aparece.

A métrica que decide é top-k, com k igual ao usado em `memegen make` (padrão
30): é o conjunto que chega ao modelo gerador. Se o meme certo não está nele, o
gerador não tem como acertar.

    python tools/bench_retrieve.py
    python tools/bench_retrieve.py --model sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2

Depois de rodar `memegen enrich`, vale repetir com os contratos reais em vez do
gabarito, para ver se o enriquecimento está à altura do escrito à mão.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memegen import catalog as C  # noqa: E402
from memegen import retrieve  # noqa: E402
from tests.contratos_ouro import OURO  # noqa: E402

# situação -> template que deveria ser escolhido
CASOS = [
    ("escolher entre dormir cedo ou terminar a série", "two_buttons"),
    ("troquei de framework de novo e o antigo funcionava bem", "distracted_boyfriend"),
    ("mandei o email e até hoje ninguém respondeu", "waiting_skeleton"),
    ("otimizei a query a tarde toda e ela roda uma vez por mês", "grus_plan"),
    ("prefiro café coado a café de cápsula, ponto final", "change_my_mind"),
    ("deletei a branch errada e fingi que não fui eu", "disaster_girl"),
    ("sábado à noite sem nada para fazer", "sad_pablo_escobar"),
    ("me recuso a ler a documentação, prefiro tentar por três horas", "uno_draw_25_cards"),
    ("saí do Windows e fui para o Linux, sem arrependimento", "drake_hotline_bling"),
    ("backend e frontend concordando que a culpa é do DevOps", "epic_handshake"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=retrieve.DEFAULT_MODEL)
    ap.add_argument("-k", type=int, default=30, help="tamanho do conjunto que vai ao gerador")
    ap.add_argument("--reais", action="store_true",
                    help="usa os contratos de data/contracts.json em vez do gabarito")
    args = ap.parse_args()

    cat = C.load_catalog()
    if not cat:
        print("catálogo vazio — rode `memegen sync`", file=sys.stderr)
        return 1

    bons = C.load_contracts() if args.reais else OURO
    if not bons:
        print("nenhum contrato — rode `memegen enrich`", file=sys.stderr)
        return 1

    mix = {}
    for tid, t in cat.items():
        if tid in bons:
            mix[tid] = bons[tid]
        else:
            mix[tid] = {"funcao": t["description"], "quando_usar": t.get("keywords", []),
                        "tom": [], "slots": [], "nsfw": False}

    print(f"modelo: {args.model}")
    print(f"indexando {len(mix)} templates ({len(bons)} com contrato)...")
    retrieve.build(mix, model_name=args.model)

    casos = [(q, e) for q, e in CASOS if e in bons]
    t1 = t5 = tk = 0
    for q, esperado in casos:
        ids = retrieve.search(q, k=max(args.k, 30))
        pos = ids.index(esperado) + 1 if esperado in ids else None
        t1 += pos == 1
        t5 += bool(pos and pos <= 5)
        tk += bool(pos and pos <= args.k)
        marca = "TOP1" if pos == 1 else (f"#{pos:<3}" if pos else "FORA")
        print(f"  [{marca}] {q[:52]:52} -> {ids[0]}")

    n = len(casos)
    print(f"\n  top-1: {t1}/{n}   top-5: {t5}/{n}   top-{args.k}: {tk}/{n}")
    if tk < n:
        print(f"\n  {n - tk} caso(s) ficaram fora do conjunto enviado ao gerador.")
        print("  Nesses, nenhum modelo de geração consegue acertar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
