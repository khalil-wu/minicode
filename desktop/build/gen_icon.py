"""Generate MiniCode desktop icon — modern terminal prompt on deep gradient."""
from PIL import Image, ImageDraw, ImageFilter
import math

SIZE = 512
RADIUS = 108

def make_gradient(size):
    """Create a deep indigo-to-purple radial gradient background."""
    img = Image.new("RGB", (size, size))
    pixels = img.load()
    cx, cy = size / 2, size / 2
    for y in range(size):
        for x in range(size):
            dx = (x - cx) / cx
            dy = (y - cy) / cy
            dist = min(math.sqrt(dx * dx + dy * dy), 1.0)
            # Center: rich indigo (48, 30, 130) → Edge: deep navy (18, 10, 60)
            r = int(48 * (1 - dist) + 18 * dist)
            g = int(30 * (1 - dist) + 10 * dist)
            b = int(130 * (1 - dist) + 60 * dist)
            pixels[x, y] = (r, g, b)
    return img


def rounded_rect_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def draw_symbol(draw, size):
    """Draw a stylized '> _' terminal prompt."""
    sw = 30  # stroke width
    cx, cy = size // 2, size // 2

    # Chevron ">"
    tip_x = 280
    left_x = 145
    top_y = 155
    bot_y = size - 155

    # Glow layer (drawn slightly larger, blurred later)
    cyan = (100, 210, 255)
    white = (255, 255, 255)

    # Draw chevron lines
    draw.line([(left_x, top_y), (tip_x, cy)], fill=cyan, width=sw)
    draw.line([(left_x, bot_y), (tip_x, cy)], fill=cyan, width=sw)
    # Round caps
    r = sw // 2
    for px, py in [(left_x, top_y), (tip_x, cy), (left_x, bot_y)]:
        draw.ellipse([px - r, py - r, px + r, py + r], fill=cyan)

    # Cursor line "_"
    cur_left = 310
    cur_right = 410
    cur_y = cy + 55
    draw.rounded_rectangle(
        [cur_left, cur_y, cur_right, cur_y + sw],
        radius=sw // 3,
        fill=white,
    )

    # Blinking dot
    dot_cx = 435
    dot_r = 12
    draw.ellipse(
        [dot_cx - dot_r, cur_y + sw // 2 - dot_r, dot_cx + dot_r, cur_y + sw // 2 + dot_r],
        fill=cyan,
    )


def main():
    # Background gradient
    bg = make_gradient(SIZE)

    # Apply rounded rectangle mask
    mask = rounded_rect_mask(SIZE, RADIUS)
    bg.putalpha(mask)

    # Draw glow layer (blurred symbol underneath for soft glow)
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    draw_symbol(glow_draw, SIZE)
    glow_blurred = glow.filter(ImageFilter.GaussianBlur(radius=12))

    # Composite: bg + glow + sharp symbol
    result = bg.copy()
    result = Image.alpha_composite(result, glow_blurred)

    sharp = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    sharp_draw = ImageDraw.Draw(sharp)
    draw_symbol(sharp_draw, SIZE)
    result = Image.alpha_composite(result, sharp)

    # Save PNG
    out_png = r"C:\Desktop\MiniCode\desktop\build\icon.png"
    result.save(out_png, "PNG")
    print(f"Saved {out_png} ({SIZE}x{SIZE})")

    # Generate ICO with multiple sizes
    ico_sizes = [256, 128, 64, 48, 32, 16]
    frames = [result.resize((s, s), Image.LANCZOS) for s in ico_sizes]
    out_ico = r"C:\Desktop\MiniCode\desktop\build\icon.ico"
    frames[0].save(out_ico, format="ICO", sizes=[(s, s) for s in ico_sizes], append_images=frames[1:])
    print(f"Saved {out_ico} (sizes: {ico_sizes})")


if __name__ == "__main__":
    main()
