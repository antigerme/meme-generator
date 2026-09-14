"""Cliente Anthropic falso: registra o que foi enviado e devolve resposta fixa.

Não substitui rodar contra a API de verdade — não diz nada sobre a qualidade do
que o modelo escreve. Serve para o que é determinístico e quebra calado: nome de
parâmetro errado, schema malformado, bloco de imagem na posição errada,
cache_control ausente, parsing que engasga com resposta inesperada.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


class _Usage(SimpleNamespace):
    pass


def _msg(texto: str, model: str = "claude-fake") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=texto)],
        model=model,
        usage=_Usage(
            input_tokens=1000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class FakeBatches:
    def __init__(self, parent):
        self.parent = parent
        self._requests = []

    def create(self, requests):
        self.parent.batch_requests = list(requests)
        self._requests = list(requests)
        return SimpleNamespace(id="batch_fake_1", processing_status="in_progress")

    def retrieve(self, batch_id):
        return SimpleNamespace(
            id=batch_id,
            processing_status="ended",
            request_counts=SimpleNamespace(succeeded=len(self._requests),
                                           processing=0, errored=0),
        )

    def results(self, batch_id):
        for req in self._requests:
            cid = req["custom_id"] if isinstance(req, dict) else req.custom_id
            resposta = self.parent.contract_for(cid)
            if resposta is None:
                yield SimpleNamespace(
                    custom_id=cid,
                    result=SimpleNamespace(type="errored",
                                           error=SimpleNamespace(type="invalid_request")),
                )
            else:
                yield SimpleNamespace(
                    custom_id=cid,
                    result=SimpleNamespace(type="succeeded",
                                           message=_msg(json.dumps(resposta))),
                )


class FakeMessages:
    def __init__(self, parent):
        self.parent = parent
        self.batches = FakeBatches(parent)

    def create(self, **kwargs):
        self.parent.calls.append(kwargs)
        return _msg(json.dumps(self.parent.next_response()))


class FakeAnthropic:
    """Instale com monkeypatch: `anthropic.Anthropic = lambda: fake`."""

    def __init__(self, contracts=None, responses=None):
        self.messages = FakeMessages(self)
        self.calls = []
        self.batch_requests = []
        self._contracts = contracts or {}
        self._responses = list(responses or [])

    def contract_for(self, template_id):
        return self._contracts.get(template_id)

    def next_response(self):
        if not self._responses:
            raise AssertionError("FakeAnthropic: resposta não configurada")
        return self._responses.pop(0)
