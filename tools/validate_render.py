"""Renderiza os 836 templates e verifica se o texto fica dentro da caixa.

Não basta não quebrar: o texto pintado precisa cair dentro do retângulo que o
9GAG declarou. Renderiza cada caixa isolada sobre fundo transparente e mede a
extensão real da tinta.
"""
import sys
from PIL import Image
from memegen import catalog as C, config
from memegen.render import render

cat = C.load_catalog()
TEXTOS = ["Texto de teste um", "Segundo texto", "Terceiro", "Quarto", "Quinto",
          "Sexto", "Setimo", "Oitavo"]

falhas, vazamentos, ok = [], [], 0
for n, (tid, t) in enumerate(sorted(cat.items()), 1):
    img_path = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    if not img_path.exists():
        falhas.append((tid, "imagem ausente")); continue
    try:
        base = Image.open(img_path).convert("RGB")
        scale = base.width / config.CANVAS_WIDTH
        for i, box in enumerate(t["textBoxes"]):
            # renderiza SÓ esta caixa, sobre fundo preto uniforme
            branco = Image.new("RGB", base.size, (0, 0, 0))
            branco.save("/tmp/_blank.jpg", quality=95)
            saida = render(t, {box["id"]: TEXTOS[i % len(TEXTOS)]}, "/tmp/_blank.jpg")

            bbox = saida.convert("L").point(lambda p: 255 if p > 12 else 0).getbbox()
            if bbox is None:
                falhas.append((tid, f"{box['id']}: nada foi pintado")); continue

            # limites esperados, com folga para o contorno
            folga = max(4, box.get("strokeWidth", 0) * scale)
            x0 = box["x"] * scale - folga
            y0 = box["y"] * scale - folga
            x1 = (box["x"] + box["width"]) * scale + folga
            y1 = (box["y"] + box["height"]) * scale + folga
            if box.get("rotation"):
                continue  # caixa girada tem extensão maior por construção
            if bbox[0] < x0 or bbox[1] < y0 or bbox[2] > x1 or bbox[3] > y1:
                vazamentos.append((tid, box["id"], bbox,
                                   (round(x0), round(y0), round(x1), round(y1))))
        ok += 1
    except Exception as e:
        falhas.append((tid, f"{type(e).__name__}: {e}"))
    if n % 200 == 0:
        print(f"  {n}/{len(cat)}", flush=True)

print(f"\nrenderizados sem erro : {ok}/{len(cat)}")
print(f"falhas                : {len(falhas)}")
print(f"texto fora da caixa   : {len(vazamentos)}")
for f in falhas[:10]: print("   FALHA:", f)
for v in vazamentos[:10]: print("   VAZOU:", v)
sys.exit(1 if (falhas or vazamentos) else 0)
