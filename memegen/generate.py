"""Da situação do usuário para memes prontos.

Uma única chamada faz as duas coisas que precisam de julgamento — escolher os
templates e escrever o texto de cada caixa — porque separá-las obriga o seletor
a decidir sem saber se o texto vai funcionar.

O catálogo enriquecido inteiro dá ~92 mil tokens para 836 templates — cabe no
contexto de qualquer modelo atual, mas custa caro quando o cache está frio, que
é o caso normal de uso pessoal esporádico (o TTL padrão é de 5 minutos).

Por isso há dois modos:

  auto (padrão)       usa busca vetorial se houver índice construído, e cai
                      para a triagem por LLM se não houver. Nada a configurar.
  vetorial            busca semântica local separa os candidatos. Custo
                      marginal zero na seleção; exige `memegen index` e a
                      dependência opcional sentence-transformers.
  shortlist           um modelo barato lê uma lista compacta (~20k tokens) e
                      separa candidatos. Não precisa de dependência extra, mas
                      paga ~$0,02 por meme só na triagem.
  full                o catálogo inteiro vai direto para o modelo bom. Mais
                      caro, e faz sentido quando várias gerações acontecem em
                      sequência e o cache fica quente.
"""

from __future__ import annotations

import json

import anthropic

from . import catalog as C
from . import config

SYSTEM_HEAD = """\
Você escolhe memes e escreve o texto deles, em português do Brasil.

Recebe uma situação — pode ser um desabafo, um trecho de conversa, um bug, uma \
reunião, qualquer coisa — e devolve os memes que melhor se aplicam, já com o \
texto de cada caixa preenchido.

Como escolher:
- Case pela FUNÇÃO do meme, não por palavra em comum. Se a situação é "troquei \
de framework de novo", o meme certo é o que serve para "ser tentado por algo \
novo ignorando o que já funciona" — não um que por acaso cite programação.
- Varie o formato entre as sugestões. Três memes de comparação lado a lado é \
uma sugestão só, repetida.
- Em empate, prefira o de menor `rank` (mais reconhecível).
- Respeite o papel de cada caixa. O papel está escrito no catálogo; a ordem das \
caixas não é a ordem semântica.

Como escrever:
- Texto de meme é curto e seco. Não explique a piada, não repita a situação, não \
comece com "quando você".
- Respeite o `max_chars` de cada slot. Texto que estoura a caixa é encolhido até \
ficar ilegível.
- Use o português que a pessoa usou. Se ela escreveu com gíria, mantenha.
- Preencha TODAS as caixas do template escolhido, usando o `box_id` exato.
- `porque`: uma frase dizendo por que este meme serve para esta situação.
"""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "sugestoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "porque": {"type": "string"},
                    "textos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "box_id": {"type": "string"},
                                "texto": {"type": "string"},
                            },
                            "required": ["box_id", "texto"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["template_id", "porque", "textos"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sugestoes"],
    "additionalProperties": False,
}


def catalog_text(catalog: dict, contracts: dict, incluir_nsfw: bool = False) -> str:
    """Serializa o catálogo enriquecido no formato que vai para o modelo."""
    blocos = []
    for tid, t in sorted(catalog.items(), key=lambda kv: kv[1].get("_rank", 9999)):
        c = contracts.get(tid)
        if not c:
            continue
        if c.get("nsfw") and not incluir_nsfw:
            continue
        slots = "; ".join(
            f"{s['box_id']}={s['papel']} (máx {s['max_chars']})" for s in c["slots"]
        )
        blocos.append(
            f"### {tid} | {t['name']} | rank {t.get('_rank', '?')}\n"
            f"funcao: {c['funcao']}\n"
            f"usar: {'; '.join(c['quando_usar'])}\n"
            f"tom: {', '.join(c['tom'])}\n"
            f"slots: {slots}"
        )
    return "\n\n".join(blocos)


SHORTLIST_SYSTEM = """\
Você faz a triagem de um catálogo de memes.

Recebe uma situação e uma lista de templates, cada um com sua função. Devolve os \
ids dos mais promissores — case pela FUNÇÃO do meme, não por palavra em comum \
com a situação. Seja generoso: é uma peneira grossa, outro modelo faz a escolha \
final. Inclua formatos variados, não só variações do mesmo tipo de piada.
"""

SHORTLIST_SCHEMA = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["ids"],
    "additionalProperties": False,
}


