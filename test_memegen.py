#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Testes do memegen. Como o proprio programa, so biblioteca padrao.

    python3 -m unittest -v
    python3 test_memegen.py

Nao precisam de rede nem de chave da API: o catalogo vem de um fixture embutido
e as chamadas a API sao substituidas.
"""

import base64
import io
import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
import zipfile
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "memegen_sob_teste", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "memegen.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)


# --------------------------------------------------------------------- fixture

def _caixa(bid, x, y, w, h, **extra):
    base = {"id": bid, "x": x, "y": y, "width": w, "height": h,
            "text": "Text", "fontSize": 50, "fontFamily": "impact",
            "color": "#ffffff", "textAlign": "center", "textBaseline": "middle",
            "capitalize": True, "outlineStyle": "stroke", "strokeWidth": 5,
            "strokeColor": "#000000", "autoFontSize": True, "rotation": 0}
    base.update(extra)
    return base


TEMPLATES = [
    {"id": "drake", "name": "Drake", "description": "It's used to show a preference.",
     "keywords": ["drake", "preferencia"], "year": 2015,
     "url": "/templates/drake.jpg", "thumbnailUrl": "/templates/drake_small.jpg",
     "placeholder": "xx", "width": 1200, "height": 1200,
     "textBoxes": [_caixa("text-0", 326.4, 6.4, 306.9, 306.9),
                   _caixa("text-1", 326.4, 326.4, 306.9, 306.9)]},
    {"id": "botoes", "name": "Dois Botoes", "description": "A hard choice.",
     "keywords": ["dilema"], "year": 2014,
     "url": "/templates/botoes.jpg", "thumbnailUrl": "/templates/botoes_small.jpg",
     "placeholder": "xx", "width": 600, "height": 908,
     "textBoxes": [_caixa("text-0", 58.7, 90.1, 200.0, 96.5, rotation=349),
                   _caixa("text-1", 291.0, 59.0, 153.0, 86.0, rotation=352),
                   _caixa("text-2", 21.0, 803.0, 597.0, 130.0)]},
]

CONTRATOS = {
    "drake": {"funcao": "Contrastar duas opcoes rejeitando a primeira",
              "quando_usar": ["preferir uma coisa a outra"], "tom": ["sarcastico"],
              "slots": [{"box_id": "text-0", "papel": "a opcao rejeitada",
                         "exemplo": "", "max_chars": 60},
                        {"box_id": "text-1", "papel": "a opcao preferida",
                         "exemplo": "", "max_chars": 60}],
              "nsfw": False},
    "botoes": {"funcao": "Sofrer diante de um dilema", "quando_usar": ["dilema"],
               "tom": ["ansioso"],
               "slots": [{"box_id": "text-0", "papel": "primeira opcao",
                          "exemplo": "", "max_chars": 40},
                         {"box_id": "text-1", "papel": "segunda opcao",
                          "exemplo": "", "max_chars": 40},
                         {"box_id": "text-2", "papel": "quem enfrenta",
                          "exemplo": "", "max_chars": 60}],
               "nsfw": False},
}


def jpeg_falso(largura, altura):
    """JPEG minimo, so com o cabecalho SOF0 -- o suficiente para medir."""
    return (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 +
            b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" +
            struct.pack(">HH", altura, largura) + b"\x03" + b"\x00" * 9 +
            b"\xff\xd9")


class Base(unittest.TestCase):
    """Redireciona os caminhos do modulo para um diretorio temporario."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (M.DADOS, M.IMAGENS, M.CATALOGO, M.CONTRATOS, M.ESTADO,
                      M.CERT, M.CHAVE)
        M.DADOS = self.tmp
        M.IMAGENS = os.path.join(self.tmp, "templates")
        M.CATALOGO = os.path.join(self.tmp, "catalogo.json")
        M.CONTRATOS = os.path.join(self.tmp, "contratos.json")
        M.ESTADO = os.path.join(self.tmp, "estado.json")
        M.CERT = os.path.join(self.tmp, "cert.pem")
        M.CHAVE = os.path.join(self.tmp, "key.pem")
        M.garantir_dirs()
        self.catalogo = M.indexar(TEMPLATES)
        M.gravar_json(M.CATALOGO, self.catalogo)
        self.contratos = json.loads(json.dumps(CONTRATOS))
        for tid, c in self.contratos.items():
            c["source_hash"] = self.catalogo[tid]["_hash"]
        M.gravar_json(M.CONTRATOS, self.contratos)
        for t in TEMPLATES:
            with open(os.path.join(M.IMAGENS, t["id"] + ".jpg"), "wb") as f:
                f.write(jpeg_falso(640, int(round(640.0 * t["height"] / t["width"]))))
        M.ESTADO_MEM = M.Estado()

    def tearDown(self):
        (M.DADOS, M.IMAGENS, M.CATALOGO, M.CONTRATOS, M.ESTADO,
         M.CERT, M.CHAVE) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


# ------------------------------------------------------------------- catalogo

