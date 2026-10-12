from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "benchmarks" / "results" / "overlay-samples.png"


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Microsoft YaHei first: PIL does no font fallback, and Segoe UI lacks CJK
    # glyphs, so CJK labels (转录已取消 / 撤销) render as tofu boxes unless the
    # base font itself carries the glyphs. YaHei covers both Latin and CJK.
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def rounded_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: str) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def draw_recording(canvas: Image.Image, offset: tuple[int, int], width: int = 176, height: int = 64, frame: int = 6) -> None:
    draw = ImageDraw.Draw(canvas)
    ox, oy = offset
    pill_h = 52
    pill_top = oy + (height - pill_h) // 2
    pill = (ox, pill_top, ox + width, pill_top + pill_h)
    rounded_rect(draw, (pill[0] + 1, pill[1] + 2, pill[2] + 1, pill[3] + 2), pill_h // 2, "#353535")
    rounded_rect(draw, pill, pill_h // 2, "#000000")
    cy = (pill[1] + pill[3]) // 2
    button_d = 38
    left_btn = (pill[0] + 6, cy - button_d // 2, pill[0] + 6 + button_d, cy + button_d // 2)
    right_btn = (pill[2] - 6 - button_d, cy - button_d // 2, pill[2] - 6, cy + button_d // 2)
    rounded_rect(draw, left_btn, button_d // 2, "#404040")
    rounded_rect(draw, right_btn, button_d // 2, "#fbfbfb")
    lx = (left_btn[0] + left_btn[2]) // 2
    rx = (right_btn[0] + right_btn[2]) // 2
    draw.line((lx - 9, cy - 9, lx + 9, cy + 9), fill="#f5f5f5", width=3)
    draw.line((lx + 9, cy - 9, lx - 9, cy + 9), fill="#f5f5f5", width=3)
    draw.line((rx - 11, cy, rx - 3, cy + 8, rx + 13, cy - 10), fill="#111111", width=3, joint="curve")
    start_x = pill[0] + 58
    end_x = pill[2] - 58
    dot_count = 12
    gap = (end_x - start_x) / (dot_count - 1)
    for i in range(dot_count):
        phase = frame / 5.0 + i * 0.55
        brightness = int(112 + 76 * (0.5 + 0.5 * math.sin(phase)))
        color = f"#{brightness:02x}{brightness:02x}{brightness:02x}"
        x = int(start_x + i * gap)
        draw.ellipse((x - 2, cy - 2, x + 2, cy + 2), fill=color)


def draw_thinking(canvas: Image.Image, offset: tuple[int, int], width: int = 142, height: int = 64) -> None:
    draw = ImageDraw.Draw(canvas)
    ox, oy = offset
    pill_h = 52
    pill_top = oy + (height - pill_h) // 2
    pill = (ox, pill_top, ox + width, pill_top + pill_h)
    rounded_rect(draw, (pill[0] + 1, pill[1] + 2, pill[2] + 1, pill[3] + 2), pill_h // 2, "#353535")
    rounded_rect(draw, pill, pill_h // 2, "#3b3b3b")
    text = "Thinking"
    fnt = font(15, bold=True)
    text_box = draw.textbbox((0, 0), text, font=fnt)
    tx = ox + width / 2 - (text_box[2] - text_box[0]) / 2
    ty = oy + height / 2 - (text_box[3] - text_box[1]) / 2 - 1
    draw.text((tx, ty), text, fill="#9c9c9c", font=fnt)


def draw_cancelled(canvas: Image.Image, offset: tuple[int, int], width: int = 260, height: int = 64) -> None:
    draw = ImageDraw.Draw(canvas)
    ox, oy = offset
    toast_h = 58
    top = oy + (height - toast_h) // 2
    box = (ox, top, ox + width, top + toast_h)
    rounded_rect(draw, (box[0] + 4, box[1] + 4, box[2] + 4, box[3] + 4), 14, "#333333")
    rounded_rect(draw, box, 14, "#191718")
    cy = (box[1] + box[3]) // 2
    ix = box[0] + 28
    draw.ellipse((ix - 9, cy - 9, ix + 9, cy + 9), outline="#7287f4", width=2)
    draw.text((ix - 2, cy - 9), "i", fill="#7287f4", font=font(12, True))
    draw.text((box[0] + 56, cy - 11), "转录已取消", fill="#ffffff", font=font(15, True))
    btn = (box[2] - 82, cy - 20, box[2] - 10, cy + 20)
    rounded_rect(draw, btn, 20, "#404040")
    label = "撤销"
    fnt = font(14, True)
    text_box = draw.textbbox((0, 0), label, font=fnt)
    draw.text(((btn[0] + btn[2]) / 2 - (text_box[2] - text_box[0]) / 2, cy - 11), label, fill="#ffffff", font=fnt)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render synthetic overlay states, not desktop screenshots.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    canvas = Image.new("RGB", (660, 148), "#f5f5f5")
    draw = ImageDraw.Draw(canvas)
    draw.text((24, 14), "Recording: click X / check", fill="#111111", font=font(13, bold=False))
    draw.text((250, 14), "Processing: always Thinking", fill="#111111", font=font(13, bold=False))
    draw.text((430, 14), "Cancelled + Undo", fill="#111111", font=font(13, bold=False))
    draw_recording(canvas, (24, 56))
    draw_thinking(canvas, (260, 56))
    draw_cancelled(canvas, (392, 56))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
