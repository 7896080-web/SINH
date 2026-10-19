"""Значок AWER для ярлыка админки (scripts/awer.ico): белая «A» с оранжевой полоской на тёмно-синем квадрате.
Запускать у разработчика (нужен Pillow): python scripts/make_icon.py scripts/awer.ico — рядом пишется preview.png."""
import sys
from PIL import Image, ImageDraw, ImageFont
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG, FG, ACC = (30, 42, 58, 255), (255, 255, 255, 255), (242, 163, 58, 255)

def draw(size):
    S = size * 8
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = 0 if size <= 24 else S // 32
    d.rounded_rectangle([pad, pad, S - 1 - pad, S - 1 - pad], radius=S // 5, fill=BG,
                        outline=(255, 255, 255, 70) if size >= 32 else None, width=max(S // 64, 8))
    small = size <= 24
    fs = int(S * (0.78 if small else 0.62))
    f = ImageFont.truetype(FONT, fs)
    l, t, r, b = d.textbbox((0, 0), "A", font=f)
    w, h = r - l, b - t
    top = (S - h) / 2 - (S * 0.06 if not small else S * 0.04)
    d.text(((S - w) / 2 - l, top - t), "A", font=f, fill=FG)
    # акцент — полоска под буквой (план / рост)
    bw, bh = int(w * 0.9), max(int(S * (0.09 if small else 0.065)), 8)
    y = top + h + S * (0.035 if not small else 0.02)
    y = min(y, S - pad - S * 0.1 - bh)
    d.rounded_rectangle([(S - bw) / 2, y, (S + bw) / 2, y + bh], radius=bh // 2, fill=ACC)
    return im.resize((size, size), Image.LANCZOS)

sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [draw(s) for s in sizes]
imgs[-1].save(sys.argv[1], format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
# превью: все размеры на светлом и тёмном фоне
W = sum(sizes) + 20 * len(sizes) + 20
pv = Image.new("RGBA", (W, 2 * 280), (245, 245, 245, 255))
pv.paste(Image.new("RGBA", (W, 280), (12, 44, 84, 255)), (0, 280))
x = 20
for s, im in zip(sizes, imgs):
    pv.alpha_composite(im, (x, 140 - s // 2)); pv.alpha_composite(im, (x, 420 - s // 2)); x += s + 20
pv.save(str(__import__("pathlib").Path(sys.argv[1]).with_name("preview.png")))
