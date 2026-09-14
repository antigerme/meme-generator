"""Interface de linha de comando."""

from __future__ import annotations

import argparse
import sys

from . import catalog as C
from . import config, enrich, source
from .generate import suggest
from .render import render


def cmd_sync(args) -> int:
    """Verifica o 9GAG e atualiza o espelho local."""
    config.ensure_dirs()
    state = C.load_state()
    pointer = source.fetch_pointer()

    print(f"build do 9GAG: {pointer.manifest_asset}")
    print(f"bundle atual : {pointer.templates_asset}")

    if state.get("templates_asset") == pointer.templates_asset and not args.force:
        print(f"nada mudou desde {state.get('synced_at', '?')} "
              f"({state.get('count', '?')} templates). Use --force para rebaixar.")
        return 0

    if state.get("templates_asset"):
        print(f"bundle anterior: {state['templates_asset']} -> mudou, baixando")

    _, raw = source.fetch_catalog(pointer)
    novo = C.index(raw)
    antigo = C.load_catalog()
    d = C.diff(antigo, novo)

    print(f"\ndiff: {d}")
    for tid in d.added[:20]:
        print(f"  + {tid} ({novo[tid]['name']})")
    if len(d.added) > 20:
        print(f"  + ... e mais {len(d.added) - 20}")
    for tid in d.changed[:20]:
        print(f"  ~ {tid} ({novo[tid]['name']})")
    for tid in d.removed[:20]:
        print(f"  - {tid}")

    C.save_catalog(novo)
    C.save_state({
        "manifest_asset": pointer.manifest_asset,
        "templates_asset": pointer.templates_asset,
        "synced_at": C.now(),
        "count": len(novo),
    })

    pendentes = C.stale_contracts(novo, C.load_contracts())
    if pendentes:
        print(f"\n{len(pendentes)} templates sem contrato atualizado.")
        print("  rode: memegen enrich")
    return 0


def cmd_enrich(args) -> int:
    config.ensure_dirs()
    catalog = C.load_catalog()
    if not catalog:
        print("catálogo vazio — rode `memegen sync` primeiro", file=sys.stderr)
        return 1

    if args.collect:
        enrich.collect(args.collect, catalog)
        return 0

    pendentes = C.stale_contracts(catalog, C.load_contracts())
    if args.only:
        pendentes = [t for t in args.only if t in catalog]
    if args.limit:
        pendentes = pendentes[: args.limit]

    if not pendentes:
        print("todos os contratos estão em dia")
        return 0

    print(f"{len(pendentes)} templates a enriquecer com {args.model or config.ENRICH_MODEL}")
    print("baixando imagens faltantes...")
    falhas = enrich.ensure_images(catalog, pendentes)
    pendentes = [t for t in pendentes if t not in falhas]

    if args.dry_run:
        print(f"[dry-run] enfileiraria {len(pendentes)} requisições")
        print("\nexemplo de prompt:\n")
        print(enrich.build_prompt(catalog[pendentes[0]]))
        return 0

    batch_id = enrich.submit(catalog, pendentes, args.model)
    if args.wait:
        enrich.collect(batch_id, catalog)
    else:
        print(f"\nacompanhe com: memegen enrich --collect {batch_id}")
    return 0


def cmd_make(args) -> int:
    config.ensure_dirs()
    catalog = C.load_catalog()
    sugestoes, uso = suggest(args.situacao, n=args.n, catalog=catalog,
                             model=args.model, incluir_nsfw=args.nsfw,
                             modo=args.modo, k=args.k)

    http = source._client(timeout=60.0)
    try:
        for i, s in enumerate(sugestoes, 1):
            tid = s["template_id"]
            t = catalog.get(tid)
            if not t:
                print(f"{i}. {tid}: template desconhecido, pulando")
                continue

            img = config.TEMPLATE_IMAGES / f"{tid}.jpg"
            if not img.exists():
                source.download_image(t["url"], img, http)

            textos = {x["box_id"]: x["texto"] for x in s["textos"]}
            out = config.OUT / f"{i:02d}_{tid}.jpg"
            render(t, textos, img, out)

            print(f"\n{i}. {t['name']}  ({tid})")
            print(f"   {s['porque']}")
            for box_id, txt in textos.items():
                print(f"   [{box_id}] {txt}")
            print(f"   -> {out}")
    finally:
        http.close()

    print(f"\nmodo    : {uso.get('modo')}")
    if uso.get("triagem"):
        t = uso["triagem"]
        print(f"\ntriagem : {t['input']} entrada, {t['output']} saída "
              f"(cache {t['cache_read']}R/{t['cache_write']}W) "
              f"-> {uso['candidatos']} candidatos")
    print(f"geração : {uso['input']} entrada, {uso['output']} saída "
          f"(cache {uso['cache_read']}R/{uso['cache_write']}W)")
    return 0