class TestCatalogo(Base):

    def test_hash_ignora_miniatura_mas_pega_descricao(self):
        t = dict(TEMPLATES[0])
        base = M.hash_fonte(t)
        t["thumbnailUrl"] = "/outro_small.jpg"
        t["placeholder"] = "zzz"
        self.assertEqual(M.hash_fonte(t), base)
        t["description"] = "outra coisa"
        self.assertNotEqual(M.hash_fonte(t), base)

    def test_hash_pega_geometria_das_caixas(self):
        t = json.loads(json.dumps(TEMPLATES[0]))
        base = M.hash_fonte(t)
        t["textBoxes"][0]["x"] += 10
        self.assertNotEqual(M.hash_fonte(t), base)

    def test_indexar_guarda_ranking_de_popularidade(self):
        self.assertEqual(self.catalogo["drake"]["_rank"], 0)
        self.assertEqual(self.catalogo["botoes"]["_rank"], 1)

    def test_diferenca_classifica_cada_caso(self):
        antigo = json.loads(json.dumps(self.catalogo))
        del antigo["botoes"]
        antigo["drake"]["_hash"] = "diferente"
        d = M.diferenca(antigo, self.catalogo)
        self.assertEqual(d["novos"], ["botoes"])
        self.assertEqual(d["alterados"], ["drake"])
        self.assertEqual(d["removidos"], [])

    def test_pendentes_por_hash_desatualizado(self):
        self.assertEqual(M.contratos_pendentes(self.catalogo, self.contratos), [])
        contratos = json.loads(json.dumps(self.contratos))
        contratos["drake"]["source_hash"] = "velho"
        self.assertEqual(M.contratos_pendentes(self.catalogo, contratos), ["drake"])

    def test_gravar_json_e_atomico(self):
        alvo = os.path.join(self.tmp, "x.json")
        M.gravar_json(alvo, {"a": 1})
        self.assertEqual(M.ler_json(alvo, None), {"a": 1})
        self.assertFalse(os.path.exists(alvo + ".tmp"))


class TestBundle(Base):
    """Extracao do catalogo de dentro do bundle JS do 9GAG."""

    def test_extrai_json_de_dentro_do_bundle(self):
        dados = json.dumps(TEMPLATES).replace("\\", "\\\\").replace("`", "\\`")
        bundle = "var x=1;const Z=JSON.parse(`" + dados + "`),y=2;"
        saida = M.extrair_catalogo(bundle)
        self.assertEqual(len(saida), 2)
        self.assertEqual(saida[0]["id"], "drake")

    def test_desescapa_template_literal(self):
        self.assertEqual(M._desescapar(r"a\`b\\c\$d"), r"a`b\c$d")

    def test_aspas_escapadas_sobrevivem(self):
        """O caso real: as descricoes do 9GAG trazem aspas escapadas."""
        t = [{"id": "x", "name": 'diz "oi"', "textBoxes": []}]
        dados = json.dumps(t).replace("\\", "\\\\").replace("`", "\\`")
        saida = M.extrair_catalogo("JSON.parse(`" + dados + "`)")
        self.assertEqual(saida[0]["name"], 'diz "oi"')

    def test_bundle_sem_json_da_erro_claro(self):
        with self.assertRaises(RuntimeError) as ctx:
            M.extrair_catalogo("var x = 1;")
        self.assertIn("empacotamento", str(ctx.exception))


# ------------------------------------------------------------------ geometria

class TestGeometria(Base):

    def test_canvas_tem_640_de_largura(self):
        cw, ch = M.tamanho_canvas(600, 908)
        self.assertEqual(cw, 640.0)
        self.assertAlmostEqual(ch, 640.0 * 908 / 600, places=6)

    def test_nenhuma_caixa_escapa_do_canvas(self):
        for tid, t in self.catalogo.items():
            cw, ch = M.tamanho_canvas(t["width"], t["height"])
            for b in t["textBoxes"]:
                self.assertGreaterEqual(b["x"], -1, tid)
                self.assertLessEqual(b["x"] + b["width"], cw + 1.5, tid)
                self.assertLessEqual(b["y"] + b["height"], ch + 1.5, tid)

    def test_le_a_dimensao_real_do_jpeg(self):
        """O catalogo declara 600x908; o 9GAG serve 640x969. E o servido que
        sai no meme, entao e o que a interface mostra."""
        larg, alt = M._tamanho_real("botoes", self.catalogo["botoes"])
        self.assertEqual((larg, alt), (640, 969))
        self.assertNotEqual((larg, alt), (600, 908))

    def test_dimensao_cai_para_o_catalogo_sem_imagem(self):
        os.remove(os.path.join(M.IMAGENS, "drake.jpg"))
        self.assertEqual(M._tamanho_real("drake", self.catalogo["drake"]),
                         (1200, 1200))

    def test_arquivo_corrompido_nao_derruba(self):
        with open(os.path.join(M.IMAGENS, "drake.jpg"), "wb") as f:
            f.write(b"nao sou um jpeg")
        self.assertEqual(M._tamanho_real("drake", self.catalogo["drake"]),
                         (1200, 1200))


# ----------------------------------------------------------------- prompting

