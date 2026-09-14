"""Servidor web local.

Roda na sua máquina porque precisa de três coisas que uma página hospedada não
tem: a sua chave da API, o catálogo em disco e o renderizador. Não tem
autenticação nem proteção — é para escutar em localhost, não para expor.

O laço que importa é o de edição: o modelo escreve o texto, você ajusta, a
imagem volta na hora. Texto de meme quase sempre precisa de um retoque, e ter
que rodar a geração de novo só para trocar uma palavra mataria o uso.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import traceback
import unicodedata
import zipfile
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import catalog as C
from . import config, source
from .render import render

app = FastAPI(title="memegen", docs_url=None, redoc_url=None)

_STATIC = config.ROOT / "memegen" / "static"


class SuggestBody(BaseModel):
    situacao: str
    n: int = 3
    modo: str = "auto"
    nsfw: bool = False


class RenderBody(BaseModel):
    template_id: str
    textos: dict[str, str]


class ItemZip(BaseModel):
    template_id: str
    textos: dict[str, str]


class ZipBody(BaseModel):
    itens: list[ItemZip]
    situacao: str = ""


def _catalog() -> dict:
    cat = C.load_catalog()
    if not cat:
        raise HTTPException(503, "catálogo vazio — rode `memegen sync`")
    return cat


def _imagem_local(template: dict) -> Any:
    """Caminho da imagem do template, baixando sob demanda."""
    dest = config.TEMPLATE_IMAGES / f"{template['id']}.jpg"
    if not dest.exists():
        source.download_image(template["url"], dest)
    return dest


def _renderizar(template: dict, textos: dict[str, str]) -> str:
    """Renderiza e devolve a URL. O nome do arquivo é o hash do conteúdo, então
    repetir a mesma combinação reaproveita o arquivo em vez de redesenhar."""
    chave = json.dumps({"t": template["id"], "x": textos}, sort_keys=True,
                       ensure_ascii=False)
    nome = f"{template['id']}-{hashlib.sha256(chave.encode()).hexdigest()[:12]}.jpg"
    destino = config.OUT / nome
    if not destino.exists():
        render(template, textos, _imagem_local(template), destino)
    return f"/render/{nome}"


_tamanhos: dict[tuple[str, float], tuple[int, int]] = {}


def _tamanho_real(tid: str, t: dict) -> tuple[int, int]:
    """Dimensão da imagem servida, que não é a declarada no catálogo.

    O 9GAG serve tudo normalizado no canvas de 640 px; `width`/`height` no
    catálogo são as dimensões do original. Medido: 779 dos 836 divergem. Como é
    o tamanho servido que sai no meme, é ele que a interface mostra.
    """
    caminho = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    try:
        chave = (tid, caminho.stat().st_mtime)
    except OSError:
        return t["width"], t["height"]
    if chave not in _tamanhos:
        try:
            from PIL import Image
            with Image.open(caminho) as im:   # lê só o cabeçalho
                _tamanhos[chave] = im.size
        except Exception:  # noqa: BLE001
            return t["width"], t["height"]
    return _tamanhos[chave]


def _resumo(tid: str, t: dict, contracts: dict) -> dict:
    c = contracts.get(tid) or {}
    return {
        "id": tid,
        "nome": t["name"],
        "ano": t.get("year") if t.get("year", 0) > 0 else None,
        "rank": t.get("_rank"),
        "descricao": t.get("description"),
        "keywords": t.get("keywords", []),
        "largura": _tamanho_real(tid, t)[0],
        "altura": _tamanho_real(tid, t)[1],
        "caixas": [
            {
                "id": b["id"],
                "papel": next((s["papel"] for s in c.get("slots", [])
                               if s["box_id"] == b["id"]), None),
                "max_chars": next((s["max_chars"] for s in c.get("slots", [])
                                   if s["box_id"] == b["id"]), None),
            }
            for b in t["textBoxes"]
        ],
        "contrato": {
            "funcao": c.get("funcao"),
            "quando_usar": c.get("quando_usar", []),
            "tom": c.get("tom", []),
        } if c else None,
        "img": f"/template/{tid}.jpg",
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((_STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/status")
def status() -> dict:
    cat = C.load_catalog()
    contracts = C.load_contracts()
    estado = C.load_state()
    pendentes = C.stale_contracts(cat, contracts) if cat else []

    indice = False
    try:
        from . import retrieve
        indice = not retrieve.is_stale(contracts) if contracts else False
    except Exception:  # noqa: BLE001
        pass

    pronto = bool(cat and contracts)
    aviso = None
    if not cat:
        aviso = "Catálogo vazio. Rode `memegen sync`."
    elif not contracts:
        aviso = "Nenhum contrato. Rode `memegen enrich` — sem isso a escolha não funciona."
    elif pendentes:
        aviso = f"{len(pendentes)} templates sem contrato atualizado. Rode `memegen enrich`."

    return {
        "pronto": pronto,
        "aviso": aviso,
        "templates": len(cat),
        "contratos": len(contracts),
        "pendentes": len(pendentes),
        "indice": indice,
        "sincronizado": estado.get("synced_at"),
        "bundle": estado.get("templates_asset"),
    }


@app.get("/api/templates")
def listar(q: str = "", limit: int = 60, offset: int = 0) -> dict:
    cat = _catalog()
    contracts = C.load_contracts()
    itens = sorted(cat.items(), key=lambda kv: kv[1].get("_rank", 9999))

    if q:
        termo = q.lower()
        def casa(kv):
            tid, t = kv
            c = contracts.get(tid) or {}
            alvo = " ".join([
                t["name"], " ".join(t.get("keywords", [])),
                c.get("funcao", ""), " ".join(c.get("quando_usar", [])),
            ]).lower()
            return termo in alvo
        itens = [kv for kv in itens if casa(kv)]

    total = len(itens)
    pagina = itens[offset:offset + limit]
    return {
        "total": total,
        "itens": [_resumo(tid, t, contracts) for tid, t in pagina],
    }


@app.get("/api/templates/{tid}")
def detalhe(tid: str) -> dict:
    cat = _catalog()
    if tid not in cat:
        raise HTTPException(404, "template desconhecido")
    return _resumo(tid, cat[tid], C.load_contracts())


@app.post("/api/suggest")
def sugerir(body: SuggestBody) -> dict:
    from .generate import suggest

    cat = _catalog()
    if not body.situacao.strip():
        raise HTTPException(400, "descreva a situação")

    try:
        sugestoes, uso = suggest(body.situacao, n=body.n, catalog=cat,
                                 modo=body.modo, incluir_nsfw=body.nsfw)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(502, f"falha ao chamar o modelo: {e}") from e

    contracts = C.load_contracts()
    saida = []
    for s in sugestoes:
        tid = s.get("template_id")
        t = cat.get(tid)
        if not t:
            continue
        textos = {x["box_id"]: x["texto"] for x in s.get("textos", [])}
        try:
            url = _renderizar(t, textos)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            raise HTTPException(500, f"falha ao renderizar {tid}: {e}") from e
        saida.append({**_resumo(tid, t, contracts),
                      "porque": s.get("porque"), "textos": textos, "render": url})

    return {"sugestoes": saida, "uso": uso}


@app.post("/api/render")
def rerenderizar(body: RenderBody) -> dict:
    cat = _catalog()
    t = cat.get(body.template_id)
    if not t:
        raise HTTPException(404, "template desconhecido")
    validos = {b["id"] for b in t["textBoxes"]}
    textos = {k: v for k, v in body.textos.items() if k in validos}
    try:
        return {"render": _renderizar(t, textos)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, f"falha ao renderizar: {e}") from e


def _slug(texto: str, limite: int = 40) -> str:
    """Transforma a situação num pedaço de nome de arquivo utilizável."""
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    plano = re.sub(r"[^a-zA-Z0-9]+", "-", plano).strip("-").lower()
    return plano[:limite].strip("-")


@app.post("/api/zip")
def baixar_todos(body: ZipBody) -> Response:
    """Empacota os memes num ZIP.

    Recebe os textos em vez de URLs de render porque o que interessa é o estado
    atual dos campos — depois das edições —, não o que o modelo escreveu. Como o
    render é cacheado por conteúdo, reempacotar o que já foi visto na tela não
    redesenha nada.
    """
    cat = _catalog()
    if not body.itens:
        raise HTTPException(400, "nada para baixar")

    buf = io.BytesIO()
    usados: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i, item in enumerate(body.itens, 1):
            t = cat.get(item.template_id)
            if not t:
                continue
            validos = {b["id"] for b in t["textBoxes"]}
            textos = {k: v for k, v in item.textos.items() if k in validos}
            try:
                url = _renderizar(t, textos)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                raise HTTPException(500, f"falha ao renderizar {item.template_id}: {e}") from e

            nome = f"{i:02d}-{item.template_id}.jpg"
            while nome in usados:                 # o mesmo template pode vir duas vezes
                nome = f"{i:02d}-{item.template_id}-{len(usados)}.jpg"
            usados.add(nome)
            z.write(config.OUT / url.rsplit("/", 1)[-1], nome)

    if not usados:
        raise HTTPException(400, "nenhum template válido")

    carimbo = datetime.now().strftime("%Y%m%d-%H%M")
    miolo = _slug(body.situacao) or "memes"
    arquivo = f"memegen-{miolo}-{carimbo}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{arquivo}"'},
    )


_NOME_SEGURO = re.compile(r"^[A-Za-z0-9_.-]+$")


@app.get("/template/{nome}")
def imagem_template(nome: str) -> FileResponse:
    tid = nome[:-4] if nome.endswith(".jpg") else nome
    if not _NOME_SEGURO.match(tid):
        raise HTTPException(400, "nome inválido")
    cat = _catalog()
    if tid not in cat:
        raise HTTPException(404, "template desconhecido")
    return FileResponse(_imagem_local(cat[tid]), media_type="image/jpeg")


@app.get("/render/{nome}")
def imagem_render(nome: str) -> FileResponse:
    if not _NOME_SEGURO.match(nome) or not nome.endswith(".jpg"):
        raise HTTPException(400, "nome inválido")
    caminho = config.OUT / nome
    if not caminho.exists():
        raise HTTPException(404, "render expirado — gere de novo")
    return FileResponse(caminho, media_type="image/jpeg")


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    import uvicorn
    config.ensure_dirs()
    print(f"memegen em http://{host}:{port}")
    uvicorn.run("memegen.web:app" if reload else app, host=host, port=port,
                reload=reload, log_level="warning")
