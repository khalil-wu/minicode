from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


BUILD_DIR = Path(__file__).resolve().parents[1] / "build"
PNG_PATH = BUILD_DIR / "icon.png"
ICO_PATH = BUILD_DIR / "icon.ico"
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def create_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)

    margin = max(1, round(size * 0.08))
    radius = round(size * 0.24)
    offset = round(size * 0.02)
    shadow_draw.rounded_rectangle(
        (margin, margin + offset, size - margin, size - margin + offset),
        radius=radius,
        fill=(15, 23, 42, 52),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(1, round(size * 0.04))))
    image.alpha_composite(shadow)

    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base_draw = ImageDraw.Draw(base)
    for y in range(size):
        ratio = y / max(1, size - 1)
        red = round(15 + (53 - 15) * ratio)
        green = round(23 + (87 - 23) * ratio)
        blue = round(42 + (255 - 42) * ratio)
        base_draw.line((0, y, size, y), fill=(red, green, blue, 255))

    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius,
        fill=255,
    )
    rounded_base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded_base.paste(base, (0, 0), mask)
    image.alpha_composite(rounded_base)

    detail = ImageDraw.Draw(image)
    stroke = max(2, round(size * 0.09))
    left = round(size * 0.26)
    right = round(size * 0.74)
    top = round(size * 0.28)
    bottom = round(size * 0.72)
    mid_x = round(size * 0.5)
    mid_y = round(size * 0.57)
    detail.line(
        [(left, bottom), (left, top), (mid_x, mid_y), (right, top), (right, bottom)],
        fill=(248, 250, 252, 255),
        width=stroke,
        joint="curve",
    )
    dot_radius = max(2, round(size * 0.06))
    dot_x = round(size * 0.73)
    dot_y = round(size * 0.72)
    detail.ellipse(
        (dot_x - dot_radius, dot_y - dot_radius, dot_x + dot_radius, dot_y + dot_radius),
        fill=(248, 250, 252, 255),
    )

    return image


def main() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    largest = create_icon(256)
    largest.save(PNG_PATH)
    largest.save(ICO_PATH, sizes=[(size, size) for size in ICON_SIZES])


if __name__ == "__main__":
    main()
