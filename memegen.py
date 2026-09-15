#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memegen - gerador de memes com IA sobre a curadoria de templates do 9GAG.

Arquivo unico, apenas biblioteca padrao. Testado de Python 3.6 a 3.14.

    python3 memegen.py sync                 baixa o catalogo do 9GAG
    python3 memegen.py enrich --wait        gera os contratos de uso
    python3 memegen.py serve                abre a interface web

A renderizacao acontece no navegador, em canvas. Nao e contorno da falta de
Pillow: e o que o proprio 9GAG faz. O canvas resolve fonte, contorno e rotacao
com o motor de texto do navegador, o que tambem elimina a cacada por Impact.py

Compatibilidade: nada de dataclasses, f-string com '=', walrus, anotacoes com
tipos embutidos parametrizados, nem ThreadingHTTPServer -- todos posteriores ao
3.6. E nada de ssl.wrap_socket nem datetime.utcnow(), removidos ou obsoletos
nas versoes novas.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime

try:                                  # 3.x
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlparse, parse_qs, unquote
except ImportError:                   # pragma: no cover - so para mensagem clara
    sys.exit("memegen exige Python 3.6 ou superior")

import http.server
import socketserver

if sys.version_info < (3, 6):
    sys.exit("memegen exige Python 3.6 ou superior (detectado %s)"
             % ".".join(str(n) for n in sys.version_info[:3]))

__version__ = "1.0.0"

# ---------------------------------------------------------------- configuracao

RAIZ = os.path.dirname(os.path.abspath(__file__))
DADOS = os.environ.get("MEMEGEN_DATA") or os.path.join(RAIZ, "dados")
IMAGENS = os.path.join(DADOS, "templates")
CATALOGO = os.path.join(DADOS, "catalogo.json")
CONTRATOS = os.path.join(DADOS, "contratos.json")
ESTADO = os.path.join(DADOS, "estado.json")
CERT = os.path.join(DADOS, "cert.pem")
CHAVE = os.path.join(DADOS, "key.pem")

BASE_9GAG = "https://meme.9gag.com"
PAGINA_9GAG = BASE_9GAG + "/meme-generator/"

# Dois provedores. A escolha e automatica pela chave presente, e
# MEMEGEN_PROVIDER decide quando as duas estao definidas.
API = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
VERSAO_API = "2023-06-01"
API_GEMINI = os.environ.get("GEMINI_BASE_URL",
                            "https://generativelanguage.googleapis.com")

PADROES = {
    "anthropic": {"enriquecer": "claude-sonnet-5", "gerar": "claude-opus-5",
                  "triar": "claude-haiku-4-5"},
    "gemini": {"enriquecer": "gemini-3.5-flash-lite", "gerar": "gemini-3.8-flash",
               "triar": "gemini-3.5-flash-lite"},
}


def provedor():
    """Qual provedor usar. A chave presente decide; a variavel desempata."""
    escolhido = (os.environ.get("MEMEGEN_PROVIDER") or "").strip().lower()
    if escolhido:
        if escolhido not in PADROES:
            raise RuntimeError("MEMEGEN_PROVIDER deve ser 'anthropic' ou 'gemini'")
        return escolhido
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    return "anthropic"


def modelo_padrao(papel):
    """Modelo para 'enriquecer', 'gerar' ou 'triar', conforme o provedor."""
    env = {"enriquecer": "MEMEGEN_ENRICH_MODEL",
           "gerar": "MEMEGEN_GENERATE_MODEL",
           "triar": "MEMEGEN_TRIAGE_MODEL"}[papel]
    return os.environ.get(env) or PADROES[provedor()][papel]

# O editor do 9GAG posiciona as caixas num canvas de largura fixa, com altura
# proporcional. Verificado nos 836 templates: nenhuma caixa ultrapassa esses
# limites e 744 encostam exatamente em x=640.
LARGURA_CANVAS = 640.0

AGENTE = "memegen/%s (personal project; syncs public meme template catalog)" % __version__


def agora():
    """Timestamp ISO em UTC. datetime.utcnow() e obsoleto no 3.12+."""
    try:
        from datetime import timezone
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    except ImportError:               # pragma: no cover
        return datetime.utcnow().replace(microsecond=0).isoformat() + "+00:00"


def garantir_dirs():
    for d in (DADOS, IMAGENS):
        if not os.path.isdir(d):
            os.makedirs(d)


# ---------------------------------------------------------------- HTTP simples

def buscar(url, dados=None, cabecalhos=None, timeout=60):
    """GET ou POST com urllib. Devolve bytes."""
    cab = {"User-Agent": AGENTE}
    if cabecalhos:
        cab.update(cabecalhos)
    corpo = None
    if dados is not None:
        corpo = json.dumps(dados).encode("utf-8")
        cab.setdefault("Content-Type", "application/json")
    req = Request(url, data=corpo, headers=cab)
    resposta = urlopen(req, timeout=timeout)
    try:
        return resposta.read()
    finally:
        resposta.close()


def buscar_texto(url, **kw):
    return buscar(url, **kw).decode("utf-8", "replace")


def tentar(funcao, tentativas=4, espera=2.0):
    """Repete com espera crescente. A rede do 9GAG derruba conexao as vezes."""
    ultimo = None
    for n in range(tentativas):
        try:
            return funcao()
        except (HTTPError, URLError, socket.error, ssl.SSLError) as e:
            ultimo = e
            if n == tentativas - 1:
                break
            time.sleep(espera * (2 ** n))
    raise ultimo


# ------------------------------------------------- catalogo do 9GAG (sync)

# O gerador do 9GAG e estatico: nao ha API. O catalogo vive num bundle JS cujo
# nome carrega o hash do conteudo, descoberto por uma cadeia de tres passos:
#
#   /meme-generator/            (~8 KB)   -> assets/manifest-<build>.js
#   /assets/manifest-<build>.js (~4 KB)   -> assets/meme-templates-<hash>.js
#   /assets/meme-templates-<hash>.js      -> o catalogo (~1,4 MB)
#
# Os dois primeiros passos custam 12 KB e bastam para detectar mudanca. O
# bundle grande so e baixado quando o hash muda.

RE_MANIFEST = re.compile(r"assets/manifest-[A-Za-z0-9_-]+\.js")
RE_TEMPLATES = re.compile(r"meme-templates-[A-Za-z0-9_-]+\.js")


def ponteiro_9gag():
    """Resolve o build atual gastando ~12 KB. Nao baixa o catalogo."""
    html = tentar(lambda: buscar_texto(PAGINA_9GAG, timeout=30))
    m = RE_MANIFEST.search(html)
    if not m:
        raise RuntimeError("nao encontrei o manifest no HTML do 9GAG - "
                           "o layout do build mudou")
    manifest = m.group(0)
    js = tentar(lambda: buscar_texto(BASE_9GAG + "/" + manifest, timeout=30))
    t = RE_TEMPLATES.search(js)
    if not t:
        raise RuntimeError("o manifest nao referencia mais um bundle "
                           "meme-templates-*.js")
    return {"manifest": manifest, "templates": t.group(0)}


def _desescapar(bruto):
    """Desfaz o escape de template literal do JS (\\\\, \\`, \\$)."""
    saida = []
    i = 0
    n = len(bruto)
    while i < n:
        c = bruto[i]
        if c == "\\" and i + 1 < n and bruto[i + 1] in "\\`$":
            saida.append(bruto[i + 1])
            i += 2
            continue
        saida.append(c)
        i += 1
    return "".join(saida)


def extrair_catalogo(bundle):
    """Extrai o array de templates de dentro do bundle JS."""
    m = re.search(r"JSON\.parse\(`", bundle)
    if not m:
        raise RuntimeError("nao achei o JSON.parse(`...`) no bundle - "
                           "o empacotamento mudou")
    inicio = m.end()
    j = inicio
    while True:
        j = bundle.index("`", j)
        if bundle[j - 1] != "\\":
            break
        j += 1
    dados = json.loads(_desescapar(bundle[inicio:j]))
    if not isinstance(dados, list) or not dados:
        raise RuntimeError("o bloco JSON encontrado nao e uma lista de templates")
    return dados


def baixar_catalogo(ponteiro=None):
    ponteiro = ponteiro or ponteiro_9gag()
    url = BASE_9GAG + "/assets/" + ponteiro["templates"]
    bundle = tentar(lambda: buscar_texto(url, timeout=180))
    return ponteiro, extrair_catalogo(bundle)


def baixar_imagem(caminho_asset, destino):
    dados = tentar(lambda: buscar(BASE_9GAG + caminho_asset, timeout=60))
    pasta = os.path.dirname(destino)
    if pasta and not os.path.isdir(pasta):
        os.makedirs(pasta)
    with open(destino, "wb") as f:
        f.write(dados)


# ------------------------------------------------------- armazenamento local

def ler_json(caminho, padrao):
    if not os.path.exists(caminho):
        return padrao
    with io.open(caminho, "r", encoding="utf-8") as f:
        return json.load(f)


def gravar_json(caminho, dados):
    pasta = os.path.dirname(caminho)
    if pasta and not os.path.isdir(pasta):
        os.makedirs(pasta)
    tmp = caminho + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(dados, ensure_ascii=False, indent=1))
    # troca atomica: nunca deixa o catalogo pela metade
    if os.path.exists(caminho) and os.name == "nt":
        os.remove(caminho)
    os.rename(tmp, caminho)


def hash_fonte(template):
    """Impressao digital dos campos que afetam o enriquecimento.

    Mudou o texto, as keywords ou a geometria das caixas -> o contrato precisa
    ser refeito. Mudou so a miniatura ou o placeholder -> nao precisa.
    """
    carga = {
        "name": template.get("name"),
        "year": template.get("year"),
        "description": template.get("description"),
        "keywords": template.get("keywords"),
        "url": template.get("url"),
        "width": template.get("width"),
        "height": template.get("height"),
        "boxes": [dict((k, b.get(k)) for k in ("id", "x", "y", "width", "height"))
                  for b in template.get("textBoxes", [])],
    }
    bruto = json.dumps(carga, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"))
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:16]


def indexar(templates):
    """Lista do 9GAG -> dicionario por id, com hash e posicao de popularidade.

    A ordem do array do 9GAG e o ranking de popularidade deles (Drake, Two
    Buttons e Distracted Boyfriend nas tres primeiras posicoes). Guardamos o
    indice porque e o unico sinal de popularidade disponivel, e serve de
    desempate quando dois templates servem a mesma situacao.
    """
    saida = {}
    for posicao, t in enumerate(templates):
        item = dict(t)
        item["_rank"] = posicao
        item["_hash"] = hash_fonte(t)
        saida[t["id"]] = item
    return saida


def diferenca(antigo, novo):
    d = {"novos": [], "alterados": [], "removidos": [], "iguais": []}
    for tid, t in novo.items():
        if tid not in antigo:
            d["novos"].append(tid)
        elif antigo[tid].get("_hash") != t["_hash"]:
            d["alterados"].append(tid)
        else:
            d["iguais"].append(tid)
    d["removidos"] = [tid for tid in antigo if tid not in novo]
    return d