def shortlist_text(catalog: dict, contracts: dict, incluir_nsfw: bool = False) -> str:
    """Versão enxuta do catálogo: só id e função, para a triagem."""
    linhas = []
    for tid, t in sorted(catalog.items(), key=lambda kv: kv[1].get("_rank", 9999)):
        c = contracts.get(tid)
        if not c or (c.get("nsfw") and not incluir_nsfw):
            continue
        linhas.append(f"{tid}: {c['funcao']}")
    return "\n".join(linhas)


def shortlist(
    situacao: str,
    catalog: dict,
    contracts: dict,
    k: int = 30,
    model: str = "claude-haiku-4-5",
    incluir_nsfw: bool = False,
) -> tuple[list[str], dict]:
    """Peneira o catálogo com um modelo barato. Devolve (ids, uso)."""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        system=[
            {"type": "text", "text": SHORTLIST_SYSTEM},
            {
                "type": "text",
                "text": "CATÁLOGO\n\n" + shortlist_text(catalog, contracts, incluir_nsfw),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{
            "role": "user",
            "content": f"Separe até {k} templates para esta situação:\n\n{situacao}",
        }],
        output_config={"format": {"type": "json_schema", "schema": SHORTLIST_SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    ids = [i for i in json.loads(text)["ids"] if i in contracts][:k]
    u = resp.usage
    uso = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    return ids, uso


def suggest(
    situacao: str,
    n: int = 3,
    catalog: dict | None = None,
    contracts: dict | None = None,
    model: str | None = None,
    incluir_nsfw: bool = False,
    modo: str = "auto",
    k: int = 30,
) -> tuple[list[dict], dict]:
    """Devolve (sugestões, uso de tokens)."""
    catalog = catalog if catalog is not None else C.load_catalog()
    contracts = contracts if contracts is not None else C.load_contracts()
    if not contracts:
        raise RuntimeError(
            "nenhum contrato encontrado — rode `memegen enrich` antes de gerar"
        )

    uso_triagem = None
    ids: list[str] = []

    if modo == "auto":
        # prefere o caminho de custo zero, mas nunca falha por falta dele
        try:
            from . import retrieve
            modo = "shortlist" if retrieve.is_stale(contracts) else "vetorial"
        except Exception:  # noqa: BLE001
            modo = "shortlist"

    if modo == "vetorial":
        from . import retrieve
        ids = retrieve.search(situacao, k)
    elif modo == "shortlist":
        ids, uso_triagem = shortlist(situacao, catalog, contracts, k,
                                     incluir_nsfw=incluir_nsfw)
    if ids:
        contracts = {i: contracts[i] for i in ids if i in contracts}
        catalog = {i: catalog[i] for i in ids if i in catalog}

    cat = catalog_text(catalog, contracts, incluir_nsfw)
    client = anthropic.Anthropic()

    resp = client.messages.create(
        model=model or config.GENERATE_MODEL,
        max_tokens=4000,
        system=[
            # bloco estável primeiro, e é ele que fica em cache
            {"type": "text", "text": SYSTEM_HEAD},
            {
                "type": "text",
                "text": f"CATÁLOGO DE TEMPLATES\n\n{cat}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{
            "role": "user",
            "content": f"Sugira {n} memes para esta situação:\n\n{situacao}",
        }],
        output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    )

    text = next(b.text for b in resp.content if b.type == "text")
    sugestoes = json.loads(text)["sugestoes"]

    u = resp.usage
    uso = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "triagem": uso_triagem,
        "candidatos": len(contracts),
        "modo": modo,
    }
    return sugestoes, uso