class TestPrompt(Base):

    def test_prompt_descreve_a_posicao_de_cada_caixa(self):
        p = M.prompt_enriquecimento(TEMPLATES[1])
        for b in TEMPLATES[1]["textBoxes"]:
            self.assertIn("box_id=" + b["id"], p)
        self.assertIn("% da largura", p)
        self.assertIn("Descricao do 9GAG", p)

    def test_posicao_situa_a_caixa_na_imagem(self):
        cw, ch = M.tamanho_canvas(1200, 1200)
        # caixa da direita no Drake
        self.assertIn("direita", M._posicao(TEMPLATES[0]["textBoxes"][0], cw, ch))
        self.assertIn("topo", M._posicao(TEMPLATES[0]["textBoxes"][0], cw, ch))
        self.assertIn("base", M._posicao(TEMPLATES[0]["textBoxes"][1], cw, ch))

    def test_catalogo_serializado_traz_o_papel_dos_slots(self):
        txt = M.texto_catalogo(self.catalogo, self.contratos)
        self.assertIn("text-0=a opcao rejeitada (max 60)", txt)
        self.assertIn("rank 0", txt)
        self.assertIn("funcao:", txt)

    def test_serializacao_respeita_nsfw(self):
        contratos = json.loads(json.dumps(self.contratos))
        contratos["botoes"]["nsfw"] = True
        self.assertNotIn("botoes", M.texto_catalogo(self.catalogo, contratos))
        self.assertIn("botoes", M.texto_catalogo(self.catalogo, contratos, nsfw=True))

    def test_triagem_e_mais_enxuta_que_o_catalogo(self):
        curto = M.texto_triagem(self.catalogo, self.contratos)
        longo = M.texto_catalogo(self.catalogo, self.contratos)
        self.assertLess(len(curto), len(longo))
        self.assertIn("drake: Contrastar", curto)


# ------------------------------------------------------------ chamadas de API

