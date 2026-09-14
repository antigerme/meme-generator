"""Pipeline completo, com tudo real menos a chamada ao modelo.

Percorre catálogo -> contratos -> índice vetorial -> busca -> geração -> imagem
em disco. Só a chamada à API é falsa; o índice é construído de verdade, a busca
roda de verdade e a imagem é renderizada de verdade.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from memegen import catalog as C
from memegen import config, generate
from memegen.render import render
from tests.contratos_ouro import OURO
from tests.fake_api import FakeAnthropic

CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"
pytestmark = pytest.mark.skipif(not CATALOG.exists(), reason="rode `memegen sync`")


@pytest.fixture(scope="module")
def catalog():
    todo = json.loads(CATALOG.read_text(encoding="utf-8"))
    return {i: todo[i] for i in OURO if i in todo}


@pytest.fixture(scope="module")
def indice(catalog, tmp_path_factory):
    """Índice vetorial real sobre os contratos-ouro."""
    st = pytest.importorskip("sentence_transformers")  # noqa: F841
    from memegen import retrieve

    d = tmp_path_factory.mktemp("idx")
    orig_path, orig_meta = retrieve.INDEX_PATH, retrieve.INDEX_META
    retrieve.INDEX_PATH = d / "index.npz"
    retrieve.INDEX_META = d / "index.json"
    retrieve.build(OURO)
    yield retrieve
    retrieve.INDEX_PATH, retrieve.INDEX_META = orig_path, orig_meta


def test_indice_cobre_todos_os_contratos(indice):
    assert not indice.is_stale(OURO)
    assert indice.is_stale({**OURO, "inexistente": {}})


def test_busca_devolve_ids_do_catalogo(indice, catalog):
    ids = indice.search("preciso escolher entre duas coisas boas", k=5)
    assert ids and all(i in OURO for i in ids)
    assert len(ids) == 5


def test_documento_indexado_usa_funcao_nao_aparencia(indice):
    doc = indice._document(OURO["distracted_boyfriend"])
    assert "tentado por algo novo" in doc     # a função
    assert "casaco" not in doc.lower()        # a aparência não entra
    assert "quem se distrai" in doc           # os papéis dos slots entram


def test_pipeline_completo_gera_imagem(catalog, indice, monkeypatch, tmp_path):
    tid = "drake_hotline_bling"
    if not (config.TEMPLATE_IMAGES / f"{tid}.jpg").exists():
        pytest.skip("imagens não baixadas")

    resposta = {"sugestoes": [{
        "template_id": tid,
        "porque": "a situação é uma preferência entre duas opções",
        "textos": [{"box_id": "text-0", "texto": "Escrever contrato à mão"},
                   {"box_id": "text-1", "texto": "Enriquecer por batch"}],
    }]}
    fake = FakeAnthropic(responses=[resposta])
    monkeypatch.setattr(generate.anthropic, "Anthropic", lambda: fake)

    sug, uso = generate.suggest(
        "prefiro automatizar a escrever na mão",
        n=1, catalog=catalog, contracts=OURO, modo="vetorial", k=5,
    )
    assert uso["modo"] == "vetorial"
    assert uso["triagem"] is None          # busca vetorial não gasta token
    assert 0 < uso["candidatos"] <= 5      # a busca estreitou o catálogo

    s = sug[0]
    textos = {x["box_id"]: x["texto"] for x in s["textos"]}
    out = tmp_path / "meme.jpg"
    img = render(catalog[s["template_id"]], textos,
                 config.TEMPLATE_IMAGES / f"{s['template_id']}.jpg", out)

    assert out.exists() and out.stat().st_size > 1000
    original = Image.open(config.TEMPLATE_IMAGES / f"{tid}.jpg")
    assert img.size == original.size
    # a imagem precisa ter mudado: texto foi de fato pintado
    assert img.tobytes() != original.convert("RGB").tobytes()


def test_contratos_ouro_batem_com_o_catalogo(catalog):
    """Invariante que o enriquecimento precisa respeitar: um slot por caixa,
    com o mesmo box_id e na mesma ordem."""
    for tid, c in OURO.items():
        caixas = [b["id"] for b in catalog[tid]["textBoxes"]]
        slots = [s["box_id"] for s in c["slots"]]
        assert slots == caixas, f"{tid}: slots {slots} != caixas {caixas}"


def test_todo_slot_tem_papel_e_limite(catalog):
    for tid, c in OURO.items():
        for s in c["slots"]:
            assert s["papel"].strip(), f"{tid}/{s['box_id']} sem papel"
            assert 10 <= s["max_chars"] <= 200, f"{tid}/{s['box_id']} limite irreal"
