"""Caminhos e constantes compartilhadas."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MEMEGEN_DATA", ROOT / "data"))
TEMPLATE_IMAGES = DATA / "templates"
OUT = Path(os.environ.get("MEMEGEN_OUT", ROOT / "out"))
FONTS = ROOT / "fonts"

CATALOG_PATH = DATA / "catalog.json"      # espelho cru do 9GAG
CONTRACTS_PATH = DATA / "contracts.json"  # enriquecimento próprio (pt-BR)
STATE_PATH = DATA / "state.json"          # ponteiros da última sincronização

# O editor do 9GAG posiciona as caixas de texto num canvas de largura fixa,
# com altura proporcional à imagem. Verificado nos 836 templates: nenhuma caixa
# ultrapassa esses limites, e 744 encostam exatamente em x=640.
CANVAS_WIDTH = 640.0

ENRICH_MODEL = os.environ.get("MEMEGEN_ENRICH_MODEL", "claude-sonnet-5")
GENERATE_MODEL = os.environ.get("MEMEGEN_GENERATE_MODEL", "claude-opus-5")


def canvas_size(image_width: int, image_height: int) -> tuple[float, float]:
    """Dimensões do canvas onde as coordenadas das caixas estão expressas."""
    return CANVAS_WIDTH, CANVAS_WIDTH * image_height / image_width


def ensure_dirs() -> None:
    for d in (DATA, TEMPLATE_IMAGES, OUT, FONTS):
        d.mkdir(parents=True, exist_ok=True)
