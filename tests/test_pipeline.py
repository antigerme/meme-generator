"""Pipeline de API exercitado com cliente falso.

Cobre o que é determinístico: formato das requisições, parsing das respostas,
caminhos de erro e seleção de modo. Não cobre a qualidade do que o modelo
escreve — isso só a API real responde.
"""

import json
from pathlib import Path

import pytest

from memegen import catalog as C
from memegen import config, enrich, generate
from tests.fake_api import FakeAnthropic

CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"
pytestmark = pytest.mark.skipif(not CATALOG.exists(), reason="rode `memegen sync`")

IDS = ["drake_hotline_bling", "distracted_boyfriend"]


@pytest.fixture
def catalog():
    todo = json.loads(CATALOG.read_text(encoding="utf-8"))
    return {i: todo[i] for i in IDS if i in todo}


def contrato(template):
    return {
        "funcao": "Contrastar duas opções",
        "quando_usar": ["preferência", "comparação"],
        "tom": ["sarcástico"],
        "slots": [
            {"box_id": b["id"], "papel": f"papel de {b['id']}",
             "exemplo": "exemplo", "max_chars": 60}
            for b in template["textBoxes"]
        ],
        "nsfw": False,
    }


@pytest.fixture
def contracts(catalog):
    return {tid: contrato(t) for tid, t in catalog.items()}


# ---------------------------------------------------------------- enrich

def test_submit_monta_requisicoes_validas(catalog, monkeypatch):
    for tid in catalog:
        if not (config.TEMPLATE_IMAGES / f"{tid}.jpg").exists():
            pytest.skip("imagens não baixadas")

    fake = FakeAnthropic()
    monkeypatch.setattr(enrich.anthropic, "Anthropic", lambda: fake)
    enrich.submit(catalog, list(catalog), model="claude-sonnet-5")

    assert len(fake.batch_requests) == len(catalog)
    for req in fake.batch_requests:
        assert req["custom_id"] in catalog
        p = req["params"]
        assert p["model"] == "claude-sonnet-5"
        assert p["max_tokens"] > 0
        assert p["system"]
        # a imagem precisa vir ANTES do texto
        blocos = p["messages"][0]["content"]
        assert blocos[0]["type"] == "image"
        assert blocos[0]["source"]["media_type"] == "image/jpeg"
        assert blocos[0]["source"]["data"]
        assert blocos[1]["type"] == "text"
        # o schema precisa estar presente e fechado
        fmt = p["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["additionalProperties"] is False
        assert set(fmt["schema"]["required"]) == {
            "funcao", "quando_usar", "tom", "slots", "nsfw"}


def test_collect_grava_contratos_com_source_hash(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONTRACTS_PATH", tmp_path / "c.json")
    fake = FakeAnthropic(contracts={tid: contrato(t) for tid, t in catalog.items()})
    monkeypatch.setattr(enrich.anthropic, "Anthropic", lambda: fake)
    fake.messages.batches._requests = [
        {"custom_id": tid, "params": {}} for tid in catalog
    ]

    novos = enrich.collect("batch_fake_1", catalog, poll=0)
    assert set(novos) == set(catalog)
    for tid, c in novos.items():
        # é o carimbo que torna a sincronização incremental
        assert c["source_hash"] == catalog[tid]["_source_hash"]
        assert c["enriched_at"]
        assert len(c["slots"]) == len(catalog[tid]["textBoxes"])


def test_collect_sobrevive_a_resultado_com_erro(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONTRACTS_PATH", tmp_path / "c.json")
    bom = IDS[0]
    fake = FakeAnthropic(contracts={bom: contrato(catalog[bom])})  # o outro falha
    monkeypatch.setattr(enrich.anthropic, "Anthropic", lambda: fake)
    fake.messages.batches._requests = [
        {"custom_id": tid, "params": {}} for tid in catalog
    ]

    novos = enrich.collect("batch_fake_1", catalog, poll=0)
    assert set(novos) == {bom}  # um erro não derruba o lote


def test_prompt_descreve_posicao_de_cada_caixa(catalog):
    p = enrich.build_prompt(catalog["distracted_boyfriend"])
    for b in catalog["distracted_boyfriend"]["textBoxes"]:
        assert f"box_id={b['id']}" in p
    # é a posição que permite ao modelo casar caixa com elemento visual
    assert "% da largura" in p and "% da altura" in p


# -------------------------------------------------------------- generate

def test_suggest_usa_cache_e_devolve_sugestoes(catalog, contracts, monkeypatch):
    esperado = {"sugestoes": [{
        "template_id": "drake_hotline_bling",
        "porque": "serve para contraste",
        "textos": [{"box_id": "text-0", "texto": "antes"},
                   {"box_id": "text-1", "texto": "depois"}],
    }]}
    fake = FakeAnthropic(responses=[esperado])
    monkeypatch.setattr(generate.anthropic, "Anthropic", lambda: fake)

    sug, uso = generate.suggest("uma situação", catalog=catalog,
                                contracts=contracts, modo="full")
    assert sug == esperado["sugestoes"]
    assert uso["modo"] == "full"

    kw = fake.calls[0]
    # o catálogo é o bloco caro: precisa estar marcado para cache
    assert kw["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "CATÁLOGO" in kw["system"][-1]["text"]
    assert kw["output_config"]["format"]["type"] == "json_schema"


def test_modo_auto_cai_para_shortlist_sem_indice(catalog, contracts, monkeypatch):
    fake = FakeAnthropic(responses=[
        {"ids": ["drake_hotline_bling"]},
        {"sugestoes": [{"template_id": "drake_hotline_bling", "porque": "x",
                        "textos": [{"box_id": "text-0", "texto": "a"}]}]},
    ])
    monkeypatch.setattr(generate.anthropic, "Anthropic", lambda: fake)
    monkeypatch.setattr("memegen.retrieve.is_stale", lambda c: True)

    _, uso = generate.suggest("situação", catalog=catalog, contracts=contracts)
    assert uso["modo"] == "shortlist"
    assert len(fake.calls) == 2  # triagem + geração
    assert uso["candidatos"] == 1  # a triagem estreitou o catálogo


def test_shortlist_descarta_id_inventado(catalog, contracts, monkeypatch):
    fake = FakeAnthropic(responses=[
        {"ids": ["drake_hotline_bling", "template_que_nao_existe"]}])
    monkeypatch.setattr(generate.anthropic, "Anthropic", lambda: fake)

    ids, _ = generate.shortlist("situação", catalog, contracts)
    assert ids == ["drake_hotline_bling"]


def test_catalog_text_esconde_nsfw(catalog, contracts):
    contracts["distracted_boyfriend"]["nsfw"] = True
    limpo = generate.catalog_text(catalog, contracts)
    assert "distracted_boyfriend" not in limpo
    assert "distracted_boyfriend" in generate.catalog_text(catalog, contracts, True)


def test_catalog_text_traz_papel_de_cada_slot(catalog, contracts):
    texto = generate.catalog_text(catalog, contracts)
    # sem o papel, o modelo escreve na caixa errada
    assert "text-0=papel de text-0" in texto
    assert "rank" in texto


def test_suggest_sem_contratos_falha_claro(catalog):
    with pytest.raises(RuntimeError, match="memegen enrich"):
        generate.suggest("x", catalog=catalog, contracts={})
