"""Sincronização com o catálogo de templates do 9GAG.

O gerador do 9GAG é totalmente estático: não há API. O catálogo vive dentro de
um bundle JavaScript com hash de conteúdo no nome, descoberto por uma cadeia de
três passos:

    /meme-generator/            (HTML, ~8 KB)   -> assets/manifest-<build>.js
    /assets/manifest-<build>.js (~4 KB)         -> assets/meme-templates-<hash>.js
    /assets/meme-templates-<hash>.js (~1.4 MB)  -> o catálogo em si

Como o nome do bundle é derivado do conteúdo, os dois primeiros passos (12 KB)
bastam para detectar se houve mudança. O bundle grande só é baixado quando o
hash muda de fato.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

BASE = "https://meme.9gag.com"
PAGE = f"{BASE}/meme-generator/"

_MANIFEST_RE = re.compile(r"assets/manifest-[A-Za-z0-9_-]+\.js")
_TEMPLATES_RE = re.compile(r"meme-templates-[A-Za-z0-9_-]+\.js")
_CATALOG_RE = re.compile(r"JSON\.parse\(`")

USER_AGENT = "memegen/0.1 (personal project; syncs public meme template catalog)"


@dataclass(frozen=True)
class SourcePointer:
    """Para onde o build atual do 9GAG aponta."""

    manifest_asset: str   # ex.: "assets/manifest-8fb5c86c.js"
    templates_asset: str  # ex.: "meme-templates-9ub_3wcz.js"

    @property
    def templates_url(self) -> str:
        return f"{BASE}/assets/{self.templates_asset}"


def _client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def fetch_pointer(client: httpx.Client | None = None) -> SourcePointer:
    """Resolve o build atual gastando ~12 KB. Não baixa o catálogo."""
    owned = client is None
    client = client or _client()
    try:
        html = client.get(PAGE).raise_for_status().text
        m = _MANIFEST_RE.search(html)
        if not m:
            raise LookupError(
                "não encontrei o manifest no HTML do 9GAG — o layout do build mudou"
            )
        manifest_asset = m.group(0)

        manifest = client.get(f"{BASE}/{manifest_asset}").raise_for_status().text
        t = _TEMPLATES_RE.search(manifest)
        if not t:
            raise LookupError(
                "o manifest não referencia mais um bundle meme-templates-*.js"
            )
        return SourcePointer(manifest_asset=manifest_asset, templates_asset=t.group(0))
    finally:
        if owned:
            client.close()


def _unescape_template_literal(raw: str) -> str:
    """Desfaz o escape de template literal do JS (\\\\, \\`, \\$)."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw) and raw[i + 1] in "\\`$":
            out.append(raw[i + 1])
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_catalog(bundle_js: str) -> list[dict]:
    """Extrai o array de templates de dentro do bundle."""
    m = _CATALOG_RE.search(bundle_js)
    if not m:
        raise LookupError(
            "não achei o JSON.parse(`...`) no bundle — o empacotamento mudou"
        )
    start = m.end()
    j = start
    while True:
        j = bundle_js.index("`", j)
        if bundle_js[j - 1] != "\\":
            break
        j += 1
    data = json.loads(_unescape_template_literal(bundle_js[start:j]))
    if not isinstance(data, list) or not data:
        raise LookupError("o bloco JSON encontrado não é uma lista de templates")
    return data


def fetch_catalog(
    pointer: SourcePointer | None = None, client: httpx.Client | None = None
) -> tuple[SourcePointer, list[dict]]:
    """Baixa e decodifica o catálogo completo (~1.4 MB)."""
    owned = client is None
    client = client or _client(timeout=120.0)
    try:
        pointer = pointer or fetch_pointer(client)
        bundle = client.get(pointer.templates_url).raise_for_status().text
        return pointer, parse_catalog(bundle)
    finally:
        if owned:
            client.close()


def download_image(asset_path: str, dest, client: httpx.Client | None = None) -> None:
    """Baixa uma imagem de template (ex.: '/templates/drake_hotline_bling.jpg')."""
    owned = client is None
    client = client or _client(timeout=60.0)
    try:
        r = client.get(f"{BASE}{asset_path}").raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
    finally:
        if owned:
            client.close()