def contratos_pendentes(catalogo, contratos):
    """Templates sem contrato, ou cujo contrato veio de uma versao antiga."""
    fora = []
    for tid, t in catalogo.items():
        c = contratos.get(tid)
        if c is None or c.get("source_hash") != t["_hash"]:
            fora.append(tid)
    return fora


def tamanho_canvas(largura, altura):
    return LARGURA_CANVAS, LARGURA_CANVAS * altura / float(largura)


# ------------------------------------------------------------ API da Anthropic

def chave_api():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if not k:
        raise RuntimeError(
            "defina ANTHROPIC_API_KEY. Sem chave da API o enriquecimento e a "
            "geracao nao rodam (sync, serve e navegacao funcionam sem ela).")
    return k


def _cabecalhos_api():
    cab = {"x-api-key": chave_api(),
           "anthropic-version": VERSAO_API,
           "Content-Type": "application/json"}
    # Chave de organizacao (nao amarrada a um workspace) exige dizer qual
    # workspace usar; chave ja escopada a um workspace dispensa o header.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace:
        cab["anthropic-workspace-id"] = workspace
    return cab


def chamar_api(caminho, carga=None, metodo=None, timeout=300):
    """Chamada a API da Anthropic. Levanta com o corpo do erro, que e onde a
    mensagem util fica - o HTTPError sozinho so diz '400 Bad Request'."""
    url = API + caminho
    cab = _cabecalhos_api()
    corpo = json.dumps(carga).encode("utf-8") if carga is not None else None
    req = Request(url, data=corpo, headers=cab)
    if metodo:
        req.get_method = lambda: metodo
    try:
        r = urlopen(req, timeout=timeout)
        try:
            return json.loads(r.read().decode("utf-8"))
        finally:
            r.close()
    except HTTPError as e:
        # o HTTPError tambem e um arquivo: sem fechar, o 3.14 emite
        # ResourceWarning quando o coletor o recolhe
        try:
            detalhe = e.read().decode("utf-8", "replace")[:500]
        finally:
            e.close()
        try:
            detalhe = json.loads(detalhe).get("error", {}).get("message", detalhe)
        except ValueError:
            pass
        if "anthropic-workspace-id" in detalhe and not os.environ.get("ANTHROPIC_WORKSPACE_ID"):
            detalhe += ("\n\nSua chave e de organizacao. Duas saidas:\n"
                        "  1. defina ANTHROPIC_WORKSPACE_ID com o id do workspace\n"
                        "     (Console > Settings > Workspaces, comeca com wrkspc_)\n"
                        "  2. ou crie uma chave ja escopada a um workspace")
        raise RuntimeError("API respondeu %s: %s" % (e.code, detalhe))


def texto_da_resposta(mensagem):
    for bloco in mensagem.get("content", []):
        if bloco.get("type") == "text":
            return bloco.get("text", "")
    return ""


# ------------------------------------------------------------- provedor Gemini

# A API do Gemini e o endpoint /interactions: `input` e uma lista de blocos e o
# schema vai em `response_format` no topo, nao dentro de generationConfig como
# no antigo generateContent.

def chave_gemini():
    k = os.environ.get("GEMINI_API_KEY")
    if not k:
        raise RuntimeError(
            "defina GEMINI_API_KEY. Pegue em https://aistudio.google.com/apikey")
    return k


def _detalhe_gemini(bruto):
    """Extrai status, motivo e mensagem do corpo de erro do Google.

    O corpo traz mais do que a mensagem: `status` (PERMISSION_DENIED,
    RESOURCE_EXHAUSTED...) e um `details` com o motivo real. Guardar so a
    mensagem escondia que um 403 podia ser limite de taxa disfarcado.
    """
    try:
        erro = json.loads(bruto).get("error", {})
    except ValueError:
        return bruto[:300], ""
    partes = []
    if erro.get("status"):
        partes.append(erro["status"])
    if erro.get("message"):
        partes.append(erro["message"])
    motivos = []
    for det in erro.get("details") or []:
        for chave in ("reason", "@type", "domain"):
            if det.get(chave):
                motivos.append(str(det[chave]))
    if motivos:
        partes.append("(" + "; ".join(motivos[:3]) + ")")
    return " ".join(partes) or bruto[:300], erro.get("status", "")


def _e_temporario(codigo, detalhe):
    """Vale a pena repetir? Limite de taxa e falha do servidor, sim.

    O Google devolve 403 tanto para permissao de verdade quanto para limite de
    taxa, entao o codigo sozinho nao decide -- e preciso olhar o motivo.
    """
    if codigo == 429 or codigo >= 500:
        return True
    if codigo == 403:
        alvo = detalhe.lower()
        return any(p in alvo for p in ("rate", "quota", "exhaust", "limit",
                                       "too many", "unavailable"))
    return False


def chamar_gemini(sistema, blocos, schema=None, modelo=None, max_tokens=4000,
                  timeout=90, tentativas=4):
    """Uma interacao com o Gemini. Devolve (texto, uso).

    Timeout curto de proposito: uma chamada presa com timeout longo parece a
    aplicacao travada. Falhar rapido e repetir e melhor do que esperar.
    """
    carga = {"model": modelo, "input": blocos}
    if sistema:
        carga["system_instruction"] = sistema
    if schema:
        carga["response_format"] = {"type": "text",
                                    "mime_type": "application/json",
                                    "schema": schema}
    url = API_GEMINI.rstrip("/") + "/v1beta/interactions"
    cab = {"x-goog-api-key": chave_gemini(), "Content-Type": "application/json"}
    corpo = json.dumps(carga).encode("utf-8")

    resposta = None
    for n in range(tentativas):
        try:
            r = urlopen(Request(url, data=corpo, headers=cab), timeout=timeout)
            try:
                resposta = json.loads(r.read().decode("utf-8"))
            finally:
                r.close()
            break
        except HTTPError as e:
            try:
                bruto = e.read().decode("utf-8", "replace")
            finally:
                e.close()
            detalhe, _ = _detalhe_gemini(bruto)
            if _e_temporario(e.code, detalhe) and n < tentativas - 1:
                time.sleep(2 ** n * 3)        # 3s, 6s, 12s
                continue
            raise RuntimeError("Gemini respondeu %s: %s" % (e.code, detalhe))
        except (URLError, socket.error, ssl.SSLError) as e:
            if n == tentativas - 1:
                raise RuntimeError("Gemini nao respondeu: %s" % e)
            time.sleep(2 ** n * 3)

    texto = resposta.get("output_text")
    if not texto:
        # caminho alternativo documentado: o ultimo passo carrega o conteudo
        for passo in reversed(resposta.get("steps") or []):
            for bloco in passo.get("content") or []:
                if bloco.get("text"):
                    texto = bloco["text"]
                    break
            if texto:
                break
    if not texto:
        raise RuntimeError("resposta do Gemini sem texto: %s"
                           % json.dumps(resposta)[:300])
    return texto, _uso_gemini(resposta)


def _uso_gemini(resposta):
    """Contagem de tokens. Os nomes variam entre versoes da API, entao
    procuramos os que ja apareceram antes de desistir e devolver zero."""
    u = (resposta.get("usage") or resposta.get("usageMetadata")
         or resposta.get("usage_metadata") or {})

    def pega(*nomes):
        for n in nomes:
            if isinstance(u.get(n), int):
                return u[n]
        return 0

    return {"input": pega("input_tokens", "promptTokenCount", "inputTokens",
                          "prompt_tokens"),
            "output": pega("output_tokens", "candidatesTokenCount",
                           "outputTokens", "completion_tokens"),
            "cache_read": pega("cached_content_token_count",
                               "cachedContentTokenCount"),
            "cache_write": 0}


# ------------------------------------------------- chamada unica, sem provedor

def _blocos_anthropic(texto, imagem=None):
    blocos = []
    if imagem:
        blocos.append(_bloco_imagem(imagem))
    blocos.append({"type": "text", "text": texto})
    return blocos


def _blocos_gemini(texto, imagem=None):
    blocos = [{"type": "text", "text": texto}]
    if imagem:
        with open(imagem, "rb") as f:
            dados = base64.b64encode(f.read()).decode("ascii")
        blocos.append({"type": "image", "data": dados, "mime_type": "image/jpeg"})
    return blocos


