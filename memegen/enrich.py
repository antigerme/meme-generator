"""Enriquecimento do catálogo: de metadado do 9GAG para contrato de uso em pt-BR.

O 9GAG entrega `description` (a história do meme) e `keywords`, mas não diz
**qual texto vai em qual caixa**. Em `distracted_boyfriend` a ordem das caixas é
mulher-de-vermelho, namorado, namorada — trocar isso destrói a piada. Só 40 dos
836 descrevem os papéis explicitamente.

Este módulo resolve isso uma vez por template: manda a imagem, a descrição do
9GAG e a posição de cada caixa no canvas, e recebe de volta um contrato em
português dizendo o que cada caixa significa.

Roda pela Batch API (metade do preço) e é incremental: só entra na fila o
template cujo `source_hash` mudou desde o último enriquecimento.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from . import catalog as C
from . import config, source

SYSTEM = """\
Você cataloga templates de meme para um gerador automático em português do Brasil.

Para cada template você recebe: a imagem, o nome, o ano, a descrição em inglês \
escrita pelo 9GAG e a posição de cada caixa de texto sobre a imagem.

Produza um contrato de uso. O campo mais importante é o papel de cada caixa: \
olhe a imagem e a posição da caixa e diga o que aquele texto específico \
representa. A ordem das caixas NÃO é a ordem semântica — em "Distracted \
Boyfriend" a primeira caixa fica sobre a mulher de vermelho (a tentação), a \
segunda sobre o namorado (quem se distrai) e a terceira sobre a namorada (o que \
está sendo ignorado).

Regras:
- Escreva tudo em português do Brasil.
- `funcao`: em uma frase, para que serve este meme — a função pragmática, não a \
descrição visual. "Contrastar duas opções rejeitando a primeira", não "homem de \
casaco laranja".
- `quando_usar`: 3 a 6 situações concretas em que ele se aplica. Pense em como \
alguém descreveria a própria situação, não em como descreveria a imagem.
- `tom`: 1 a 3 marcadores (ex.: sarcástico, autodepreciativo, triunfal, \
resignado, absurdo).
- `slots`: um por caixa, na mesma ordem recebida, repetindo o `box_id` exato. \
`papel` diz o que entra ali; `exemplo` é um texto curto e plausível em pt-BR.
- `max_chars`: limite realista para o texto caber na caixa, dado o tamanho dela.
- `nsfw`: true se o template envolver conteúdo sexual, violento ou ofensivo.
"""

CONTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "funcao": {"type": "string"},
        "quando_usar": {"type": "array", "items": {"type": "string"}},
        "tom": {"type": "array", "items": {"type": "string"}},
        "slots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "box_id": {"type": "string"},
                    "papel": {"type": "string"},
                    "exemplo": {"type": "string"},
                    "max_chars": {"type": "integer"},
                },
                "required": ["box_id", "papel", "exemplo", "max_chars"],
                "additionalProperties": False,
            },
        },
        "nsfw": {"type": "boolean"},
    },
    "required": ["funcao", "quando_usar", "tom", "slots", "nsfw"],
    "additionalProperties": False,
}


def _spatial(box: dict, cw: float, ch: float) -> str:
    """Descrição espacial da caixa, para o modelo casar caixa com elemento visual."""
    cx = (box["x"] + box["width"] / 2) / cw
    cy = (box["y"] + box["height"] / 2) / ch
    h = "esquerda" if cx < 0.37 else "direita" if cx > 0.63 else "centro"
    v = "topo" if cy < 0.37 else "base" if cy > 0.63 else "meio"
    pct_w = 100 * box["width"] / cw
    pct_h = 100 * box["height"] / ch
    return (
        f"{v}-{h} (centro em {cx:.0%} da largura, {cy:.0%} da altura; "
        f"caixa ocupa {pct_w:.0f}% x {pct_h:.0f}% da imagem)"
    )


def build_prompt(template: dict) -> str:
    cw, ch = config.canvas_size(template["width"], template["height"])
    linhas = [
        f"Nome: {template['name']}",
        f"Ano: {template['year']}" if template.get("year", 0) > 0 else "Ano: desconhecido",
        f"Descrição do 9GAG: {template['description']}",
        f"Keywords do 9GAG: {', '.join(template.get('keywords', []))}",
        "",
        f"Caixas de texto ({len(template['textBoxes'])}), na ordem do template:",
    ]
    for b in template["textBoxes"]:
        linhas.append(f"  - box_id={b['id']}: {_spatial(b, cw, ch)}")
    return "\n".join(linhas)


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def ensure_images(catalog: dict, ids: list[str], client=None) -> list[str]:
    """Garante a imagem local de cada template. Devolve os que falharam."""
    falhas = []
    http = client or source._client(timeout=60.0)
    try:
        for tid in ids:
            dest = config.TEMPLATE_IMAGES / f"{tid}.jpg"
            if dest.exists() and dest.stat().st_size > 0:
                continue
            try:
                source.download_image(catalog[tid]["url"], dest, http)
            except Exception as e:  # noqa: BLE001
                falhas.append(tid)
                print(f"  falha ao baixar {tid}: {e}")
    finally:
        if client is None:
            http.close()
    return falhas


def submit(catalog: dict, ids: list[str], model: str | None = None) -> str:
    """Enfileira o enriquecimento na Batch API. Devolve o id do lote."""
    model = model or config.ENRICH_MODEL
    client = anthropic.Anthropic()
    requests = []
    for tid in ids:
        t = catalog[tid]
        img = config.TEMPLATE_IMAGES / f"{tid}.jpg"
        if not img.exists():
            continue
        requests.append(
            Request(
                custom_id=tid,
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=2000,
                    system=SYSTEM,
                    messages=[{
                        "role": "user",
                        "content": [_image_block(img), {"type": "text", "text": build_prompt(t)}],
                    }],
                    output_config={"format": {"type": "json_schema", "schema": CONTRACT_SCHEMA}},
                ),
            )
        )
    if not requests:
        raise RuntimeError("nenhum template com imagem disponível para enriquecer")
    batch = client.messages.batches.create(requests=requests)
    print(f"lote {batch.id} criado com {len(requests)} templates ({model})")
    return batch.id


def collect(batch_id: str, catalog: dict, poll: int = 30) -> dict[str, dict]:
    """Aguarda o lote e grava os contratos. Devolve só os novos."""
    client = anthropic.Anthropic()
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            break
        c = batch.request_counts
        print(f"  {batch.processing_status}: {c.succeeded} prontos, {c.processing} na fila")
        time.sleep(poll)

    contracts = C.load_contracts()
    novos: dict[str, dict] = {}
    erros = 0
    for result in client.messages.batches.results(batch_id):
        tid = result.custom_id
        if result.result.type != "succeeded":
            erros += 1
            print(f"  {tid}: {result.result.type}")
            continue
        msg = result.result.message
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if not text:
            erros += 1
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            erros += 1
            print(f"  {tid}: JSON inválido")
            continue
        data["source_hash"] = catalog[tid]["_source_hash"]
        data["enriched_at"] = C.now()
        data["model"] = msg.model
        contracts[tid] = data
        novos[tid] = data

    C.save_contracts(contracts)
    print(f"contratos gravados: {len(novos)} ok, {erros} com erro")
    return novos
