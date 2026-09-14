"""Baixa todas as imagens de template, com concorrência baixa e retry.

Necessário para `tools/validate_render.py` e para gerar memes offline. O
`memegen make` baixa sob demanda, então isto só é preciso para validação em
massa ou uso sem rede.
"""
import concurrent.futures as cf
import time

from memegen import catalog as C
from memegen import config, source

config.ensure_dirs()
cat = C.load_catalog()


def pendentes():
    return [(tid, t) for tid, t in cat.items()
            if not (config.TEMPLATE_IMAGES / f"{tid}.jpg").exists()]


def get(item):
    tid, t = item
    dest = config.TEMPLATE_IMAGES / f"{tid}.jpg"
    for tentativa in range(4):
        try:
            source.download_image(t["url"], dest)
            return None
        except Exception as e:  # noqa: BLE001
            if tentativa == 3:
                return (tid, type(e).__name__)
            time.sleep(2 ** tentativa)
    return None


if __name__ == "__main__":
    for rodada in range(1, 7):
        falta = pendentes()
        if not falta:
            break
        print(f"rodada {rodada}: {len(falta)} a baixar", flush=True)
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            erros = [r for r in ex.map(get, falta) if r]
        print(f"  {len(erros)} erros", flush=True)

    falta = pendentes()
    print(f"\nem disco: {len(cat) - len(falta)}/{len(cat)}")
