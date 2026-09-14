"""Endpoints da interface web.

A chamada ao modelo é simulada; tudo o mais é real — renderização, cache por
hash, servidor de imagens e rejeição de caminho hostil.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from memegen import config
from memegen import web
from tests.contratos_ouro import OURO
from tests.fake_api import FakeAnthropic

CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"
pytestmark = pytest.mark.skipif(not CATALOG.exists(), reason="rode `memegen sync`")

TID = "drake_hotline_bling"


@pytest.fixture
def cliente(monkeypatch, tmp_path):
    """Servidor com contratos-ouro e saída num diretório temporário."""
    monkeypatch.setattr(config, "CONTRACTS_PATH", tmp_path / "contracts.json")
    monkeypatch.setattr(config, "OUT", tmp_path / "out")
    (tmp_path / "out").mkdir()
    (tmp_path / "contracts.json").write_text(
        json.dumps(OURO, ensure_ascii=False), encoding="utf-8")
    return TestClient(web.app)


def test_pagina_carrega(cliente):
    r = cliente.get("/")
    assert r.status_code == 200
    assert "memegen" in r.text
    assert "text/html" in r.headers["content-type"]


def test_status_reporta_pendencias(cliente):
    d = cliente.get("/api/status").json()
    assert d["templates"] > 800
    assert d["contratos"] == len(OURO)
    assert d["pendentes"] > 0          # só 10 dos 836 têm contrato
    assert "enrich" in d["aviso"]


def test_listagem_ordenada_por_popularidade(cliente):
    d = cliente.get("/api/templates?limit=5").json()
    assert d["total"] > 800
    ranks = [i["rank"] for i in d["itens"]]
    assert ranks == sorted(ranks)
    assert d["itens"][0]["id"] == TID   # o mais popular do catálogo


def test_busca_casa_por_funcao_do_contrato(cliente):
    """Busca por um termo que só existe no contrato, não no nome nem nas
    keywords do 9GAG — é o que a busca do próprio 9GAG não faz."""
    d = cliente.get("/api/templates?q=dilema").json()
    assert "two_buttons" in [i["id"] for i in d["itens"]]


def test_detalhe_traz_historia_e_papeis(cliente):
    d = cliente.get(f"/api/templates/{TID}").json()
    assert d["descricao"]                                   # história do 9GAG
    assert d["contrato"]["funcao"]                          # enriquecimento
    papeis = [b["papel"] for b in d["caixas"]]
    assert papeis == ["a opção rejeitada", "a opção preferida"]


def test_detalhe_de_template_sem_contrato(cliente):
    d = cliente.get("/api/templates/disaster_girl").json()
    assert d["contrato"] is not None      # está no gabarito
    d = cliente.get("/api/templates/success_kid").json()
    assert d["contrato"] is None          # não está, e não quebra
    assert all(b["papel"] is None for b in d["caixas"])


def test_render_e_cache_por_conteudo(cliente):
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")
    corpo = {"template_id": TID, "textos": {"text-0": "antes", "text-1": "depois"}}
    a = cliente.post("/api/render", json=corpo).json()["render"]
    b = cliente.post("/api/render", json=corpo).json()["render"]
    assert a == b                                    # mesma entrada, mesmo arquivo

    corpo["textos"]["text-0"] = "outro"
    c = cliente.post("/api/render", json=corpo).json()["render"]
    assert c != a                                    # entrada diferente, arquivo novo

    img = cliente.get(a)
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/jpeg"
    assert len(img.content) > 1000


def test_render_ignora_box_id_desconhecido(cliente):
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")
    r = cliente.post("/api/render", json={
        "template_id": TID, "textos": {"text-0": "ok", "inventado": "x"}})
    assert r.status_code == 200


def test_render_de_template_inexistente(cliente):
    r = cliente.post("/api/render", json={"template_id": "nao_existe", "textos": {}})
    assert r.status_code == 404


def test_caminho_hostil_e_recusado(cliente):
    assert cliente.get("/render/..%2f..%2fetc%2fpasswd").status_code in (400, 404)
    assert cliente.get("/template/..%2f..%2fetc%2fpasswd").status_code in (400, 404)
    assert cliente.get("/render/naoexiste.jpg").status_code == 404


def test_suggest_renderiza_e_devolve_papeis(cliente, monkeypatch):
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")
    from memegen import generate
    fake = FakeAnthropic(responses=[{"sugestoes": [{
        "template_id": TID, "porque": "contraste entre duas opções",
        "textos": [{"box_id": "text-0", "texto": "antes"},
                   {"box_id": "text-1", "texto": "depois"}]}]}])
    monkeypatch.setattr(generate.anthropic, "Anthropic", lambda: fake)

    r = cliente.post("/api/suggest", json={"situacao": "prefiro isto àquilo",
                                           "n": 1, "modo": "full"})
    assert r.status_code == 200
    d = r.json()
    s = d["sugestoes"][0]
    assert s["id"] == TID
    assert s["porque"]
    assert s["textos"] == {"text-0": "antes", "text-1": "depois"}
    assert cliente.get(s["render"]).status_code == 200   # a imagem existe
    assert [b["papel"] for b in s["caixas"]] == ["a opção rejeitada", "a opção preferida"]
    assert d["uso"]["modo"] == "full"


def test_suggest_exige_situacao(cliente):
    assert cliente.post("/api/suggest", json={"situacao": "   "}).status_code == 400


def test_suggest_sem_contratos_avisa(cliente, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONTRACTS_PATH", tmp_path / "vazio.json")
    r = cliente.post("/api/suggest", json={"situacao": "algo"})
    assert r.status_code == 503
    assert "enrich" in r.json()["detail"]


# ---------------------------------------------------------------- zip

def _zip_de(cliente, itens, situacao=""):
    return cliente.post("/api/zip", json={"itens": itens, "situacao": situacao})


def test_zip_empacota_todos_os_memes(cliente):
    import io
    import zipfile
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")

    r = _zip_de(cliente, [
        {"template_id": TID, "textos": {"text-0": "antes", "text-1": "depois"}},
        {"template_id": "two_buttons",
         "textos": {"text-0": "a", "text-1": "b", "text-2": "eu"}},
    ], "escolher entre dormir cedo ou terminar a série")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["01-drake_hotline_bling.jpg", "02-two_buttons.jpg"]
    assert z.testzip() is None
    for nome in z.namelist():
        assert z.read(nome).startswith(b"\xff\xd8")   # JPEG de verdade


def test_zip_usa_o_texto_atual_e_nao_o_gerado(cliente):
    """O botão baixa o que está nos campos agora, depois das edições."""
    import io
    import zipfile
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")

    original = {"template_id": TID, "textos": {"text-0": "antes", "text-1": "depois"}}
    editado = {"template_id": TID, "textos": {"text-0": "EDITADO", "text-1": "depois"}}
    a = zipfile.ZipFile(io.BytesIO(_zip_de(cliente, [original]).content)).read(
        "01-drake_hotline_bling.jpg")
    b = zipfile.ZipFile(io.BytesIO(_zip_de(cliente, [editado]).content)).read(
        "01-drake_hotline_bling.jpg")
    assert a != b


def test_zip_nomeia_pelo_texto_da_situacao(cliente):
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")
    r = _zip_de(cliente, [{"template_id": TID, "textos": {"text-0": "x"}}],
                "Reunião!! que podia ter sido um e-mail")
    cd = r.headers["content-disposition"]
    assert "reuniao-que-podia-ter-sido-um-e-mail" in cd   # sem acento nem pontuação
    assert cd.endswith('.zip"')


def test_zip_do_mesmo_template_duas_vezes_nao_colide(cliente):
    import io
    import zipfile
    if not (config.TEMPLATE_IMAGES / f"{TID}.jpg").exists():
        pytest.skip("imagens não baixadas")
    r = _zip_de(cliente, [
        {"template_id": TID, "textos": {"text-0": "um"}},
        {"template_id": TID, "textos": {"text-0": "dois"}},
    ])
    nomes = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(nomes) == len(set(nomes)) == 2


def test_zip_vazio_e_recusado(cliente):
    assert _zip_de(cliente, []).status_code == 400


def test_zip_com_template_inexistente(cliente):
    r = _zip_de(cliente, [{"template_id": "nao_existe", "textos": {}}])
    assert r.status_code == 400
