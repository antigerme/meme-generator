"""Catálogo local: espelho do 9GAG + contratos enriquecidos, com diff incremental.

Dois arquivos, deliberadamente separados:

  catalog.json    espelho cru do 9GAG, substituído a cada sincronização
  contracts.json  enriquecimento próprio em pt-BR, acumulado ao longo do tempo

A separação é o que torna a sincronização barata: quando o 9GAG publica novos
memes, só os templates cujo `source_hash` mudou precisam voltar para o modelo.
Os outros mantêm o contrato que já custou tokens.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config


def source_hash(template: dict) -> str:
    """Impressão digital dos campos que afetam o enriquecimento.

    Mudou o texto, as keywords ou a geometria das caixas -> o contrato precisa
    ser refeito. Mudou só a miniatura ou o placeholder -> não precisa.
    """
    payload = {
        "name": template.get("name"),
        "year": template.get("year"),
        "description": template.get("description"),
        "keywords": template.get("keywords"),
        "url": template.get("url"),
        "width": template.get("width"),
        "height": template.get("height"),
        "boxes": [
            # só o que muda o significado dos slots
            {k: b.get(k) for k in ("id", "x", "y", "width", "height")}
            for b in template.get("textBoxes", [])
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Diff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    @property
    def needs_enrichment(self) -> list[str]:
        return self.added + self.changed

    def __str__(self) -> str:
        return (
            f"{len(self.added)} novos, {len(self.changed)} alterados, "
            f"{len(self.removed)} removidos, {len(self.unchanged)} inalterados"
        )


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False),
        encoding="utf-8",
    )
    tmp.replace(path)  # troca atômica: nunca deixa o catálogo pela metade


def load_catalog() -> dict[str, dict]:
    return _load(config.CATALOG_PATH, {})


def save_catalog(catalog: dict[str, dict]) -> None:
    _save(config.CATALOG_PATH, catalog)


def load_contracts() -> dict[str, dict]:
    return _load(config.CONTRACTS_PATH, {})


def save_contracts(contracts: dict[str, dict]) -> None:
    _save(config.CONTRACTS_PATH, contracts)


def load_state() -> dict:
    return _load(config.STATE_PATH, {})


def save_state(state: dict) -> None:
    _save(config.STATE_PATH, state)


def index(templates: list[dict]) -> dict[str, dict]:
    """Lista do 9GAG -> dicionário por id, com hash e posição de popularidade.

    A ordem do array do 9GAG é o ranking de popularidade deles (Drake, Two
    Buttons e Distracted Boyfriend nas três primeiras posições). Guardamos o
    índice porque é o único sinal de popularidade disponível, e ele serve de
    desempate quando dois templates servem à mesma situação.
    """
    out: dict[str, dict] = {}
    for rank, t in enumerate(templates):
        entry = dict(t)
        entry["_rank"] = rank
        entry["_source_hash"] = source_hash(t)
        out[t["id"]] = entry
    return out


def diff(old: dict[str, dict], new: dict[str, dict]) -> Diff:
    d = Diff()
    for tid, t in new.items():
        if tid not in old:
            d.added.append(tid)
        elif old[tid].get("_source_hash") != t["_source_hash"]:
            d.changed.append(tid)
        else:
            d.unchanged.append(tid)
    d.removed = [tid for tid in old if tid not in new]
    return d


def stale_contracts(catalog: dict[str, dict], contracts: dict[str, dict]) -> list[str]:
    """Templates sem contrato, ou cujo contrato foi gerado de uma versão antiga."""
    out = []
    for tid, t in catalog.items():
        c = contracts.get(tid)
        if c is None or c.get("source_hash") != t["_source_hash"]:
            out.append(tid)
    return out


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
