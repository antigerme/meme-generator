"""Busca semântica local sobre o catálogo enriquecido.

Alternativa ao estágio de triagem por LLM. A triagem manda ~20 mil tokens para
um modelo barato a cada meme; aqui o custo marginal por consulta é zero — os
vetores dos 836 templates cabem num arquivo .npy de poucos megabytes e a busca
é um produto escalar em numpy. Não há banco vetorial: para 836 itens, um índice
dedicado seria mais peça para manter do que ganho.

O que é indexado é a FUNÇÃO do meme, não sua aparência: "ser tentado por algo
novo ignorando o que já funciona", não "homem de casaco laranja". Isso não é
preferência de estilo — foi medido. Indexando a descrição original do 9GAG, que
é visual e histórica, a busca casa por assunto de superfície e erra o alvo:

    "escolher entre dormir cedo ou terminar a série"
      -> Alarm Clock, Sleeping Shaq          (casou com "sono")
      esperado: Two Buttons                  (a função é dilema)

    "troquei de framework de novo"
      -> Skype, Internet Explorer            (casou com "tecnologia")
      esperado: Distracted Boyfriend         (a função é tentação pelo novo)

Por isso `_document()` monta o texto a partir de `funcao`, `quando_usar` e dos
papéis dos slots — os campos que o enriquecimento produz — e nunca da descrição
crua do 9GAG. A qualidade da busca é herdada inteiramente da qualidade do
enriquecimento.

Dependência opcional: `pip install sentence-transformers`. O import é preguiçoso
para que o resto do projeto funcione sem ela.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import catalog as C
from . import config

# Medido sobre 10 situações em pt-BR, com contratos escritos à mão competindo
# contra 826 distratores (ver tools/bench_retrieve.py). O que importa é top-30,
# porque é esse conjunto que vai para o modelo gerador:
#
#   paraphrase-multilingual-MiniLM-L12-v2   top-5  4/10   top-30  7/10   (~120 MB)
#   intfloat/multilingual-e5-large          top-5 10/10   top-30 10/10   (~2,2 GB)
#
# O modelo pequeno deixa o meme certo de fora em 30% dos casos, e nesses o
# gerador nunca tem chance de acertar. O grande custa disco e uma carga mais
# lenta, mas roda em CPU e só é usado na indexação e na consulta.
DEFAULT_MODEL = "intfloat/multilingual-e5-large"
FAST_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

INDEX_PATH = config.DATA / "index.npz"
INDEX_META = config.DATA / "index.json"

_model = None


def _prefixos(name: str) -> tuple[str, str]:
    """A família e5 foi treinada com prefixos e perde qualidade sem eles."""
    return ("query: ", "passage: ") if "e5" in name.lower() else ("", "")


def _load_model(name: str):
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "busca vetorial exige sentence-transformers:\n"
                "  pip install sentence-transformers\n"
                "ou use `memegen make --modo shortlist` (triagem por LLM)"
            ) from e
        _model = SentenceTransformer(name)
    return _model


def _document(contract: dict) -> str:
    """O texto que representa o template no índice."""
    partes = [contract["funcao"], *contract.get("quando_usar", [])]
    partes += [s["papel"] for s in contract.get("slots", [])]
    partes += contract.get("tom", [])
    return ". ".join(p for p in partes if p)


def build(contracts: dict | None = None, model_name: str = DEFAULT_MODEL) -> int:
    """Constrói o índice a partir dos contratos. Devolve quantos foram indexados."""
    import numpy as np

    contracts = contracts if contracts is not None else C.load_contracts()
    if not contracts:
        raise RuntimeError("nenhum contrato — rode `memegen enrich` antes")

    _, pre_doc = _prefixos(model_name)
    ids = sorted(contracts)
    docs = [pre_doc + _document(contracts[i]) for i in ids]
    model = _load_model(model_name)
    vecs = model.encode(docs, normalize_embeddings=True, batch_size=32,
                        show_progress_bar=True)

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(INDEX_PATH, vectors=np.asarray(vecs, dtype="float32"))
    INDEX_META.write_text(
        json.dumps({"ids": ids, "model": model_name, "built_at": C.now()},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return len(ids)


def is_stale(contracts: dict | None = None) -> bool:
    """True se o índice não existe ou não cobre os contratos atuais."""
    if not (INDEX_PATH.exists() and INDEX_META.exists()):
        return True
    contracts = contracts if contracts is not None else C.load_contracts()
    meta = json.loads(INDEX_META.read_text(encoding="utf-8"))
    return set(meta["ids"]) != set(contracts)


def search(situacao: str, k: int = 30) -> list[str]:
    """Devolve os k template_ids mais próximos da situação."""
    import numpy as np

    if not INDEX_PATH.exists():
        raise RuntimeError("índice ausente — rode `memegen index`")
    meta = json.loads(INDEX_META.read_text(encoding="utf-8"))
    vectors = np.load(INDEX_PATH)["vectors"]

    model = _load_model(meta["model"])
    pre_q, _ = _prefixos(meta["model"])
    q = model.encode([pre_q + situacao], normalize_embeddings=True)[0]
    scores = vectors @ q  # vetores normalizados -> produto escalar é cosseno
    top = np.argsort(-scores)[:k]
    return [meta["ids"][i] for i in top]