class FalsaAPI(object):
    """Substitui chamar_api registrando o que seria enviado."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def __call__(self, caminho, carga=None, metodo=None, timeout=300):
        self.chamadas.append({"caminho": caminho, "carga": carga})
        if not self.respostas:
            raise AssertionError("FalsaAPI: resposta nao configurada para " + caminho)
        return self.respostas.pop(0)


def _msg(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}],
            "model": "falso", "usage": {"input_tokens": 10, "output_tokens": 5}}


class TestAPI(Base):

    def setUp(self):
        Base.setUp(self)
        self._chamar = M.chamar_api
        self._chave = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-teste"

    def tearDown(self):
        M.chamar_api = self._chamar
        if self._chave is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._chave
        Base.tearDown(self)

    def test_lote_leva_imagem_antes_do_texto_e_schema_fechado(self):
        falsa = FalsaAPI([{"id": "batch_1"}])
        M.chamar_api = falsa
        M.enviar_lote(self.catalogo, ["drake", "botoes"], modelo="modelo-x")

        carga = falsa.chamadas[0]["carga"]
        self.assertEqual(falsa.chamadas[0]["caminho"], "/v1/messages/batches")
        self.assertEqual(len(carga["requests"]), 2)
        for req in carga["requests"]:
            self.assertIn(req["custom_id"], self.catalogo)
            blocos = req["params"]["messages"][0]["content"]
            self.assertEqual(blocos[0]["type"], "image")
            self.assertEqual(blocos[0]["source"]["media_type"], "image/jpeg")
            self.assertTrue(blocos[0]["source"]["data"])
            self.assertEqual(blocos[1]["type"], "text")
            fmt = req["params"]["output_config"]["format"]
            self.assertEqual(fmt["type"], "json_schema")
            self.assertFalse(fmt["schema"]["additionalProperties"])
            self.assertEqual(req["params"]["model"], "modelo-x")

    def test_geracao_marca_o_catalogo_para_cache(self):
        falsa = FalsaAPI([_msg({"sugestoes": []})])
        M.chamar_api = falsa
        M.sugerir("uma situacao", self.catalogo, self.contratos, modo="full")
        sistema = falsa.chamadas[0]["carga"]["system"]
        self.assertEqual(sistema[-1]["cache_control"], {"type": "ephemeral"})
        self.assertIn("CATALOGO", sistema[-1]["text"])

    def test_triagem_estreita_o_catalogo_e_descarta_id_inventado(self):
        falsa = FalsaAPI([_msg({"ids": ["drake", "nao_existe"]}),
                          _msg({"sugestoes": []})])
        M.chamar_api = falsa
        _, uso = M.sugerir("x", self.catalogo, self.contratos, modo="shortlist")
        self.assertEqual(uso["candidatos"], 1)
        self.assertIsNotNone(uso["triagem"])
        self.assertEqual(len(falsa.chamadas), 2)

    def test_modo_full_nao_gasta_token_com_triagem(self):
        falsa = FalsaAPI([_msg({"sugestoes": []})])
        M.chamar_api = falsa
        _, uso = M.sugerir("x", self.catalogo, self.contratos, modo="full")
        self.assertIsNone(uso["triagem"])
        self.assertEqual(len(falsa.chamadas), 1)

    def test_sem_contrato_o_erro_aponta_o_caminho(self):
        with self.assertRaises(RuntimeError) as ctx:
            M.sugerir("x", self.catalogo, {})
        self.assertIn("enrich", str(ctx.exception))

    def test_coleta_grava_hash_e_sobrevive_a_erro(self):
        contratos = dict((tid, dict(self.contratos[tid])) for tid in self.contratos)
        linhas = [
            json.dumps({"custom_id": "drake",
                        "result": {"type": "succeeded", "message": _msg(contratos["drake"])}}),
            json.dumps({"custom_id": "botoes", "result": {"type": "errored"}}),
        ]
        M.chamar_api = FalsaAPI([{"processing_status": "ended",
                                  "results_url": "https://exemplo/resultados"}])
        M.gravar_json(M.CONTRATOS, {})
        original = M.buscar_texto
        M.buscar_texto = lambda url, **kw: "\n".join(linhas)
        try:
            novos = M.coletar_lote("batch_1", self.catalogo, intervalo=0)
        finally:
            M.buscar_texto = original
        self.assertEqual(list(novos), ["drake"])          # o erro nao derruba o lote
        self.assertEqual(novos["drake"]["source_hash"], self.catalogo["drake"]["_hash"])
        self.assertIn("enriched_at", novos["drake"])

    def test_chave_ausente_da_mensagem_util(self):
        anterior = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                M.chave_api()
            self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))
        finally:
            if anterior:
                os.environ["ANTHROPIC_API_KEY"] = anterior


# ------------------------------------------------------------------- servidor

class TestServidor(Base):

    def setUp(self):
        Base.setUp(self)
        self.srv = M.Servidor(("127.0.0.1", 0), M.Handler)
        self.porta = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.porta

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        Base.tearDown(self)

    def get(self, rota):
        r = urlopen(self.base + rota, timeout=10)
        try:
            return r.getcode(), r.read(), dict(r.headers)
        finally:
            r.close()

    def post(self, rota, corpo):
        req = Request(self.base + rota, data=json.dumps(corpo).encode(),
                      headers={"Content-Type": "application/json"})
        r = urlopen(req, timeout=20)
        try:
            return r.getcode(), r.read(), dict(r.headers)
        finally:
            r.close()

    def json_get(self, rota):
        return json.loads(self.get(rota)[1].decode())

    def test_pagina_carrega_e_traz_o_renderizador(self):
        codigo, corpo, cab = self.get("/")
        self.assertEqual(codigo, 200)
        texto = corpo.decode()
        self.assertIn("text/html", cab["Content-Type"])
        # o canvas e onde a renderizacao acontece; sem ele nao ha meme
        self.assertIn("function desenhar", texto)
        self.assertIn("CANVAS_W = 640", texto)

    def test_status_aponta_o_que_falta(self):
        d = self.json_get("/api/status")
        self.assertTrue(d["pronto"])
        self.assertEqual(d["templates"], 2)
        self.assertEqual(d["contratos"], 2)
        M.gravar_json(M.CONTRATOS, {})
        d = self.json_get("/api/status")
        self.assertFalse(d["pronto"])
        self.assertIn("enrich", d["aviso"])

    def test_listagem_ordena_por_popularidade(self):
        d = self.json_get("/api/templates")
        self.assertEqual([i["id"] for i in d["itens"]], ["drake", "botoes"])
        self.assertEqual(d["total"], 2)

    def test_busca_casa_pela_funcao_do_contrato(self):
        """'dilema' nao esta no nome nem nas keywords do 9GAG: so na funcao.
        E exatamente o que a busca do proprio 9GAG nao faz."""
        d = self.json_get("/api/templates?q=dilema")
        self.assertEqual([i["id"] for i in d["itens"]], ["botoes"])

    def test_paginacao(self):
        d = self.json_get("/api/templates?limit=1&offset=1")
        self.assertEqual(d["total"], 2)
        self.assertEqual([i["id"] for i in d["itens"]], ["botoes"])

    def test_detalhe_traz_historia_papeis_e_geometria(self):
        d = self.json_get("/api/templates/drake")
        self.assertTrue(d["descricao"])
        self.assertEqual([b["papel"] for b in d["caixas"]],
                         ["a opcao rejeitada", "a opcao preferida"])
        # o navegador precisa da geometria completa para desenhar
        for chave in ("x", "y", "width", "height", "fontSize", "strokeWidth",
                      "capitalize", "rotation", "autoFontSize"):
            self.assertIn(chave, d["caixas"][0])

    def test_template_sem_contrato_nao_quebra(self):
        M.gravar_json(M.CONTRATOS, {})
        d = self.json_get("/api/templates/drake")
        self.assertIsNone(d["contrato"])
        self.assertTrue(all(b["papel"] is None for b in d["caixas"]))

    def test_imagem_e_servida(self):
        codigo, corpo, cab = self.get("/template/drake.jpg")
        self.assertEqual(codigo, 200)
        self.assertEqual(cab["Content-Type"], "image/jpeg")
        self.assertTrue(corpo.startswith(b"\xff\xd8"))

    def test_caminho_hostil_e_recusado(self):
        for rota in ("/template/..%2f..%2fetc%2fpasswd", "/template/../../etc/passwd"):
            try:
                codigo = self.get(rota)[0]
            except HTTPError as e:
                codigo = e.code
                e.close()
            self.assertIn(codigo, (400, 404), rota)

    def test_rota_desconhecida(self):
        try:
            codigo = self.get("/nao/existe")[0]
        except HTTPError as e:
            codigo = e.code
            e.close()
        self.assertEqual(codigo, 404)

    def test_zip_empacota_o_que_o_navegador_desenhou(self):
        png = base64.b64encode(jpeg_falso(10, 10)).decode()   # bytes quaisquer
        codigo, corpo, cab = self.post("/api/zip", {
            "itens": [{"nome": "01-drake.png", "png": png},
                      {"nome": "02-botoes.png", "png": png}],
            "situacao": "Reuniao!! que podia ter sido e-mail"})
        self.assertEqual(codigo, 200)
        self.assertEqual(cab["Content-Type"], "application/zip")
        self.assertIn("reuniao-que-podia-ter-sido-e-mail", cab["Content-Disposition"])
        z = zipfile.ZipFile(io.BytesIO(corpo))
        self.assertEqual(z.namelist(), ["01-drake.png", "02-botoes.png"])
        self.assertIsNone(z.testzip())

    def test_zip_recusa_nome_hostil(self):
        png = base64.b64encode(b"x").decode()
        corpo = self.post("/api/zip", {"itens": [{"nome": "../../fora.png", "png": png}]})[1]
        nomes = zipfile.ZipFile(io.BytesIO(corpo)).namelist()
        self.assertEqual(nomes, ["fora.png"])       # sobrou so o basename

    def test_zip_vazio_e_recusado(self):
        try:
            codigo = self.post("/api/zip", {"itens": []})[0]
        except HTTPError as e:
            codigo = e.code
            e.close()
        self.assertEqual(codigo, 400)

    def test_suggest_exige_situacao(self):
        try:
            codigo = self.post("/api/suggest", {"situacao": "   "})[0]
        except HTTPError as e:
            codigo = e.code
            e.close()
        self.assertEqual(codigo, 400)

    def test_estado_releva_arquivo_alterado(self):
        """O servidor precisa enxergar um `enrich` rodado com ele no ar."""
        self.assertEqual(self.json_get("/api/status")["contratos"], 2)
        M.gravar_json(M.CONTRATOS, {"drake": self.contratos["drake"]})
        os.utime(M.CONTRATOS, (0, 0))              # forca mtime diferente
        self.assertEqual(self.json_get("/api/status")["contratos"], 1)


# ----------------------------------------------------------------------- TLS

class TestTLS(Base):
    """navigator.share e navigator.clipboard exigem contexto seguro. Do
    celular o servidor e alcancado pelo IP da rede, que em HTTP nao e seguro."""

    def setUp(self):
        Base.setUp(self)
        if not shutil.which("openssl"):
            self.skipTest("openssl nao disponivel")

    def test_gera_par_utilizavel(self):
        cert, chave = M.gerar_certificado()
        self.assertTrue(os.path.exists(cert) and os.path.exists(chave))
        with open(cert) as f:
            self.assertIn("BEGIN CERTIFICATE", f.read())
        if os.name != "nt":
            self.assertEqual(oct(os.stat(chave).st_mode)[-3:], "600")

    def test_cobre_localhost_e_os_ips_da_maquina(self):
        import subprocess
        cert, _ = M.gerar_certificado()
        saida = subprocess.check_output(
            ["openssl", "x509", "-in", cert, "-noout", "-text"]).decode()
        self.assertIn("DNS:localhost", saida)
        for ip in M.ips_locais():
            self.assertIn("IP Address:" + ip, saida)
        self.assertIn("CA:FALSE", saida)

    def test_reaproveita_o_existente(self):
        cert, chave = M.gerar_certificado()
        with open(cert, "rb") as f:
            antes = f.read()
        M.gerar_certificado()
        with open(cert, "rb") as f:
            self.assertEqual(f.read(), antes)
        M.gerar_certificado(forcar=True)
        with open(cert, "rb") as f:
            self.assertNotEqual(f.read(), antes)

    def test_ips_locais_incluem_loopback(self):
        self.assertIn("127.0.0.1", M.ips_locais())


# -------------------------------------------------------------- portabilidade

class TestPortabilidade(unittest.TestCase):

    def test_so_usa_biblioteca_padrao(self):
        import ast
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memegen.py")
        with io.open(caminho, encoding="utf-8") as f:
            arvore = ast.parse(f.read())
        modulos = set()
        for n in ast.walk(arvore):
            if isinstance(n, ast.Import):
                modulos.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                modulos.add(n.module.split(".")[0])
        import sys
        padrao = getattr(sys, "stdlib_module_names", None)
        if padrao is None:
            self.skipTest("sys.stdlib_module_names exige 3.10+")
        fora = sorted(m for m in modulos if m not in padrao)
        self.assertEqual(fora, [], "importa de fora da biblioteca padrao: %s" % fora)

    def test_nao_usa_sintaxe_posterior_ao_36(self):
        """Barreiras comuns: walrus, f-string com '=', match, e anotacoes com
        tipos embutidos parametrizados (list[str])."""
        import ast
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memegen.py")
        with io.open(caminho, encoding="utf-8") as f:
            arvore = ast.parse(f.read())
        achados = []
        for n in ast.walk(arvore):
            if n.__class__.__name__ == "NamedExpr":
                achados.append("walrus na linha %d" % n.lineno)
            if n.__class__.__name__ == "Match":
                achados.append("match na linha %d" % n.lineno)
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                    and n.value.id in ("list", "dict", "tuple", "set", "type"):
                achados.append("%s[...] na linha %d" % (n.value.id, n.lineno))
        self.assertEqual(achados, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestWorkspace(Base):
    """Chave de organizacao exige dizer qual workspace usar."""

    def setUp(self):
        Base.setUp(self)
        self._antes = dict((k, os.environ.get(k))
                           for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"))
        os.environ["ANTHROPIC_API_KEY"] = "sk-teste"
        os.environ.pop("ANTHROPIC_WORKSPACE_ID", None)

    def tearDown(self):
        for k, v in self._antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        Base.tearDown(self)

    def test_sem_workspace_o_header_nao_vai(self):
        self.assertNotIn("anthropic-workspace-id", M._cabecalhos_api())

    def test_com_workspace_o_header_vai(self):
        os.environ["ANTHROPIC_WORKSPACE_ID"] = "wrkspc_abc"
        self.assertEqual(M._cabecalhos_api()["anthropic-workspace-id"], "wrkspc_abc")

    def test_cabecalhos_basicos_sempre_presentes(self):
        cab = M._cabecalhos_api()
        self.assertEqual(cab["x-api-key"], "sk-teste")
        self.assertEqual(cab["anthropic-version"], M.VERSAO_API)


# ---------------------------------------------------------------- provedores

class RespostaFalsa(object):
    """Substitui o objeto devolvido por urlopen."""

    def __init__(self, dados):
        self._dados = json.dumps(dados).encode("utf-8")

    def read(self):
        return self._dados

    def close(self):
        pass


class TestProvedor(Base):

    def setUp(self):
        Base.setUp(self)
        self._antes = dict((k, os.environ.get(k)) for k in
                           ("ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                            "MEMEGEN_PROVIDER", "MEMEGEN_ENRICH_MODEL"))
        for k in self._antes:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        Base.tearDown(self)

    def test_a_chave_presente_escolhe_o_provedor(self):
        os.environ["GEMINI_API_KEY"] = "g"
        self.assertEqual(M.provedor(), "gemini")
        os.environ["ANTHROPIC_API_KEY"] = "a"
        # com as duas, anthropic e o padrao
        self.assertEqual(M.provedor(), "anthropic")

    def test_variavel_desempata(self):
        os.environ["ANTHROPIC_API_KEY"] = "a"
        os.environ["GEMINI_API_KEY"] = "g"
        os.environ["MEMEGEN_PROVIDER"] = "gemini"
        self.assertEqual(M.provedor(), "gemini")

    def test_provedor_desconhecido_e_recusado(self):
        os.environ["MEMEGEN_PROVIDER"] = "openai"
        with self.assertRaises(RuntimeError):
            M.provedor()

    def test_modelos_mudam_com_o_provedor(self):
        os.environ["GEMINI_API_KEY"] = "g"
        self.assertTrue(M.modelo_padrao("gerar").startswith("gemini"))
        os.environ["ANTHROPIC_API_KEY"] = "a"
        self.assertTrue(M.modelo_padrao("gerar").startswith("claude"))

    def test_variavel_de_modelo_vence_o_padrao(self):
        os.environ["GEMINI_API_KEY"] = "g"
        os.environ["MEMEGEN_ENRICH_MODEL"] = "modelo-meu"
        self.assertEqual(M.modelo_padrao("enriquecer"), "modelo-meu")


class TestGemini(Base):

    def setUp(self):
        Base.setUp(self)
        self._antes = dict((k, os.environ.get(k)) for k in
                           ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MEMEGEN_PROVIDER"))
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = "chave-teste"
        os.environ["MEMEGEN_PROVIDER"] = "gemini"
        self._urlopen = M.urlopen
        self.enviado = []

    def tearDown(self):
        M.urlopen = self._urlopen
        for k, v in self._antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        Base.tearDown(self)

    def _responder(self, corpo):
        def falso(req, timeout=None):
            self.enviado.append(
                {"url": req.get_full_url(),
                 "cabecalhos": dict((k.lower(), v) for k, v in req.header_items()),
                 "carga": json.loads(req.data.decode("utf-8"))})
            return RespostaFalsa(corpo)
        M.urlopen = falso

    def test_requisicao_tem_a_forma_do_endpoint_interactions(self):
        self._responder({"output_text": '{"ids": []}'})
        M.gerar_json("instrucao", "pergunta", M.ESQUEMA_TRIAGEM, "triar")
        env = self.enviado[0]
        self.assertTrue(env["url"].endswith("/v1beta/interactions"))
        self.assertEqual(env["cabecalhos"]["x-goog-api-key"], "chave-teste")
        carga = env["carga"]
        self.assertEqual(carga["system_instruction"], "instrucao")
        self.assertEqual(carga["input"], [{"type": "text", "text": "pergunta"}])
        # o schema vai no topo, nao dentro de generationConfig
        self.assertEqual(carga["response_format"]["mime_type"], "application/json")
        self.assertEqual(carga["response_format"]["schema"], M.ESQUEMA_TRIAGEM)
        self.assertTrue(carga["model"].startswith("gemini"))

    def test_imagem_vai_como_bloco_inline(self):
        self._responder({"output_text": "{}"})
        img = os.path.join(M.IMAGENS, "drake.jpg")
        M.gerar_json("s", "t", M.ESQUEMA_CONTRATO, "enriquecer", imagem=img)
        blocos = self.enviado[0]["carga"]["input"]
        imagem = [b for b in blocos if b.get("type") == "image"][0]
        self.assertEqual(imagem["mime_type"], "image/jpeg")
        with open(img, "rb") as f:
            self.assertEqual(base64.b64decode(imagem["data"]), f.read())

    def test_sistema_em_lista_vira_uma_instrucao_so(self):
        self._responder({"output_text": "{}"})
        M.gerar_json(["parte um", "parte dois"], "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertIn("parte um", self.enviado[0]["carga"]["system_instruction"])
        self.assertIn("parte dois", self.enviado[0]["carga"]["system_instruction"])

    def test_le_o_texto_pelo_caminho_alternativo(self):
        """Sem output_text, o conteudo vem no ultimo passo."""
        self._responder({"steps": [{"content": [{"text": '{"ids": ["drake"]}'}]}]})
        dados, _ = M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual(dados["ids"], ["drake"])

    def test_contagem_de_tokens_aceita_nomes_diferentes(self):
        self._responder({"output_text": "{}",
                         "usageMetadata": {"promptTokenCount": 120,
                                           "candidatesTokenCount": 30}})
        _, uso = M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual((uso["input"], uso["output"]), (120, 30))

    def test_sem_contagem_nao_quebra(self):
        self._responder({"output_text": "{}"})
        _, uso = M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual((uso["input"], uso["output"]), (0, 0))

    def test_resposta_sem_texto_da_erro_claro(self):
        self._responder({"steps": []})
        with self.assertRaises(RuntimeError) as ctx:
            M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertIn("sem texto", str(ctx.exception))

    def test_json_invalido_da_erro_claro(self):
        self._responder({"output_text": "isto nao e json"})
        with self.assertRaises(RuntimeError) as ctx:
            M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertIn("JSON", str(ctx.exception))

    def test_chave_ausente_aponta_onde_pegar(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(RuntimeError) as ctx:
            M.chave_gemini()
        self.assertIn("aistudio", str(ctx.exception))

    def test_sugerir_funciona_ponta_a_ponta(self):
        self._responder({"output_text": json.dumps({"sugestoes": [
            {"template_id": "drake", "porque": "serve",
             "textos": [{"box_id": "text-0", "texto": "a"},
                        {"box_id": "text-1", "texto": "b"}]}]})})
        sug, uso = M.sugerir("uma situacao", self.catalogo, self.contratos, modo="full")
        self.assertEqual(sug[0]["template_id"], "drake")
        self.assertEqual(uso["provedor"], "gemini")


class TestEnriquecimentoSequencial(Base):
    """Caminho para provedor sem API de lote: precisa ser retomavel."""

    def setUp(self):
        Base.setUp(self)
        self._gerar = M.gerar_json
        M.gravar_json(M.CONTRATOS, {})

    def tearDown(self):
        M.gerar_json = self._gerar
        Base.tearDown(self)

    def _contrato(self, tid):
        return {"funcao": "f", "quando_usar": ["q"], "tom": ["t"],
                "slots": [{"box_id": b["id"], "papel": "p", "exemplo": "e",
                           "max_chars": 40}
                          for b in self.catalogo[tid]["textBoxes"]],
                "nsfw": False}

    def test_grava_a_cada_template(self):
        vistos = []

        def falso(sistema, texto, schema, papel, imagem=None, modelo=None,
                  max_tokens=4000, cachear_sistema=False):
            tid = "drake" if "Drake" in texto else "botoes"
            vistos.append(tid)
            # ja gravado antes de a proxima chamada acontecer
            if len(vistos) == 2:
                self.assertIn(vistos[0], M.ler_json(M.CONTRATOS, {}))
            return self._contrato(tid), {"input": 1, "output": 1}
        M.gerar_json = falso

        novos = M.enriquecer_sequencial(self.catalogo, ["drake", "botoes"], pausa=0)
        self.assertEqual(sorted(novos), ["botoes", "drake"])
        gravados = M.ler_json(M.CONTRATOS, {})
        self.assertEqual(gravados["drake"]["source_hash"],
                         self.catalogo["drake"]["_hash"])

    def test_cota_estourada_para_e_preserva_o_feito(self):
        def falso(sistema, texto, schema, papel, imagem=None, modelo=None,
                  max_tokens=4000, cachear_sistema=False):
            if "Drake" in texto:
                return self._contrato("drake"), {"input": 1, "output": 1}
            raise RuntimeError("Gemini respondeu 429: quota exceeded")
        M.gerar_json = falso

        novos = M.enriquecer_sequencial(self.catalogo, ["drake", "botoes"], pausa=0)
        self.assertEqual(list(novos), ["drake"])
        # o que passou ficou gravado, e o que falta continua pendente
        self.assertEqual(list(M.ler_json(M.CONTRATOS, {})), ["drake"])
        self.assertEqual(M.contratos_pendentes(self.catalogo,
                                               M.ler_json(M.CONTRATOS, {})),
                         ["botoes"])

    def test_erro_comum_nao_derruba_os_outros(self):
        def falso(sistema, texto, schema, papel, imagem=None, modelo=None,
                  max_tokens=4000, cachear_sistema=False):
            if "Drake" in texto:
                raise RuntimeError("Gemini respondeu 400: schema invalido")
            return self._contrato("botoes"), {"input": 1, "output": 1}
        M.gerar_json = falso

        novos = M.enriquecer_sequencial(self.catalogo, ["drake", "botoes"], pausa=0)
        self.assertEqual(list(novos), ["botoes"])


class TestErroGemini(unittest.TestCase):
    """403 do Google e ambiguo: pode ser permissao de verdade ou limite de taxa
    disfarcado. So o motivo dentro do corpo decide se vale repetir."""

    def test_detalhe_traz_status_e_motivo(self):
        corpo = json.dumps({"error": {
            "code": 403, "status": "PERMISSION_DENIED",
            "message": "The caller does not have permission",
            "details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}})
        detalhe, status = M._detalhe_gemini(corpo)
        self.assertIn("PERMISSION_DENIED", detalhe)
        self.assertIn("RATE_LIMIT_EXCEEDED", detalhe)   # o que estava escondido
        self.assertEqual(status, "PERMISSION_DENIED")

    def test_detalhe_sobrevive_a_corpo_nao_json(self):
        detalhe, status = M._detalhe_gemini("<html>502 Bad Gateway</html>")
        self.assertIn("502", detalhe)
        self.assertEqual(status, "")

    def test_403_por_limite_de_taxa_e_repetivel(self):
        self.assertTrue(M._e_temporario(403, "PERMISSION_DENIED (RATE_LIMIT_EXCEEDED)"))
        self.assertTrue(M._e_temporario(403, "Quota exceeded for requests"))

    def test_403_de_permissao_real_nao_e_repetivel(self):
        self.assertFalse(M._e_temporario(
            403, "PERMISSION_DENIED API key not valid for this project"))

    def test_429_e_5xx_sao_repetiveis(self):
        self.assertTrue(M._e_temporario(429, "too many requests"))
        self.assertTrue(M._e_temporario(503, "service unavailable"))

    def test_400_nao_e_repetivel(self):
        self.assertFalse(M._e_temporario(400, "invalid schema"))

    def test_cota_diaria_para_o_lote(self):
        self.assertTrue(M._cota_esgotada(
            "Gemini respondeu 429: Quota exceeded for quota metric requests per day"))
        self.assertTrue(M._cota_esgotada("RESOURCE_EXHAUSTED daily limit reached"))

    def test_limite_por_minuto_nao_para_o_lote(self):
        """O retry ja absorve isso; parar seria desistir cedo demais."""
        self.assertFalse(M._cota_esgotada(
            "Gemini respondeu 429: rate limit exceeded, retry in 20s"))
        self.assertFalse(M._cota_esgotada("Gemini respondeu 400: schema invalido"))


class TestRetryGemini(Base):

    def setUp(self):
        Base.setUp(self)
        self._antes = dict((k, os.environ.get(k)) for k in
                           ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "MEMEGEN_PROVIDER"))
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["GEMINI_API_KEY"] = "k"
        os.environ["MEMEGEN_PROVIDER"] = "gemini"
        self._urlopen, self._sleep = M.urlopen, M.time.sleep
        M.time.sleep = lambda s: None          # sem espera real nos testes

    def tearDown(self):
        M.urlopen, M.time.sleep = self._urlopen, self._sleep
        for k, v in self._antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        Base.tearDown(self)

    def _falhar_n_vezes(self, n, codigo, corpo):
        estado = {"n": 0}

        def falso(req, timeout=None):
            estado["n"] += 1
            if estado["n"] <= n:
                raise HTTPError(req.get_full_url(), codigo, "erro", {},
                                io.BytesIO(corpo.encode()))
            return RespostaFalsa({"output_text": '{"ids": []}'})
        M.urlopen = falso
        return estado

    def test_repete_e_se_recupera_do_403_por_taxa(self):
        corpo = json.dumps({"error": {"code": 403, "status": "PERMISSION_DENIED",
                                      "message": "rate limit exceeded"}})
        estado = self._falhar_n_vezes(2, 403, corpo)
        M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual(estado["n"], 3)       # duas falhas e um sucesso

    def test_nao_repete_permissao_real(self):
        corpo = json.dumps({"error": {"code": 403, "status": "PERMISSION_DENIED",
                                      "message": "API key not valid"}})
        estado = self._falhar_n_vezes(9, 403, corpo)
        with self.assertRaises(RuntimeError):
            M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual(estado["n"], 1)       # falhou uma vez e desistiu

    def test_desiste_depois_do_limite_de_tentativas(self):
        corpo = json.dumps({"error": {"code": 429, "message": "rate limit"}})
        estado = self._falhar_n_vezes(99, 429, corpo)
        with self.assertRaises(RuntimeError):
            M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual(estado["n"], 4)

    def test_erro_de_rede_tambem_repete(self):
        estado = {"n": 0}

        def falso(req, timeout=None):
            estado["n"] += 1
            if estado["n"] == 1:
                raise URLError("conexao caiu")
            return RespostaFalsa({"output_text": "{}"})
        M.urlopen = falso
        M.gerar_json("s", "t", M.ESQUEMA_TRIAGEM, "triar")
        self.assertEqual(estado["n"], 2)
