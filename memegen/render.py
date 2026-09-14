"""Renderização das caixas de texto sobre a imagem do template.

As coordenadas que o 9GAG guarda em `textBoxes` não estão em pixels da imagem:
estão num canvas de 640 px de largura e altura proporcional. A conversão para
pixels é uma única escala, `largura_da_imagem / 640`, aplicada a posição,
tamanho de fonte e espessura do contorno.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import config

# Impact é a fonte canônica de meme (94% das caixas do catálogo). Não é
# redistribuível, então procuramos no sistema e caímos para um sans bold.
_FONT_CANDIDATES = {
    "impact": [
        config.FONTS / "Impact.ttf",
        config.FONTS / "impact.ttf",
        Path("/usr/share/fonts/truetype/msttcorefonts/Impact.ttf"),
        Path("/Library/Fonts/Impact.ttf"),
        Path("C:/Windows/Fonts/impact.ttf"),
    ],
    "arial": [
        Path("/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ],
    "helvetica": [],
    "comic sans ms": [
        Path("/usr/share/fonts/truetype/msttcorefonts/Comic_Sans_MS.ttf"),
        Path("C:/Windows/Fonts/comic.ttf"),
    ],
}

_FALLBACKS = [
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_warned: set[str] = set()


def _font_file(family: str) -> Path:
    for p in _FONT_CANDIDATES.get(family.lower(), []):
        if p.exists():
            return p
    for p in _FALLBACKS:
        if p.exists():
            if family.lower() not in _warned:
                _warned.add(family.lower())
                print(
                    f"  aviso: fonte '{family}' não encontrada, usando {p.name}. "
                    f"Para o visual original, coloque Impact.ttf em {config.FONTS}/"
                )
            return p
    raise FileNotFoundError("nenhuma fonte TrueType disponível no sistema")


def _font(family: str, size: int) -> ImageFont.FreeTypeFont:
    key = (family.lower(), max(1, size))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(_font_file(family)), max(1, size))
    return _font_cache[key]


@dataclass
class _Fitted:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    line_height: int


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: float, draw) -> list[str] | None:
    """Quebra o texto respeitando a largura. None se alguma palavra não couber."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        if draw.textlength(cur, font=font) > max_w:
            return None
        for w in words[1:]:
            cand = f"{cur} {w}"
            if draw.textlength(cand, font=font) <= max_w:
                cur = cand
            else:
                lines.append(cur)
                if draw.textlength(w, font=font) > max_w:
                    return None
                cur = w
        lines.append(cur)
    return lines


def _fit(text: str, box_w: float, box_h: float, family: str, start: int,
         auto: bool, draw) -> _Fitted:
    """Maior corpo de fonte em que o texto quebrado cabe na caixa."""
    line_gap = 1.15

    def try_size(size: int) -> _Fitted | None:
        f = _font(family, size)
        lines = _wrap(text, f, box_w, draw)
        if lines is None:
            return None
        lh = int(size * line_gap)
        if lh * len(lines) > box_h:
            return None
        return _Fitted(lines, f, lh)

    if not auto:
        return try_size(start) or _Fitted([text], _font(family, start), int(start * line_gap))

    lo, hi, best = 4, max(8, int(box_h)), None
    while lo <= hi:
        mid = (lo + hi) // 2
        got = try_size(mid)
        if got:
            best, lo = got, mid + 1
        else:
            hi = mid - 1
    return best or _Fitted([text], _font(family, 4), 5)


def render(
    template: dict,
    texts: dict[str, str] | list[str],
    image_path: Path,
    out_path: Path | None = None,
) -> Image.Image:
    """Compõe os textos sobre a imagem do template.

    `texts` aceita um dicionário {box_id: texto} ou uma lista na ordem das
    caixas. Caixas sem texto correspondente ficam vazias.
    """
    img = Image.open(image_path).convert("RGB")
    boxes = template["textBoxes"]

    if isinstance(texts, list):
        texts = {b["id"]: t for b, t in zip(boxes, texts)}

    # Uma única escala converte canvas -> pixels. A imagem em disco pode ter sido
    # reamostrada, então derivamos a escala da largura real e não da declarada.
    scale = img.width / config.CANVAS_WIDTH

    for box in boxes:
        raw = (texts or {}).get(box["id"], "")
        if not raw or not raw.strip():
            continue
        if box.get("capitalize"):
            raw = raw.upper()

        bx, by = box["x"] * scale, box["y"] * scale
        bw, bh = box["width"] * scale, box["height"] * scale

        # camada própria por caixa: permite rotacionar sem afetar as outras
        pad = int(max(bw, bh) * 0.5) + 8
        layer = Image.new("RGBA", (int(bw) + pad * 2, int(bh) + pad * 2), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)

        fitted = _fit(
            raw, bw, bh,
            box.get("fontFamily", "impact"),
            max(1, int(box.get("fontSize", 50) * scale)),
            box.get("autoFontSize", True),
            ld,
        )

        stroke = max(0, round(box.get("strokeWidth", 0) * scale * 0.5))
        style = box.get("outlineStyle", "stroke")
        fill = box.get("color", "#ffffff")
        stroke_fill = box.get("strokeColor", "#000000")

        total_h = fitted.line_height * len(fitted.lines)
        baseline = box.get("textBaseline", "middle")
        if baseline == "top":
            y = pad
        elif baseline == "bottom":
            y = pad + bh - total_h
        else:
            y = pad + (bh - total_h) / 2

        align = box.get("textAlign", "center")
        for line in fitted.lines:
            lw = ld.textlength(line, font=fitted.font)
            if align == "left":
                x = pad
            elif align == "right":
                x = pad + bw - lw
            else:
                x = pad + (bw - lw) / 2

            if style == "shadow":
                off = max(1, round(2 * scale))
                ld.text((x + off, y + off), line, font=fitted.font, fill=stroke_fill)
                ld.text((x, y), line, font=fitted.font, fill=fill)
            else:
                ld.text(
                    (x, y), line, font=fitted.font, fill=fill,
                    stroke_width=stroke, stroke_fill=stroke_fill,
                )
            y += fitted.line_height

        rot = box.get("rotation") or 0
        if rot:
            # o 9GAG grava a rotação em graus no sentido horário
            layer = layer.rotate(-rot, resample=Image.BICUBIC, center=(layer.width / 2, layer.height / 2))

        img.paste(layer, (int(bx) - pad, int(by) - pad), layer)

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, quality=92)
    return img
