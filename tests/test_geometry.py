"""A geometria do canvas é a base de todo o resto: se a escala estiver errada,
todos os 836 templates renderizam torto. Estes testes travam a descoberta."""

import json
from pathlib import Path

import pytest

from memegen import catalog as C
from memegen.config import CANVAS_WIDTH, canvas_size

CATALOG = Path(__file__).parent.parent / "data" / "catalog.json"
pytestmark = pytest.mark.skipif(
    not CATALOG.exists(), reason="rode `memegen sync` primeiro"
)


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_nenhuma_caixa_escapa_do_canvas(catalog):
    """Todas as caixas cabem num canvas de 640 x (640*h/w), com folga de 1.5px."""
    fora = []
    for tid, t in catalog.items():
        cw, ch = canvas_size(t["width"], t["height"])
        for b in t["textBoxes"]:
            if (b["x"] < -1 or b["y"] < -1
                    or b["x"] + b["width"] > cw + 1.5
                    or b["y"] + b["height"] > ch + 1.5):
                fora.append(tid)
                break
    assert fora == [], f"{len(fora)} templates fora do canvas: {fora[:5]}"


def test_largura_do_canvas_e_atingida(catalog):
    """A maioria dos templates tem alguma caixa encostando em x=640 — é o que
    prova que 640 é a largura real e não um chute."""
    encostam = sum(
        1 for t in catalog.values()
        if abs(max(b["x"] + b["width"] for b in t["textBoxes"]) - CANVAS_WIDTH) < 2
    )
    assert encostam > len(catalog) * 0.8, f"só {encostam}/{len(catalog)} encostam"


def test_todo_template_tem_caixa(catalog):
    assert all(t["textBoxes"] for t in catalog.values())


def test_box_ids_unicos_por_template(catalog):
    for tid, t in catalog.items():
        ids = [b["id"] for b in t["textBoxes"]]
        assert len(ids) == len(set(ids)), f"{tid} tem box_id duplicado"


def test_source_hash_ignora_thumbnail(catalog):
    """Mudar a miniatura não deve invalidar o contrato; mudar a descrição deve."""
    t = dict(next(iter(catalog.values())))
    base = C.source_hash(t)
    t["thumbnailUrl"] = "/templates/outro_small.jpg"
    t["placeholder"] = "xxxx"
    assert C.source_hash(t) == base
    t["description"] = "outra coisa"
    assert C.source_hash(t) != base


def test_diff_detecta_novos_e_alterados(catalog):
    idx = {k: dict(v) for k, v in catalog.items()}
    antigo = {k: dict(v) for k, v in idx.items()}
    removido = next(iter(antigo))
    antigo.pop(removido)
    alterado = next(iter(antigo))
    antigo[alterado]["_source_hash"] = "diferente"

    d = C.diff(antigo, idx)
    assert removido in d.added
    assert alterado in d.changed
    assert d.needs_enrichment == d.added + d.changed
