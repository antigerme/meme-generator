"""O renderizador precisa posicionar o texto dentro da caixa declarada."""

import json
from pathlib import Path

import pytest

from memegen import config
from memegen.render import _fit, render
from PIL import Image, ImageDraw

CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"


@pytest.fixture(scope="module")
def catalog():
    if not CATALOG.exists():
        pytest.skip("rode `memegen sync` primeiro")
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_fit_respeita_a_caixa():
    img = Image.new("RGB", (400, 200))
    d = ImageDraw.Draw(img)
    f = _fit("um texto razoavelmente longo que precisa quebrar",
             300, 100, "impact", 50, True, d)
    assert f.line_height * len(f.lines) <= 100
    for line in f.lines:
        assert d.textlength(line, font=f.font) <= 300


def test_fit_encolhe_texto_grande():
    img = Image.new("RGB", (400, 200))
    d = ImageDraw.Draw(img)
    curto = _fit("oi", 300, 100, "impact", 50, True, d)
    longo = _fit("oi " * 60, 300, 100, "impact", 50, True, d)
    assert longo.font.size < curto.font.size


def test_render_preserva_dimensoes(catalog, tmp_path):
    tid = "drake_hotline_bling"
    img_path = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    if not img_path.exists():
        pytest.skip("imagem do template não baixada")
    original = Image.open(img_path)
    out = render(catalog[tid], ["primeiro", "segundo"], img_path, tmp_path / "o.jpg")
    assert out.size == original.size
    assert (tmp_path / "o.jpg").exists()


def test_render_aceita_dict_e_lista(catalog):
    tid = "drake_hotline_bling"
    img_path = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    if not img_path.exists():
        pytest.skip("imagem do template não baixada")
    t = catalog[tid]
    por_lista = render(t, ["a", "b"], img_path)
    por_dict = render(t, {"text-0": "a", "text-1": "b"}, img_path)
    assert por_lista.tobytes() == por_dict.tobytes()


def test_render_ignora_caixa_vazia(catalog):
    tid = "drake_hotline_bling"
    img_path = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    if not img_path.exists():
        pytest.skip("imagem do template não baixada")
    t = catalog[tid]
    vazio = render(t, {"text-0": "", "text-1": ""}, img_path)
    limpo = Image.open(img_path).convert("RGB")
    assert vazio.tobytes() == limpo.tobytes()