def gerar_json(sistema, texto, schema, papel, imagem=None, modelo=None,
               max_tokens=4000, cachear_sistema=False):
    """Pede uma resposta em JSON ao provedor ativo. Devolve (dados, uso).

    `cachear_sistema` marca o ultimo bloco do sistema para prompt caching no
    Anthropic; no Gemini nao ha equivalente explicito e o campo e ignorado.
    """
    modelo = modelo or modelo_padrao(papel)
    qual = provedor()

    if qual == "gemini":
        bruto, uso = chamar_gemini(
            "\n\n".join(sistema) if isinstance(sistema, list) else sistema,
            _blocos_gemini(texto, imagem), schema, modelo, max_tokens)
    else:
        partes = sistema if isinstance(sistema, list) else [sistema]
        blocos_sistema = []
        for i, parte in enumerate(partes):
            bloco = {"type": "text", "text": parte}
            if cachear_sistema and i == len(partes) - 1:
                bloco["cache_control"] = {"type": "ephemeral"}
            blocos_sistema.append(bloco)
        resposta = chamar_api("/v1/messages", {
            "model": modelo, "max_tokens": max_tokens,
            "system": blocos_sistema,
            "messages": [{"role": "user",
                          "content": _blocos_anthropic(texto, imagem)}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        })
        bruto, uso = texto_da_resposta(resposta), _uso(resposta)

    try:
        return json.loads(bruto), uso
    except ValueError:
        raise RuntimeError("o modelo nao devolveu JSON valido: %s" % bruto[:300])


# ------------------------------------------------------------- enriquecimento

# O 9GAG entrega `description` (a historia do meme) e `keywords`, mas nao diz
# QUAL texto vai em QUAL caixa. Em distracted_boyfriend a ordem das caixas e
# mulher-de-vermelho, namorado, namorada -- trocar isso destroi a piada. So 40
# dos 836 descrevem os papeis explicitamente. Este passo resolve isso uma vez
# por template e o resultado vira dado estatico.

SISTEMA_ENRIQUECER = u"""\
Voce cataloga templates de meme para um gerador automatico em portugues do Brasil.

Para cada template voce recebe: a imagem, o nome, o ano, a descricao em ingles \
escrita pelo 9GAG e a posicao de cada caixa de texto sobre a imagem.

Produza um contrato de uso. O campo mais importante e o papel de cada caixa: \
olhe a imagem e a posicao da caixa e diga o que aquele texto especifico \
representa. A ordem das caixas NAO e a ordem semantica -- em "Distracted \
Boyfriend" a primeira caixa fica sobre a mulher de vermelho (a tentacao), a \
segunda sobre o namorado (quem se distrai) e a terceira sobre a namorada (o que \
esta sendo ignorado).

Regras:
- Escreva tudo em portugues do Brasil.
- `funcao`: em uma frase, para que serve este meme -- a funcao pragmatica, nao a \
descricao visual. "Contrastar duas opcoes rejeitando a primeira", nao "homem de \
casaco laranja".
- `quando_usar`: 3 a 6 situacoes concretas em que ele se aplica. Pense em como \
alguem descreveria a propria situacao, nao em como descreveria a imagem.
- `tom`: 1 a 3 marcadores (ex.: sarcastico, autodepreciativo, triunfal, \
resignado, absurdo).
- `slots`: um por caixa, na mesma ordem recebida, repetindo o `box_id` exato. \
`papel` diz o que entra ali; `exemplo` e um texto curto e plausivel em pt-BR.
- `max_chars`: limite realista para o texto caber na caixa, dado o tamanho dela.
- `nsfw`: true se o template envolver conteudo sexual, violento ou ofensivo.
"""

ESQUEMA_CONTRATO = {
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


def _posicao(caixa, cw, ch):
    """Descricao espacial da caixa, para o modelo casar caixa com elemento."""
    cx = (caixa["x"] + caixa["width"] / 2.0) / cw
    cy = (caixa["y"] + caixa["height"] / 2.0) / ch
    h = "esquerda" if cx < 0.37 else ("direita" if cx > 0.63 else "centro")
    v = "topo" if cy < 0.37 else ("base" if cy > 0.63 else "meio")
    return ("%s-%s (centro em %d%% da largura, %d%% da altura; caixa ocupa "
            "%d%% x %d%% da imagem)"
            % (v, h, round(cx * 100), round(cy * 100),
               round(100 * caixa["width"] / cw), round(100 * caixa["height"] / ch)))


def prompt_enriquecimento(t):
    cw, ch = tamanho_canvas(t["width"], t["height"])
    linhas = ["Nome: " + t["name"]]
    linhas.append("Ano: %s" % t["year"] if t.get("year", 0) > 0 else "Ano: desconhecido")
    linhas.append(u"Descricao do 9GAG: " + t.get("description", ""))
    linhas.append(u"Keywords do 9GAG: " + ", ".join(t.get("keywords", [])))
    linhas.append("")
    linhas.append(u"Caixas de texto (%d), na ordem do template:" % len(t["textBoxes"]))
    for b in t["textBoxes"]:
        linhas.append("  - box_id=%s: %s" % (b["id"], _posicao(b, cw, ch)))
    return "\n".join(linhas)


def _bloco_imagem(caminho):
    with open(caminho, "rb") as f:
        dados = base64.b64encode(f.read()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": dados}}


def garantir_imagens(catalogo, ids, aviso=True):
    """Garante a imagem local de cada template. Devolve os que falharam."""
    falhas = []
    total = len(ids)
    for n, tid in enumerate(ids, 1):
        destino = os.path.join(IMAGENS, tid + ".jpg")
        if os.path.exists(destino) and os.path.getsize(destino) > 0:
            continue
        try:
            baixar_imagem(catalogo[tid]["url"], destino)
        except Exception as e:
            falhas.append(tid)
            print("  falha ao baixar %s: %s" % (tid, e))
        if aviso and n % 100 == 0:
            print("  %d/%d" % (n, total))
            sys.stdout.flush()
    return falhas


def enviar_lote(catalogo, ids, modelo=None):
    """Enfileira o enriquecimento na Batch API (metade do preco)."""
    modelo = modelo or modelo_padrao("enriquecer")
    requisicoes = []
    for tid in ids:
        img = os.path.join(IMAGENS, tid + ".jpg")
        if not os.path.exists(img):
            continue
        requisicoes.append({
            "custom_id": tid,
            "params": {
                "model": modelo,
                "max_tokens": 2000,
                "system": SISTEMA_ENRIQUECER,
                "messages": [{"role": "user", "content": [
                    _bloco_imagem(img),
                    {"type": "text", "text": prompt_enriquecimento(catalogo[tid])},
                ]}],
                "output_config": {"format": {"type": "json_schema",
                                             "schema": ESQUEMA_CONTRATO}},
            },
        })
    if not requisicoes:
        raise RuntimeError("nenhum template com imagem disponivel para enriquecer")
    lote = chamar_api("/v1/messages/batches", {"requests": requisicoes})
    print("lote %s criado com %d templates (%s)"
          % (lote["id"], len(requisicoes), modelo))
    return lote["id"]


def _cota_esgotada(texto):
    """Cota do dia acabou (parar) ou so limite por minuto (ja foi repetido)?

    O retry em chamar_gemini ja absorve o limite por minuto. Chegar aqui com
    mensagem de cota significa que repetir nao adiantou.
    """
    alvo = texto.lower()
    diario = ("per day" in alvo or "daily" in alvo or "perday" in alvo
              or "exhausted" in alvo or "billing" in alvo)
    return diario and ("quota" in alvo or "resource" in alvo or "limit" in alvo)


def enriquecer_sequencial(catalogo, ids, modelo=None, pausa=1.0):
    """Enriquecimento um a um, para provedores sem API de lote.

    Grava depois de cada template em vez de so no fim: uma cota estourada ou um
    Ctrl+C no meio nao perdem o que ja foi feito, e a proxima execucao continua
    de onde parou porque `contratos_pendentes` ja exclui o que tem contrato.
    """
    modelo = modelo or modelo_padrao("enriquecer")
    contratos = ler_json(CONTRATOS, {})
    novos = {}
    erros = []
    total = len(ids)
    inicio = time.time()
    print("%d templates, um a um com %s (pausa de %.1fs)" % (total, modelo, pausa))
    print("Ctrl+C a qualquer momento: o que ja terminou fica gravado.\n")

    for n, tid in enumerate(ids, 1):
        # imprime ANTES da chamada: enquanto ela demora, a linha na tela ja diz
        # em que template estamos, em vez de parecer congelado
        restante = ""
        if n > 3:
            por_item = (time.time() - inicio) / (n - 1)
            faltam = int(por_item * (total - n + 1))
            restante = "  ~%dmin restantes" % max(1, faltam // 60)
        sys.stdout.write("  [%d/%d] %s%s\n" % (n, total, tid, restante))
        sys.stdout.flush()

        img = os.path.join(IMAGENS, tid + ".jpg")
        if not os.path.exists(img):
            erros.append((tid, "imagem ausente"))
            continue
        try:
            dados, _ = gerar_json(SISTEMA_ENRIQUECER,
                                  prompt_enriquecimento(catalogo[tid]),
                                  ESQUEMA_CONTRATO, "enriquecer", imagem=img,
                                  modelo=modelo, max_tokens=2000)
        except RuntimeError as e:
            texto = str(e)
            if _cota_esgotada(texto):
                print("\n  cota diaria esgotada em %s (%d de %d feitos)."
                      % (tid, n - 1, total))
                print("  O que ja foi feito esta gravado. Rode de novo quando a "
                      "cota renovar e ele continua daqui.")
                break
            erros.append((tid, texto))
            print("      falhou: %s" % texto[:160])
            continue

        dados["source_hash"] = catalogo[tid]["_hash"]
        dados["enriched_at"] = agora()
        dados["model"] = modelo
        contratos[tid] = dados
        novos[tid] = dados
        gravar_json(CONTRATOS, contratos)      # grava a cada um: retomavel

        if pausa:
            time.sleep(pausa)

    print("\ncontratos gravados: %d ok, %d com erro" % (len(novos), len(erros)))
    if erros:
        print("falharam (rode de novo para tentar so estes):")
        for tid, motivo in erros[:10]:
            print("  %s: %s" % (tid, motivo[:120]))
        if len(erros) > 10:
            print("  ... e mais %d" % (len(erros) - 10))
    return novos


def coletar_lote(lote_id, catalogo, intervalo=30):
    """Aguarda o lote e grava os contratos. Devolve so os novos."""
    while True:
        lote = chamar_api("/v1/messages/batches/" + lote_id)
        if lote.get("processing_status") == "ended":
            break
        c = lote.get("request_counts", {})
        print("  %s: %s prontos, %s na fila"
              % (lote.get("processing_status"), c.get("succeeded"),
                 c.get("processing")))
        sys.stdout.flush()
        time.sleep(intervalo)

    url = lote.get("results_url")
    if not url:
        raise RuntimeError("o lote terminou sem results_url")
    bruto = buscar_texto(url, cabecalhos=_cabecalhos_api(), timeout=300)

    contratos = ler_json(CONTRATOS, {})
    novos = {}
    erros = 0
    for linha in bruto.splitlines():
        linha = linha.strip()
        if not linha:
            continue
        item = json.loads(linha)
        tid = item.get("custom_id")
        resultado = item.get("result", {})
        if resultado.get("type") != "succeeded":
            erros += 1
            print("  %s: %s" % (tid, resultado.get("type")))
            continue
        try:
            dados = json.loads(texto_da_resposta(resultado["message"]))
        except (ValueError, KeyError):
            erros += 1
            print("  %s: resposta invalida" % tid)
            continue
        dados["source_hash"] = catalogo[tid]["_hash"]
        dados["enriched_at"] = agora()
        dados["model"] = resultado["message"].get("model")
        contratos[tid] = dados
        novos[tid] = dados

    gravar_json(CONTRATOS, contratos)
    print("contratos gravados: %d ok, %d com erro" % (len(novos), erros))
    return novos


# -------------------------------------------------------------------- geracao

SISTEMA_GERAR = u"""\
Voce escolhe memes e escreve o texto deles, em portugues do Brasil.

Recebe uma situacao -- pode ser um desabafo, um trecho de conversa, um bug, uma \
reuniao, qualquer coisa -- e devolve os memes que melhor se aplicam, ja com o \
texto de cada caixa preenchido.

Como escolher:
- Case pela FUNCAO do meme, nao por palavra em comum. Se a situacao e "troquei \
de framework de novo", o meme certo e o que serve para "ser tentado por algo \
novo ignorando o que ja funciona" -- nao um que por acaso cite programacao.
- Varie o formato entre as sugestoes. Tres memes de comparacao lado a lado e uma \
sugestao so, repetida.
- Em empate, prefira o de menor `rank` (mais reconhecivel).
- Respeite o papel de cada caixa. O papel esta escrito no catalogo; a ordem das \
caixas nao e a ordem semantica.

Como escrever:
- Texto de meme e curto e seco. Nao explique a piada, nao repita a situacao, nao \
comece com "quando voce".
- Respeite o `max_chars` de cada slot. Texto que estoura a caixa e encolhido ate \
ficar ilegivel.
- Use o portugues que a pessoa usou. Se ela escreveu com giria, mantenha.
- Preencha TODAS as caixas do template escolhido, usando o `box_id` exato.
- `porque`: uma frase dizendo por que este meme serve para esta situacao.
"""

ESQUEMA_RESULTADO = {
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

SISTEMA_TRIAGEM = u"""\
Voce faz a triagem de um catalogo de memes.

Recebe uma situacao e uma lista de templates, cada um com sua funcao. Devolve os \
ids dos mais promissores -- case pela FUNCAO do meme, nao por palavra em comum \
com a situacao. Seja generoso: e uma peneira grossa, outro modelo faz a escolha \
final. Inclua formatos variados, nao so variacoes do mesmo tipo de piada.
"""

ESQUEMA_TRIAGEM = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["ids"],
    "additionalProperties": False,
}


def texto_catalogo(catalogo, contratos, nsfw=False):
    """Serializa o catalogo enriquecido no formato que vai para o modelo."""
    itens = sorted(catalogo.items(), key=lambda kv: kv[1].get("_rank", 9999))
    blocos = []
    for tid, t in itens:
        c = contratos.get(tid)
        if not c or (c.get("nsfw") and not nsfw):
            continue
        slots = "; ".join("%s=%s (max %s)" % (s["box_id"], s["papel"], s["max_chars"])
                          for s in c.get("slots", []))
        blocos.append(
            u"### %s | %s | rank %s\nfuncao: %s\nusar: %s\ntom: %s\nslots: %s"
            % (tid, t["name"], t.get("_rank", "?"), c["funcao"],
               "; ".join(c.get("quando_usar", [])), ", ".join(c.get("tom", [])),
               slots))
    return "\n\n".join(blocos)


def texto_triagem(catalogo, contratos, nsfw=False):
    """Versao enxuta: so id e funcao, para a peneira grossa."""
    itens = sorted(catalogo.items(), key=lambda kv: kv[1].get("_rank", 9999))
    linhas = []
    for tid, t in itens:
        c = contratos.get(tid)
        if not c or (c.get("nsfw") and not nsfw):
            continue
        linhas.append(u"%s: %s" % (tid, c["funcao"]))
    return "\n".join(linhas)


def _uso(resposta):
    u = resposta.get("usage", {})
    return {"input": u.get("input_tokens", 0),
            "output": u.get("output_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0) or 0,
            "cache_write": u.get("cache_creation_input_tokens", 0) or 0}


def triar(situacao, catalogo, contratos, k=30, modelo=None, nsfw=False):
    """Peneira o catalogo com um modelo barato. Devolve (ids, uso)."""
    dados, uso = gerar_json(
        [SISTEMA_TRIAGEM, u"CATALOGO\n\n" + texto_triagem(catalogo, contratos, nsfw)],
        u"Separe ate %d templates para esta situacao:\n\n%s" % (k, situacao),
        ESQUEMA_TRIAGEM, "triar", modelo=modelo, max_tokens=1500,
        cachear_sistema=True)
    ids = dados.get("ids", [])
    return [i for i in ids if i in contratos][:k], uso


def sugerir(situacao, catalogo, contratos, n=3, modelo=None, modo="shortlist",
            k=30, nsfw=False):
    """Da situacao para (sugestoes, uso).

    Uma unica chamada escolhe os templates e escreve o texto, porque separar as
    duas coisas obriga o seletor a decidir sem saber se o texto vai funcionar.
    """
    if not contratos:
        raise RuntimeError("nenhum contrato encontrado - rode `enrich` antes de gerar")

    uso_triagem = None
    if modo == "shortlist":
        ids, uso_triagem = triar(situacao, catalogo, contratos, k, nsfw=nsfw)
        if ids:
            contratos = dict((i, contratos[i]) for i in ids if i in contratos)
            catalogo = dict((i, catalogo[i]) for i in ids if i in catalogo)

    dados, uso = gerar_json(
        [SISTEMA_GERAR,
         u"CATALOGO DE TEMPLATES\n\n" + texto_catalogo(catalogo, contratos, nsfw)],
        u"Sugira %d memes para esta situacao:\n\n%s" % (n, situacao),
        ESQUEMA_RESULTADO, "gerar", modelo=modelo, cachear_sistema=True)
    sugestoes = dados.get("sugestoes", [])
    uso["triagem"] = uso_triagem
    uso["candidatos"] = len(contratos)
    uso["modo"] = modo
    uso["provedor"] = provedor()
    return sugestoes, uso


# --------------------------------------------------------------------- pagina

# A renderizacao mora aqui, em canvas, e nao no servidor. Nao e contorno da
# falta de Pillow: e o que o proprio 9GAG faz, e resolve de graca o que era
# trabalhoso do lado Python -- fonte Impact, contorno, rotacao -- usando o
# motor de texto do navegador. De quebra, editar um texto passa a ser instantaneo,
# sem ida e volta ao servidor.

PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memegen</title>
<style>
:root {
  color-scheme: light dark;
  --bg:#fbfaf8; --surface:#fff; --border:#e3e0da; --text:#1b1a17; --muted:#6d6a63;
  --accent:#b8502a; --accent-text:#fff;
  --warn-bg:#fdf4e3; --warn-border:#e5c98c; --warn-text:#6b4e12; --radius:10px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#171614; --surface:#211f1c; --border:#35322d; --text:#edeae4; --muted:#9b968c;
    --accent:#d4713f; --accent-text:#171614;
    --warn-bg:#2e2617; --warn-border:#5c4a23; --warn-text:#e0c68d;
  }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1080px; margin:0 auto; padding:24px 16px 64px; }
header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
h1 { font-size:22px; margin:0; letter-spacing:-.01em; }
.sub, .meta { color:var(--muted); font-size:13px; }
nav { display:flex; gap:4px; margin:20px 0 18px; border-bottom:1px solid var(--border); }
nav button { background:none; border:none; border-bottom:2px solid transparent;
  padding:8px 14px; font:inherit; font-weight:500; color:var(--muted);
  cursor:pointer; margin-bottom:-1px; }
nav button[aria-selected="true"] { color:var(--text); border-bottom-color:var(--accent); }
.aviso { background:var(--warn-bg); border:1px solid var(--warn-border);
  color:var(--warn-text); padding:10px 13px; border-radius:var(--radius);
  font-size:14px; margin-bottom:16px; }
.aviso code { background:rgba(0,0,0,.09); padding:1px 5px; border-radius:4px; }
textarea, input[type=text], select { width:100%; font:inherit; color:var(--text);
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); padding:11px 13px; }
textarea { min-height:96px; resize:vertical; }
textarea:focus, input:focus, select:focus { outline:2px solid var(--accent); outline-offset:-1px; }
.linha { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:11px; }
.linha label { font-size:13px; color:var(--muted); display:flex; align-items:center; gap:6px; }
select { width:auto; padding:7px 10px; font-size:14px; }
button.primario { background:var(--accent); color:var(--accent-text); border:none;
  border-radius:var(--radius); padding:10px 20px; font:inherit; font-weight:600; cursor:pointer; }
button.primario:disabled { opacity:.5; cursor:default; }
button.leve, a.leve { display:inline-block; background:var(--surface); color:var(--text);
  border:1px solid var(--border); border-radius:7px; padding:6px 12px; font:inherit;
  font-size:13px; line-height:1.4; cursor:pointer; text-decoration:none; }
button.leve:hover:not(:disabled), a.leve:hover { border-color:var(--accent); }
button.leve:disabled { opacity:.55; cursor:default; }
.exemplos { margin-top:10px; display:flex; gap:6px; flex-wrap:wrap; }
.exemplos button { background:none; border:1px dashed var(--border); color:var(--muted);
  border-radius:999px; padding:4px 11px; font:inherit; font-size:12.5px; cursor:pointer; }
.exemplos button:hover { color:var(--text); border-color:var(--accent); border-style:solid; }
.barra { display:flex; align-items:center; justify-content:space-between; gap:12px;
  margin-top:20px; padding-bottom:10px; border-bottom:1px solid var(--border); }
.card { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); padding:16px; margin-top:16px; }
.card h3 { margin:0 0 2px; font-size:16px; }
.porque { font-size:14px; margin:9px 0 13px; color:var(--muted); font-style:italic; }
.duas { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:18px; align-items:start; }
@media (max-width:720px) { .duas { grid-template-columns:1fr; } }
.duas canvas { width:100%; max-height:62vh; object-fit:contain; border-radius:7px;
  display:block; border:1px solid var(--border); background:var(--bg); }
.campo { margin-bottom:11px; }
.campo label { display:flex; justify-content:space-between; gap:8px; font-size:12px;
  color:var(--muted); margin-bottom:4px; }
.campo .papel { font-weight:600; color:var(--text); }
.campo .conta { font-variant-numeric:tabular-nums; }
.campo .conta.estourou { color:var(--accent); font-weight:600; }
.campo input { font-size:14px; padding:8px 11px; }
.grade { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:12px; margin-top:16px; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:8px;
  overflow:hidden; cursor:pointer; text-align:left; padding:0; font:inherit; color:inherit; }
.tile:hover { border-color:var(--accent); }
.tile img { width:100%; aspect-ratio:1; object-fit:cover; display:block; background:var(--bg); }
.tile div { padding:7px 9px; font-size:12.5px; line-height:1.35; }
.tile small { color:var(--muted); display:block; font-size:11.5px; }
dialog { border:1px solid var(--border); border-radius:var(--radius);
  background:var(--surface); color:var(--text); max-width:560px;
  width:calc(100% - 32px); padding:0; }
dialog::backdrop { background:rgba(0,0,0,.5); }
.dlg { padding:18px; }
.dlg img { width:100%; max-height:300px; object-fit:contain; border-radius:7px; background:var(--bg); }
.dlg h3 { margin:13px 0 3px; }
.dlg p { font-size:14px; }
.dlg .rotulo { font-size:11.5px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); margin-top:14px; }
.tags { display:flex; gap:5px; flex-wrap:wrap; margin-top:5px; }
.tags span { background:var(--bg); border:1px solid var(--border); border-radius:999px;
  padding:2px 9px; font-size:12px; }
.vazio { color:var(--muted); text-align:center; padding:40px 16px; font-size:14px; }
.erro { background:var(--warn-bg); border:1px solid var(--warn-border); color:var(--warn-text);
  padding:11px 13px; border-radius:var(--radius); margin-top:16px; font-size:14px; }
.uso { color:var(--muted); font-size:12px; margin-top:18px; font-variant-numeric:tabular-nums; }
.oculto { display:none !important; }
.girando { display:inline-block; width:13px; height:13px; border:2px solid currentColor;
  border-right-color:transparent; border-radius:50%; animation:gira .7s linear infinite;
  vertical-align:-2px; margin-right:7px; }
@keyframes gira { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
<header><h1>memegen</h1><span class="sub" id="status">carregando...</span></header>
<div id="aviso" class="aviso oculto"></div>
<nav>
  <button id="tab-gerar" aria-selected="true">Gerar</button>
  <button id="tab-navegar" aria-selected="false">Templates</button>
</nav>

<section id="painel-gerar">
  <textarea id="situacao" placeholder="Descreva a situacao. Pode ser um desabafo, um trecho de conversa, um bug, uma reuniao - qualquer coisa."></textarea>
  <div class="exemplos" id="exemplos"></div>
  <div class="linha">
    <button class="primario" id="gerar">Gerar memes</button>
    <label>sugestoes <select id="n"><option>2</option><option selected>3</option><option>4</option><option>5</option></select></label>
    <label>selecao <select id="modo">
      <option value="shortlist" selected>triagem (mais barato)</option>
      <option value="full">catalogo inteiro</option>
    </select></label>
    <label><input type="checkbox" id="nsfw" style="width:auto"> incluir nsfw</label>
  </div>
  <div id="erro" class="erro oculto"></div>
  <div id="barra" class="barra oculto">
    <span id="conta-resultados" class="meta"></span>
    <button class="leve" id="baixar-todos">Baixar todos</button>
  </div>
  <div id="resultados"></div>
  <div id="uso" class="uso"></div>
</section>

<section id="painel-navegar" class="oculto">
  <input type="text" id="busca" placeholder="Buscar por nome, keyword ou funcao...">
  <div class="meta" id="conta-busca" style="margin-top:8px"></div>
  <div class="grade" id="grade"></div>
  <div style="text-align:center;margin-top:18px"><button class="leve" id="mais">Carregar mais</button></div>
</section>
</div>
<dialog id="dlg"><div class="dlg" id="dlg-corpo"></div></dialog>
"""

SCRIPT = r"""<script>
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
}

/* ==================================================== RENDERIZACAO (canvas)

As coordenadas das caixas nao estao em pixels da imagem: estao num canvas de
640 px de largura e altura proporcional. Verificado nos 836 templates, nenhuma
caixa ultrapassa esses limites e 744 encostam exatamente em x=640. A conversao
para pixels e uma unica escala, derivada da largura REAL do arquivo -- o 9GAG
serve tudo ja redimensionado para o canvas, e o width/height do catalogo e do
original (779 dos 836 divergem).
==================================================================== */

const CANVAS_W = 640;

const FAMILIAS = {
  'impact': "Impact, 'Arial Black', Haettenschweiler, 'Franklin Gothic Bold', sans-serif",
  'arial': "Arial, Helvetica, sans-serif",
  'helvetica': "Helvetica, Arial, sans-serif",
  'comic sans ms': "'Comic Sans MS', 'Comic Sans', cursive",
};

function fonte(familia, tamanho) {
  return tamanho + "px " + (FAMILIAS[(familia || '').toLowerCase()] || FAMILIAS.impact);
}

/** Quebra o texto respeitando a largura. null se alguma palavra nao couber. */
function quebrar(ctx, texto, larguraMax) {
  const linhas = [];
  for (const paragrafo of String(texto).split('\n')) {
    const palavras = paragrafo.split(/\s+/).filter(Boolean);
    if (!palavras.length) { linhas.push(''); continue; }
    if (ctx.measureText(palavras[0]).width > larguraMax) return null;
    let atual = palavras[0];
    for (let i = 1; i < palavras.length; i++) {
      const p = palavras[i];
      const cand = atual + ' ' + p;
      if (ctx.measureText(cand).width <= larguraMax) {
        atual = cand;
      } else {
        linhas.push(atual);
        if (ctx.measureText(p).width > larguraMax) return null;
        atual = p;
      }
    }
    linhas.push(atual);
  }
  return linhas;
}

/** Maior corpo de fonte em que o texto quebrado cabe na caixa. */
function ajustar(ctx, texto, larguraMax, alturaMax, familia, inicial, auto) {
  const entrelinha = 1.15;
  const tenta = t => {
    ctx.font = fonte(familia, t);
    const linhas = quebrar(ctx, texto, larguraMax);
    if (!linhas) return null;
    const alturaLinha = t * entrelinha;
    if (alturaLinha * linhas.length > alturaMax) return null;
    return {linhas, tamanho: t, alturaLinha};
  };
  if (!auto) return tenta(inicial) || {linhas: [String(texto)], tamanho: inicial,
                                       alturaLinha: inicial * entrelinha};
  let lo = 4, hi = Math.max(8, Math.floor(alturaMax)), melhor = null;
  while (lo <= hi) {
    const meio = (lo + hi) >> 1;
    const r = tenta(meio);
    if (r) { melhor = r; lo = meio + 1; } else { hi = meio - 1; }
  }
  return melhor || {linhas: [String(texto)], tamanho: 4, alturaLinha: 5};
}

/** Compoe os textos sobre a imagem. Devolve o proprio canvas. */
function desenhar(canvas, tpl, textos, img) {
  const ctx = canvas.getContext('2d');
  canvas.width = img.naturalWidth;
  canvas.height = img.naturalHeight;
  ctx.drawImage(img, 0, 0);

  const escala = img.naturalWidth / CANVAS_W;

  for (const caixa of tpl.caixas) {
    let bruto = (textos || {})[caixa.id] || '';
    if (!bruto.trim()) continue;
    if (caixa.capitalize) bruto = bruto.toUpperCase();

    const bx = caixa.x * escala, by = caixa.y * escala;
    const bw = caixa.width * escala, bh = caixa.height * escala;

    const f = ajustar(ctx, bruto, bw, bh, caixa.fontFamily,
                      Math.max(1, Math.round((caixa.fontSize || 50) * escala)),
                      caixa.autoFontSize !== false);

    ctx.save();
    if (caixa.rotation) {
      // o 9GAG grava a rotacao em graus no sentido horario, que e o mesmo
      // sentido positivo do canvas
      ctx.translate(bx + bw / 2, by + bh / 2);
      ctx.rotate(caixa.rotation * Math.PI / 180);
      ctx.translate(-(bx + bw / 2), -(by + bh / 2));
    }

    ctx.font = fonte(caixa.fontFamily, f.tamanho);
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    ctx.miterLimit = 2;
    ctx.textAlign = caixa.textAlign === 'left' ? 'left'
                  : caixa.textAlign === 'right' ? 'right' : 'center';

    const alturaTotal = f.alturaLinha * f.linhas.length;
    const base = caixa.textBaseline;
    let y = base === 'top' ? by : base === 'bottom' ? by + bh - alturaTotal
                                                    : by + (bh - alturaTotal) / 2;
    y += f.alturaLinha / 2;

    const x = ctx.textAlign === 'left' ? bx
            : ctx.textAlign === 'right' ? bx + bw : bx + bw / 2;

    const sombra = caixa.outlineStyle === 'shadow';
    // lineWidth do canvas fica centrado no traco (metade para fora), entao vale
    // o dobro do raio que uma API de contorno externo usaria
    const contorno = Math.max(0, (caixa.strokeWidth || 0) * escala);

    for (const linha of f.linhas) {
      if (sombra) {
        const off = Math.max(1, Math.round(2 * escala));
        ctx.fillStyle = caixa.strokeColor || '#000';
        ctx.fillText(linha, x + off, y + off);
        ctx.fillStyle = caixa.color || '#fff';
        ctx.fillText(linha, x, y);
      } else {
        if (contorno > 0) {
          ctx.lineWidth = contorno;
          ctx.strokeStyle = caixa.strokeColor || '#000';
          ctx.strokeText(linha, x, y);
        }
        ctx.fillStyle = caixa.color || '#fff';
        ctx.fillText(linha, x, y);
      }
      y += f.alturaLinha;
    }
    ctx.restore();
  }
  return canvas;
}

function comoPng(canvas) {
  return new Promise((ok, falha) =>
    canvas.toBlob(b => b ? ok(b) : falha(new Error('conversao falhou')), 'image/png'));
}

function carregarImagem(src) {
  return new Promise((ok, falha) => {
    const img = new Image();
    img.onload = () => ok(img);
    img.onerror = () => falha(new Error('nao consegui carregar ' + src));
    img.src = src;
  });
}
</script>
"""

APP = r"""<script>
/* ============================================================ estado e abas */

async function carregarStatus() {
  try {
    const s = await api('/api/status');
    $('#status').textContent = s.templates + ' templates / ' + s.contratos + ' contratos';
    if (s.aviso) {
      $('#aviso').innerHTML = esc(s.aviso).replace(/`([^`]+)`/g, '<code>$1</code>');
      $('#aviso').classList.remove('oculto');
    }
    $('#gerar').disabled = !s.pronto;
  } catch (e) { $('#status').textContent = 'servidor indisponivel'; }
}

function aba(qual) {
  const g = qual === 'gerar';
  $('#tab-gerar').setAttribute('aria-selected', g);
  $('#tab-navegar').setAttribute('aria-selected', !g);
  $('#painel-gerar').classList.toggle('oculto', !g);
  $('#painel-navegar').classList.toggle('oculto', g);
  if (!g && !$('#grade').children.length) buscar(true);
}
$('#tab-gerar').onclick = () => aba('gerar');
$('#tab-navegar').onclick = () => aba('navegar');

const EXEMPLOS = [
  'passei a tarde otimizando uma query que roda uma vez por mes',
  'mandei o email e ate hoje ninguem respondeu',
  'troquei de framework de novo e o antigo funcionava bem',
  'reuniao que podia ter sido um email',
  'sabado a noite sem nada para fazer',
];
$('#exemplos').innerHTML = EXEMPLOS.map(e =>
  '<button data-t="' + esc(e) + '">' + esc(e) + '</button>').join('');
$('#exemplos').onclick = e => {
  const b = e.target.closest('button');
  if (b) { $('#situacao').value = b.dataset.t; $('#situacao').focus(); }
};

/* ========================================================== cartao de meme */

const podeCopiar = !!(navigator.clipboard && window.ClipboardItem && window.isSecureContext);
const podeCompartilhar = !!(navigator.share && navigator.canShare);

function slug(texto) {
  return String(texto).normalize('NFKD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-|-$/g, '').toLowerCase();
}

function cartao(s) {
  const el = document.createElement('div');
  el.className = 'card';
  el.dataset.template = s.id;
  el._tpl = s;

  const campos = s.caixas.map(b => {
    const v = (s.textos || {})[b.id] || '';
    const max = b.max_chars || 0;
    return '<div class="campo"><label>' +
      '<span class="papel">' + esc(b.papel || b.id) + '</span>' +
      '<span class="conta' + (max && v.length > max ? ' estourou' : '') + '">' +
      (max ? v.length + '/' + max : v.length) + '</span></label>' +
      '<input type="text" data-box="' + esc(b.id) + '" data-max="' + max +
      '" value="' + esc(v) + '"></div>';
  }).join('');

  el.innerHTML =
    '<h3>' + esc(s.nome) + '</h3>' +
    '<div class="meta">' + (s.ano ? s.ano + ' / ' : '') + '#' + (s.rank + 1) + ' em popularidade</div>' +
    (s.porque ? '<div class="porque">' + esc(s.porque) + '</div>' : '') +
    '<div class="duas"><canvas></canvas><div>' + campos +
    '<div class="linha" style="margin-top:14px">' +
      '<button class="leve compartilhar">Compartilhar</button>' +
      '<button class="leve copiar">Copiar</button>' +
      '<button class="leve baixar">Baixar</button>' +
      '<button class="leve historia">Historia do meme</button>' +
    '</div></div></div>';

  const canvas = el.querySelector('canvas');
  const redesenhar = () => {
    if (!el._img) return;
    desenhar(canvas, s, textosDo(el), el._img);
  };
  el._redesenhar = redesenhar;

  carregarImagem(s.img).then(img => { el._img = img; redesenhar(); })
    .catch(e => mostrarErro(e.message));

  el.querySelectorAll('input[data-box]').forEach(inp => {
    inp.oninput = () => {
      const max = Number(inp.dataset.max || 0);
      const conta = inp.closest('.campo').querySelector('.conta');
      conta.textContent = max ? inp.value.length + '/' + max : inp.value.length;
      conta.classList.toggle('estourou', max > 0 && inp.value.length > max);
      redesenhar();     // instantaneo: o desenho e local, sem ida ao servidor
    };
  });

  el.querySelector('.historia').onclick = () => abrirDetalhe(s.id);
  el.querySelector('.baixar').onclick = async () => {
    const png = await comoPng(canvas);
    const url = URL.createObjectURL(png);
    const a = document.createElement('a');
    a.href = url; a.download = (slug(s.nome) || 'meme') + '.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  };

  const btnC = el.querySelector('.copiar');
  if (!podeCopiar) {
    btnC.disabled = true;
    btnC.title = window.isSecureContext
      ? 'Seu navegador nao permite copiar imagem. Use Baixar.'
      : 'Copiar exige HTTPS. Suba com: memegen.py serve --host 0.0.0.0 --https';
  } else {
    btnC.onclick = async () => {
      const rotulo = btnC.textContent;
      try {
        // o clipboard so aceita image/png; JPEG e recusado no Chromium
        await navigator.clipboard.write([new ClipboardItem({'image/png': comoPng(canvas)})]);
        btnC.textContent = 'Copiado'; btnC.disabled = true;
        setTimeout(() => { btnC.textContent = rotulo; btnC.disabled = false; }, 1800);
      } catch (e) { mostrarErro('Nao consegui copiar: ' + e.message + '. Use Baixar.'); }
    };
  }

  const btnS = el.querySelector('.compartilhar');
  if (!podeCompartilhar) {
    btnS.disabled = true;
    btnS.title = window.isSecureContext
      ? 'Seu navegador nao tem compartilhamento de arquivo. Use Copiar ou Baixar.'
      : 'Compartilhar exige HTTPS. Suba com: memegen.py serve --host 0.0.0.0 --https';
  } else {
    btnS.onclick = async () => {
      try {
        // nao existe URL do WhatsApp que carregue imagem (wa.me aceita so ?text=);
        // a folha de compartilhamento do sistema e o unico caminho real
        const arq = new File([await comoPng(canvas)],
                             (slug(s.nome) || 'meme') + '.png', {type: 'image/png'});
        if (!navigator.canShare({files: [arq]})) throw new Error('este navegador nao compartilha imagem');
        await navigator.share({files: [arq]});
      } catch (e) {
        if (e.name === 'AbortError') return;   // o usuario fechou a folha
        mostrarErro('Nao consegui compartilhar: ' + e.message + '. Use Copiar.');
      }
    };
  }
  return el;
}

function textosDo(card) {
  const out = {};
  card.querySelectorAll('input[data-box]').forEach(i => out[i.dataset.box] = i.value);
  return out;
}

function mostrarErro(msg) {
  $('#erro').textContent = msg;
  $('#erro').classList.remove('oculto');
}

/* ================================================================== gerar */

$('#gerar').onclick = async () => {
  const situacao = $('#situacao').value.trim();
  if (!situacao) { $('#situacao').focus(); return; }
  const btn = $('#gerar');
  btn.disabled = true;
  btn.innerHTML = '<span class="girando"></span>Gerando...';
  $('#erro').classList.add('oculto');
  $('#barra').classList.add('oculto');
  $('#resultados').innerHTML = '';
  $('#uso').textContent = '';
  try {
    const r = await api('/api/suggest', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({situacao, n: Number($('#n').value),
                            modo: $('#modo').value, nsfw: $('#nsfw').checked}),
    });
    if (!r.sugestoes.length) {
      $('#resultados').innerHTML = '<div class="vazio">Nenhuma sugestao veio de volta.</div>';
    } else {
      r.sugestoes.forEach(s => $('#resultados').appendChild(cartao(s)));
      const n = r.sugestoes.length;
      $('#conta-resultados').textContent = n + ' meme' + (n === 1 ? '' : 's');
      $('#barra').classList.remove('oculto');
    }
    const u = r.uso || {}, t = u.triagem;
    $('#uso').textContent = 'modo ' + u.modo + ' / ' + u.candidatos + ' candidatos / ' +
      (t ? 'triagem ' + t.input + '+' + t.output + ' tok / ' : '') +
      'geracao ' + u.input + '+' + u.output + ' tok';
  } catch (e) { mostrarErro(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Gerar memes'; }
};

$('#situacao').onkeydown = e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') $('#gerar').click();
};

/* =========================================================== baixar todos */

$('#baixar-todos').onclick = async ev => {
  const btn = ev.currentTarget;
  const cards = [...document.querySelectorAll('#resultados .card')];
  if (!cards.length) return;
  const rotulo = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="girando"></span>Compactando...';
  try {
    // o desenho atual do canvas ja reflete as edicoes: e ele que vai no zip
    const itens = [];
    for (let i = 0; i < cards.length; i++) {
      const png = await comoPng(cards[i].querySelector('canvas'));
      const b64 = await new Promise(ok => {
        const fr = new FileReader();
        fr.onload = () => ok(String(fr.result).split(',')[1]);
        fr.readAsDataURL(png);
      });
      itens.push({nome: String(i + 1).padStart(2, '0') + '-' +
                        (slug(cards[i]._tpl.nome) || 'meme') + '.png', png: b64});
    }
    const r = await fetch('/api/zip', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({itens, situacao: $('#situacao').value.trim()}),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const cd = r.headers.get('Content-Disposition') || '';
    const nome = (cd.match(/filename="([^"]+)"/) || [])[1] || 'memegen.zip';
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = url; a.download = nome;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (e) { mostrarErro(e.message); }
  finally { btn.disabled = false; btn.textContent = rotulo; }
};

/* ================================================================ navegar */

let offset = 0, termo = '', total = 0;

async function buscar(zerar) {
  if (zerar) { offset = 0; $('#grade').innerHTML = ''; }
  try {
    const r = await api('/api/templates?q=' + encodeURIComponent(termo) +
                        '&offset=' + offset + '&limit=60');
    total = r.total;
    $('#conta-busca').textContent = r.total + ' template' + (r.total === 1 ? '' : 's');
    r.itens.forEach(t => {
      const b = document.createElement('button');
      b.className = 'tile';
      b.innerHTML = '<img src="' + esc(t.img) + '" loading="lazy" alt=""><div>' +
        esc(t.nome) + '<small>' + t.caixas.length + ' caixas' +
        (t.ano ? ' / ' + t.ano : '') + '</small></div>';
      b.onclick = () => abrirDetalhe(t.id);
      $('#grade').appendChild(b);
    });
    offset += r.itens.length;
    $('#mais').classList.toggle('oculto', offset >= total);
  } catch (e) { $('#conta-busca').textContent = e.message; }
}

let tBusca = null;
$('#busca').oninput = () => {
  clearTimeout(tBusca);
  tBusca = setTimeout(() => { termo = $('#busca').value.trim(); buscar(true); }, 250);
};
$('#mais').onclick = () => buscar(false);

/* ================================================================ detalhe */

async function abrirDetalhe(tid) {
  const d = $('#dlg');
  $('#dlg-corpo').innerHTML = '<p class="vazio">carregando...</p>';
  d.showModal();
  try {
    const t = await api('/api/templates/' + encodeURIComponent(tid));
    const c = t.contrato;
    $('#dlg-corpo').innerHTML =
      '<img src="' + esc(t.img) + '" alt="">' +
      '<h3>' + esc(t.nome) + '</h3>' +
      '<div class="meta">' + (t.ano ? t.ano + ' / ' : '') + '#' + (t.rank + 1) +
        ' em popularidade / ' + t.largura + 'x' + t.altura + '</div>' +
      (t.descricao ? '<div class="rotulo">Historia (9GAG)</div><p>' + esc(t.descricao) + '</p>' : '') +
      (c && c.funcao ? '<div class="rotulo">Para que serve</div><p>' + esc(c.funcao) + '</p>' : '') +
      (c && c.quando_usar && c.quando_usar.length ? '<div class="rotulo">Quando usar</div><div class="tags">' +
        c.quando_usar.map(x => '<span>' + esc(x) + '</span>').join('') + '</div>' : '') +
      '<div class="rotulo">Caixas de texto</div><div class="tags">' +
        t.caixas.map(b => '<span>' + esc(b.papel || b.id) + '</span>').join('') + '</div>' +
      (!c ? '<p class="meta" style="margin-top:14px">Sem contrato ainda - rode <code>enrich</code>.</p>' : '') +
      '<div style="margin-top:18px;text-align:right"><button class="leve" id="fechar">Fechar</button></div>';
    $('#fechar').onclick = () => d.close();
  } catch (e) {
    $('#dlg-corpo').innerHTML = '<p class="erro">' + esc(e.message) + '</p>';
  }
}
$('#dlg').onclick = e => { if (e.target.id === 'dlg') $('#dlg').close(); };

carregarStatus();
</script>
</body>
</html>
"""

HTML = PAGINA + SCRIPT + APP


# ------------------------------------------------------------- HTTPS (opcional)

# navigator.share e navigator.clipboard so funcionam em contexto seguro.
# localhost conta; http://192.168.0.10:8000 nao -- e e assim que o celular
# alcanca a maquina. Sem HTTPS, copiar e compartilhar morrem justamente no
# aparelho onde o WhatsApp esta.

def ips_locais():
    achados = set(["127.0.0.1"])
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))   # nao envia nada; so escolhe a interface
            achados.add(s.getsockname()[0])
        finally:
            s.close()
    except socket.error:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            achados.add(info[4][0])
    except socket.error:
        pass
    return sorted(achados)


def gerar_certificado(forcar=False):
    """Certificado autoassinado via openssl da maquina.

    Usa o binario em vez de uma biblioteca para nao acrescentar dependencia -
    openssl vem por padrao em Linux e macOS, e no Windows acompanha o Git.
    """
    if os.path.exists(CERT) and os.path.exists(CHAVE) and not forcar:
        return CERT, CHAVE
    if not shutil.which("openssl"):
        raise RuntimeError(
            "openssl nao encontrado - necessario para gerar o certificado. "
            "Instale-o, ou rode sem --https (copiar e compartilhar so "
            "funcionarao em localhost).")

    garantir_dirs()
    partes = ["DNS:localhost"]
    for ip in ips_locais():
        partes.append("IP:" + ip)
    conf = ("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=memegen local\n"
            "[ext]\nsubjectAltName=%s\nbasicConstraints=critical,CA:FALSE\n"
            % ",".join(partes))

    fd, caminho = tempfile.mkstemp(suffix=".cnf")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(conf)
        proc = subprocess.Popen(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "825", "-keyout", CHAVE, "-out", CERT, "-config", caminho],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # capture_output e 3.7+
        _, err = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("openssl falhou: %s"
                               % err.decode("utf-8", "replace")[:300])
    finally:
        try:
            os.unlink(caminho)                 # missing_ok e 3.8+
        except OSError:
            pass
    os.chmod(CHAVE, 0o600)
    return CERT, CHAVE


# ------------------------------------------------------------------- servidor

def _slug_arquivo(texto, limite=40):
    plano = unicodedata.normalize("NFKD", texto)
    plano = plano.encode("ascii", "ignore").decode("ascii")
    plano = re.sub(r"[^a-zA-Z0-9]+", "-", plano).strip("-").lower()
    return plano[:limite].strip("-")


NOME_SEGURO = re.compile(r"^[A-Za-z0-9_.-]+$")


class Estado(object):
    """Catalogo e contratos em memoria, relidos quando o arquivo muda."""

    def __init__(self):
        self.trava = threading.Lock()
        self._catalogo = None
        self._contratos = None
        self._mtimes = (None, None)

    def _mtime(self, caminho):
        try:
            return os.path.getmtime(caminho)
        except OSError:
            return None

    def ler(self):
        with self.trava:
            atual = (self._mtime(CATALOGO), self._mtime(CONTRATOS))
            if atual != self._mtimes or self._catalogo is None:
                self._catalogo = ler_json(CATALOGO, {})
                self._contratos = ler_json(CONTRATOS, {})
                self._mtimes = atual
            return self._catalogo, self._contratos


ESTADO_MEM = Estado()


def _tamanho_real(tid, t):
    """Dimensao da imagem servida, que nao e a declarada no catalogo.

    O 9GAG serve tudo ja normalizado no canvas de 640 px; width/height no
    catalogo sao as do original. Medido, 779 dos 836 divergem. Le so o
    cabecalho do JPEG, sem decodificar a imagem.
    """
    caminho = os.path.join(IMAGENS, tid + ".jpg")
    try:
        with open(caminho, "rb") as f:
            dados = f.read(2)
            if dados != b"\xff\xd8":
                return t["width"], t["height"]
            while True:
                b = f.read(1)
                while b and b != b"\xff":
                    b = f.read(1)
                marcador = f.read(1)
                while marcador == b"\xff":
                    marcador = f.read(1)
                if not marcador:
                    break
                m = marcador[0]
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    f.read(3)
                    alt = int.from_bytes(f.read(2), "big")
                    larg = int.from_bytes(f.read(2), "big")
                    return larg, alt
                tam = int.from_bytes(f.read(2), "big")
                if tam < 2:
                    break
                f.seek(tam - 2, 1)
    except (OSError, IndexError, ValueError):
        pass
    return t["width"], t["height"]


def _resumo(tid, t, contratos):
    c = contratos.get(tid) or {}
    slots = dict((s["box_id"], s) for s in c.get("slots", []))
    largura, altura = _tamanho_real(tid, t)
    caixas = []
    for b in t["textBoxes"]:
        s = slots.get(b["id"], {})
        caixa = dict((k, b.get(k)) for k in
                     ("id", "x", "y", "width", "height", "fontSize", "fontFamily",
                      "color", "textAlign", "textBaseline", "capitalize",
                      "outlineStyle", "strokeWidth", "strokeColor",
                      "autoFontSize", "rotation"))
        caixa["papel"] = s.get("papel")
        caixa["max_chars"] = s.get("max_chars")
        caixas.append(caixa)
    return {
        "id": tid, "nome": t["name"],
        "ano": t.get("year") if t.get("year", 0) > 0 else None,
        "rank": t.get("_rank"), "descricao": t.get("description"),
        "largura": largura, "altura": altura, "caixas": caixas,
        "contrato": ({"funcao": c.get("funcao"),
                      "quando_usar": c.get("quando_usar", []),
                      "tom": c.get("tom", [])} if c else None),
        "img": "/template/" + tid + ".jpg",
    }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "memegen/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):
        pass                                  # silencioso; erros vao pelo responder

    # -------------------------------------------------------------- utilidades

    def _responder(self, codigo, corpo, tipo="application/json; charset=utf-8",
                   extras=None):
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(corpo)
        except socket.error:
            pass                              # navegador fechou a conexao

    def _json(self, codigo, dados, extras=None):
        self._responder(codigo, json.dumps(dados, ensure_ascii=False), extras=extras)

    def _erro(self, codigo, mensagem):
        self._json(codigo, {"detail": mensagem})

    def _corpo(self, limite=64 * 1024 * 1024):
        tam = int(self.headers.get("Content-Length") or 0)
        if tam > limite:
            raise ValueError("corpo grande demais")
        if not tam:
            return {}
        return json.loads(self.rfile.read(tam).decode("utf-8"))

    def _arquivo(self, caminho, tipo, extras=None):
        try:
            with open(caminho, "rb") as f:
                dados = f.read()
        except OSError:
            return self._erro(404, "arquivo nao encontrado")
        self._responder(200, dados, tipo, extras)

    # ------------------------------------------------------------------- rotas

    def do_GET(self):
        partes = urlparse(self.path)
        rota = unquote(partes.path)
        consulta = parse_qs(partes.query)
        try:
            if rota == "/":
                return self._responder(200, HTML, "text/html; charset=utf-8")
            if rota == "/api/status":
                return self._json(200, self.rota_status())
            if rota == "/api/templates":
                return self._json(200, self.rota_listar(consulta))
            if rota.startswith("/api/templates/"):
                return self._json(200, self.rota_detalhe(rota.split("/", 3)[3]))
            if rota.startswith("/template/"):
                return self.rota_imagem(rota.split("/", 2)[2])
            return self._erro(404, "rota desconhecida")
        except KeyError as e:
            self._erro(404, "nao encontrado: %s" % e)
        except RuntimeError as e:
            self._erro(503, str(e))
        except Exception as e:                # pragma: no cover
            self._erro(500, "%s: %s" % (type(e).__name__, e))

    def do_POST(self):
        rota = unquote(urlparse(self.path).path)
        try:
            corpo = self._corpo()
            if rota == "/api/suggest":
                return self._json(200, self.rota_sugerir(corpo))
            if rota == "/api/zip":
                return self.rota_zip(corpo)
            return self._erro(404, "rota desconhecida")
        except ValueError as e:
            self._erro(400, str(e))
        except RuntimeError as e:
            self._erro(503, str(e))
        except Exception as e:                # pragma: no cover
            self._erro(500, "%s: %s" % (type(e).__name__, e))

    # ------------------------------------------------------------- implementacao

    def rota_status(self):
        catalogo, contratos = ESTADO_MEM.ler()
        estado = ler_json(ESTADO, {})
        pendentes = contratos_pendentes(catalogo, contratos) if catalogo else []
        aviso = None
        if not catalogo:
            aviso = "Catalogo vazio. Rode `sync`."
        elif not contratos:
            aviso = "Nenhum contrato. Rode `enrich` - sem isso a escolha nao funciona."
        elif pendentes:
            aviso = "%d templates sem contrato atualizado. Rode `enrich`." % len(pendentes)
        return {"pronto": bool(catalogo and contratos), "aviso": aviso,
                "templates": len(catalogo), "contratos": len(contratos),
                "pendentes": len(pendentes),
                "sincronizado": estado.get("sincronizado"),
                "bundle": estado.get("templates")}

    def rota_listar(self, consulta):
        catalogo, contratos = ESTADO_MEM.ler()
        if not catalogo:
            raise RuntimeError("catalogo vazio - rode `sync`")
        q = (consulta.get("q", [""])[0] or "").lower()
        limite = max(1, min(200, int(consulta.get("limit", ["60"])[0])))
        salto = max(0, int(consulta.get("offset", ["0"])[0]))

        itens = sorted(catalogo.items(), key=lambda kv: kv[1].get("_rank", 9999))
        if q:
            filtrados = []
            for tid, t in itens:
                c = contratos.get(tid) or {}
                alvo = " ".join([t["name"], " ".join(t.get("keywords", [])),
                                 c.get("funcao", ""),
                                 " ".join(c.get("quando_usar", []))]).lower()
                if q in alvo:
                    filtrados.append((tid, t))
            itens = filtrados
        pagina = itens[salto:salto + limite]
        return {"total": len(itens),
                "itens": [_resumo(tid, t, contratos) for tid, t in pagina]}

    def rota_detalhe(self, tid):
        catalogo, contratos = ESTADO_MEM.ler()
        if tid not in catalogo:
            raise KeyError(tid)
        return _resumo(tid, catalogo[tid], contratos)

    def rota_imagem(self, nome):
        tid = nome[:-4] if nome.endswith(".jpg") else nome
        if not NOME_SEGURO.match(tid):
            return self._erro(400, "nome invalido")
        catalogo, _ = ESTADO_MEM.ler()
        if tid not in catalogo:
            return self._erro(404, "template desconhecido")
        destino = os.path.join(IMAGENS, tid + ".jpg")
        if not os.path.exists(destino):
            baixar_imagem(catalogo[tid]["url"], destino)
        return self._arquivo(destino, "image/jpeg",
                             {"Cache-Control": "public, max-age=86400"})

    def rota_sugerir(self, corpo):
        catalogo, contratos = ESTADO_MEM.ler()
        situacao = (corpo.get("situacao") or "").strip()
        if not situacao:
            raise ValueError("descreva a situacao")
        sugestoes, uso = sugerir(
            situacao, catalogo, contratos,
            n=int(corpo.get("n", 3)), modo=corpo.get("modo", "shortlist"),
            nsfw=bool(corpo.get("nsfw")))
        saida = []
        for s in sugestoes:
            tid = s.get("template_id")
            if tid not in catalogo:
                continue
            resumo = _resumo(tid, catalogo[tid], contratos)
            resumo["porque"] = s.get("porque")
            resumo["textos"] = dict((x["box_id"], x["texto"])
                                    for x in s.get("textos", []))
            saida.append(resumo)
        return {"sugestoes": saida, "uso": uso}

    def rota_zip(self, corpo):
        """Empacota os PNGs que o navegador desenhou.

        Os bytes vem do canvas, entao refletem o estado atual dos campos - as
        edicoes vao junto, sem o servidor precisar redesenhar nada.
        """
        itens = corpo.get("itens") or []
        if not itens:
            raise ValueError("nada para baixar")
        buf = io.BytesIO()
        usados = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for i, item in enumerate(itens, 1):
                bruto = item.get("png")
                if not bruto:
                    continue
                nome = item.get("nome") or ("%02d-meme.png" % i)
                nome = os.path.basename(nome)
                if not NOME_SEGURO.match(nome):
                    nome = "%02d-meme.png" % i
                while nome in usados:
                    nome = "%02d-meme-%d.png" % (i, len(usados))
                usados.add(nome)
                z.writestr(nome, base64.b64decode(bruto))
        if not usados:
            raise ValueError("nenhuma imagem valida")
        miolo = _slug_arquivo(corpo.get("situacao") or "") or "memes"
        arquivo = "memegen-%s-%s.zip" % (miolo, datetime.now().strftime("%Y%m%d-%H%M"))
        self._responder(200, buf.getvalue(), "application/zip",
                        {"Content-Disposition": 'attachment; filename="%s"' % arquivo})


class Servidor(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # ThreadingHTTPServer so existe a partir do 3.7
    daemon_threads = True
    allow_reuse_address = True


def servir(host="127.0.0.1", porta=8000, https=False):
    garantir_dirs()
    httpd = Servidor((host, porta), Handler)
    esquema = "http"
    if https:
        cert, chave = gerar_certificado()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)   # ssl.wrap_socket saiu no 3.12
        ctx.load_cert_chain(cert, chave)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        esquema = "https"

    visivel = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print("memegen em %s://%s:%d" % (esquema, visivel, porta))
    if host == "0.0.0.0":
        for ip in ips_locais():
            if ip != "127.0.0.1":
                print("  na rede: %s://%s:%d" % (esquema, ip, porta))
        if not https:
            print("  aviso: sem --https, copiar e compartilhar nao funcionam fora")
            print("         de localhost - o navegador exige contexto seguro.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrando")
    finally:
        httpd.server_close()


# ------------------------------------------------------------------------ CLI

def cmd_sync(args):
    garantir_dirs()
    estado = ler_json(ESTADO, {})
    ponteiro = ponteiro_9gag()
    print("build do 9GAG: " + ponteiro["manifest"])
    print("bundle atual : " + ponteiro["templates"])

    if estado.get("templates") == ponteiro["templates"] and not args.force:
        print("nada mudou desde %s (%s templates). Use --force para rebaixar."
              % (estado.get("sincronizado", "?"), estado.get("total", "?")))
        return 0
    if estado.get("templates"):
        print("bundle anterior: %s -> mudou, baixando" % estado["templates"])

    _, bruto = baixar_catalogo(ponteiro)
    novo = indexar(bruto)
    d = diferenca(ler_json(CATALOGO, {}), novo)
    print("\ndiff: %d novos, %d alterados, %d removidos, %d iguais"
          % (len(d["novos"]), len(d["alterados"]), len(d["removidos"]),
             len(d["iguais"])))
    for tid in d["novos"][:20]:
        print("  + %s (%s)" % (tid, novo[tid]["name"]))
    if len(d["novos"]) > 20:
        print("  + ... e mais %d" % (len(d["novos"]) - 20))
    for tid in d["alterados"][:20]:
        print("  ~ %s (%s)" % (tid, novo[tid]["name"]))
    for tid in d["removidos"][:20]:
        print("  - %s" % tid)

    gravar_json(CATALOGO, novo)
    gravar_json(ESTADO, {"manifest": ponteiro["manifest"],
                         "templates": ponteiro["templates"],
                         "sincronizado": agora(), "total": len(novo)})
    pendentes = contratos_pendentes(novo, ler_json(CONTRATOS, {}))
    if pendentes:
        print("\n%d templates sem contrato atualizado." % len(pendentes))
        print("  rode: %s enrich" % os.path.basename(sys.argv[0]))
    return 0


def cmd_enrich(args):
    garantir_dirs()
    catalogo = ler_json(CATALOGO, {})
    if not catalogo:
        print("catalogo vazio - rode `sync` primeiro", file=sys.stderr)
        return 1
    if args.collect:
        coletar_lote(args.collect, catalogo)
        return 0

    pendentes = contratos_pendentes(catalogo, ler_json(CONTRATOS, {}))
    if args.only:
        pendentes = [t for t in args.only if t in catalogo]
    if args.limit:
        pendentes = pendentes[:args.limit]
    if not pendentes:
        print("todos os contratos estao em dia")
        return 0

    qual = provedor()
    print("%d templates a enriquecer com %s (%s)"
          % (len(pendentes), args.model or modelo_padrao("enriquecer"), qual))
    print("baixando imagens faltantes...")
    falhas = garantir_imagens(catalogo, pendentes)
    pendentes = [t for t in pendentes if t not in falhas]

    if args.dry_run:
        print("[dry-run] enviaria %d requisicoes" % len(pendentes))
        if pendentes:
            print("\nexemplo de prompt:\n")
            print(prompt_enriquecimento(catalogo[pendentes[0]]))
        return 0

    if qual == "gemini":
        # sem API de lote: sequencial, com pausa entre chamadas para caber na
        # cota gratuita, e gravando a cada template para poder retomar
        enriquecer_sequencial(catalogo, pendentes, args.model, pausa=args.pausa)
        return 0

    lote = enviar_lote(catalogo, pendentes, args.model)
    if args.wait:
        coletar_lote(lote, catalogo)
    else:
        print("\nacompanhe com: %s enrich --collect %s"
              % (os.path.basename(sys.argv[0]), lote))
    return 0


def cmd_make(args):
    """Sugere memes na linha de comando.

    Nao produz imagem: a renderizacao acontece em canvas, no navegador. Para
    imagem, use `serve`. O que sai aqui e a escolha e o texto de cada caixa,
    que e a parte que depende do modelo.
    """
    catalogo = ler_json(CATALOGO, {})
    contratos = ler_json(CONTRATOS, {})
    sugestoes, uso = sugerir(args.situacao, catalogo, contratos, n=args.n,
                             modelo=args.model, modo=args.modo, nsfw=args.nsfw)
    for i, s in enumerate(sugestoes, 1):
        t = catalogo.get(s.get("template_id"))
        if not t:
            continue
        print("\n%d. %s  (%s)" % (i, t["name"], s["template_id"]))
        print("   %s" % s.get("porque", ""))
        papeis = dict((x["box_id"], x["papel"])
                      for x in (contratos.get(s["template_id"], {}).get("slots", [])))
        for x in s.get("textos", []):
            print("   [%s] %s" % (papeis.get(x["box_id"], x["box_id"]), x["texto"]))
    t = uso.get("triagem")
    print("\nmodo %s / %d candidatos / %stotal %d+%d tok"
          % (uso["modo"], uso["candidatos"],
             ("triagem %d+%d tok / " % (t["input"], t["output"])) if t else "",
             uso["input"], uso["output"]))
    print("para ver as imagens: %s serve" % os.path.basename(sys.argv[0]))
    return 0


def cmd_status(args):
    catalogo = ler_json(CATALOGO, {})
    contratos = ler_json(CONTRATOS, {})
    estado = ler_json(ESTADO, {})
    pendentes = contratos_pendentes(catalogo, contratos) if catalogo else []
    imagens = 0
    if os.path.isdir(IMAGENS):
        imagens = len([n for n in os.listdir(IMAGENS) if n.endswith(".jpg")])
    print("python      : %s" % ".".join(str(n) for n in sys.version_info[:3]))
    print("catalogo    : %d templates" % len(catalogo))
    print("contratos   : %d (%d pendentes)" % (len(contratos), len(pendentes)))
    print("imagens     : %d em disco" % imagens)
    print("bundle      : %s" % estado.get("templates", "-"))
    print("sincronizado: %s" % estado.get("sincronizado", "nunca"))
    qual = provedor()
    print("provedor    : %s" % qual)
    if qual == "anthropic":
        print("  ANTHROPIC_API_KEY: %s"
              % ("definida" if os.environ.get("ANTHROPIC_API_KEY") else "AUSENTE"))
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        print("  workspace        : %s"
              % (ws if ws else "nao definido (so preciso para chave de organizacao)"))
    else:
        print("  GEMINI_API_KEY   : %s"
              % ("definida" if os.environ.get("GEMINI_API_KEY") else "AUSENTE"))
    for papel in ("enriquecer", "triar", "gerar"):
        print("  modelo %-10s: %s" % (papel, modelo_padrao(papel)))
    return 0


def cmd_audit(args):
    """Lista os contratos mais arriscados para revisao manual."""
    catalogo = ler_json(CATALOGO, {})
    contratos = ler_json(CONTRATOS, {})
    riscos = []
    for tid, t in catalogo.items():
        c = contratos.get(tid)
        if not c:
            continue
        n = len(t["textBoxes"])
        sem_guia = not re.search(r"used to|it'?s used|to use it|label the",
                                 t.get("description", ""), re.I)
        pontos = (n - 2) * 2 + (1 if sem_guia else 0)
        if pontos > 0:
            riscos.append((pontos, tid, t["name"], n, c))
    riscos.sort(key=lambda r: -r[0])
    print("%d contratos merecem olhada; mostrando %d\n" % (len(riscos), args.limit))
    for pontos, tid, nome, n, c in riscos[:args.limit]:
        print("[risco %d] %s (%s) - %d caixas" % (pontos, nome, tid, n))
        print("  funcao: %s" % c["funcao"])
        for s in c.get("slots", []):
            print("    %s: %s" % (s["box_id"], s["papel"]))
        print("")
    return 0


def cmd_serve(args):
    servir(host=args.host, porta=args.port, https=args.https)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        description="Gerador de memes com IA sobre o catalogo do 9GAG. "
                    "Arquivo unico, so biblioteca padrao.")
    p.add_argument("--version", action="version", version="memegen " + __version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", help="sincroniza o catalogo do 9GAG")
    s.add_argument("--force", action="store_true", help="rebaixa mesmo sem mudanca")
    s.set_defaults(func=cmd_sync)

    e = sub.add_parser("enrich", help="gera contratos de uso para templates novos")
    e.add_argument("--collect", metavar="LOTE", help="coleta um lote ja enviado")
    e.add_argument("--wait", action="store_true", help="aguarda o lote terminar")
    e.add_argument("--limit", type=int, help="processa no maximo N templates")
    e.add_argument("--only", nargs="+", metavar="ID", help="enriquece ids especificos")
    e.add_argument("--model", help="depende do provedor; veja `status`")
    e.add_argument("--pausa", type=float, default=1.0,
                   help="segundos entre chamadas no modo sequencial (Gemini)")
    e.add_argument("--dry-run", action="store_true", help="mostra o que faria")
    e.set_defaults(func=cmd_enrich)

    m = sub.add_parser("make", help="sugere memes (texto; imagem so em `serve`)")
    m.add_argument("situacao")
    m.add_argument("-n", type=int, default=3)
    m.add_argument("--model", help="depende do provedor; veja `status`")
    m.add_argument("--modo", choices=["shortlist", "full"], default="shortlist")
    m.add_argument("--nsfw", action="store_true")
    m.set_defaults(func=cmd_make)

    w = sub.add_parser("serve", help="abre a interface web local")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--https", action="store_true",
                   help="HTTPS com certificado autoassinado; necessario para "
                        "copiar e compartilhar fora de localhost (ex.: do celular)")
    w.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="estado local")
    st.set_defaults(func=cmd_status)

    a = sub.add_parser("audit", help="contratos que merecem revisao manual")
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(func=cmd_audit)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):       # required=True em subparsers e 3.7+
        p.print_help()
        return 1
    try:
        return args.func(args)
    except RuntimeError as e:
        print("erro: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