def cmd_status(args) -> int:
    catalog = C.load_catalog()
    contracts = C.load_contracts()
    state = C.load_state()
    pendentes = C.stale_contracts(catalog, contracts)

    print(f"catálogo   : {len(catalog)} templates")
    print(f"contratos  : {len(contracts)} ({len(pendentes)} pendentes)")
    print(f"bundle     : {state.get('templates_asset', '-')}")
    print(f"sincronizado: {state.get('synced_at', 'nunca')}")
    imgs = len(list(config.TEMPLATE_IMAGES.glob('*.jpg'))) if config.TEMPLATE_IMAGES.exists() else 0
    print(f"imagens    : {imgs} em disco")
    return 0


def cmd_index(args) -> int:
    config.ensure_dirs()
    from . import retrieve
    n = retrieve.build()
    print(f"índice construído com {n} templates -> {retrieve.INDEX_PATH}")
    return 0


def cmd_audit(args) -> int:
    """Lista os contratos mais arriscados para revisão manual."""
    catalog = C.load_catalog()
    contracts = C.load_contracts()
    # risco = muitas caixas (papéis ambíguos) e descrição do 9GAG sem instrução de uso
    import re
    riscos = []
    for tid, t in catalog.items():
        c = contracts.get(tid)
        if not c:
            continue
        n = len(t["textBoxes"])
        sem_guia = not re.search(r"used to|it'?s used|to use it|label the",
                                 t.get("description", ""), re.I)
        score = (n - 2) * 2 + (1 if sem_guia else 0)
        if score > 0:
            riscos.append((score, tid, t["name"], n, c))
    riscos.sort(reverse=True, key=lambda r: r[0])

    print(f"{len(riscos)} contratos merecem olhada; mostrando {args.limit}\n")
    for score, tid, name, n, c in riscos[: args.limit]:
        print(f"[risco {score}] {name} ({tid}) — {n} caixas")
        print(f"  funcao: {c['funcao']}")
        for s in c["slots"]:
            print(f"    {s['box_id']}: {s['papel']}")
        print()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="memegen", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="sincroniza o catálogo do 9GAG")
    s.add_argument("--force", action="store_true", help="rebaixa mesmo sem mudança")
    s.set_defaults(func=cmd_sync)

    e = sub.add_parser("enrich", help="gera contratos de uso para templates novos")
    e.add_argument("--collect", metavar="BATCH_ID", help="coleta um lote já enviado")
    e.add_argument("--wait", action="store_true", help="aguarda o lote terminar")
    e.add_argument("--limit", type=int, help="processa no máximo N templates")
    e.add_argument("--only", nargs="+", metavar="ID", help="enriquece ids específicos")
    e.add_argument("--model", help=f"padrão: {config.ENRICH_MODEL}")
    e.add_argument("--dry-run", action="store_true", help="mostra o que faria")
    e.set_defaults(func=cmd_enrich)

    m = sub.add_parser("make", help="gera memes para uma situação")
    m.add_argument("situacao", help="texto, conversa, desabafo, situação")
    m.add_argument("-n", type=int, default=3, help="quantas sugestões (padrão 3)")
    m.add_argument("--model", help=f"padrão: {config.GENERATE_MODEL}")
    m.add_argument("--nsfw", action="store_true", help="inclui templates marcados nsfw")
    m.add_argument("--modo", choices=["auto", "vetorial", "shortlist", "full"],
                   default="auto",
                   help="auto: vetorial se houver índice, senão shortlist (padrão)")
    m.add_argument("-k", type=int, default=30, help="candidatos na triagem (padrão 30)")
    m.set_defaults(func=cmd_make)

    st = sub.add_parser("status", help="estado local")
    st.set_defaults(func=cmd_status)

    i = sub.add_parser("index", help="(re)constrói o índice de busca vetorial")
    i.set_defaults(func=cmd_index)

    a = sub.add_parser("audit", help="contratos que merecem revisão manual")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(func=cmd_audit)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
