#!/usr/bin/env python3
"""Generate the brand images of the Rotel integration.

Home Assistant serves the images of a custom integration from
``custom_components/<domain>/brand/`` (2026.3 and newer) and HACS expects the
same directory, so both consumers read the files this script writes.

The artwork is drawn from scratch instead of shipping the Rotel trademark: a
front panel with a volume knob, which is what the integration controls. Run it
after changing the palette or the layout::

    python tools/make_brand_images.py

Only Pillow is required. The generated files are committed, so running this is
never needed to install the integration.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "rotel_control" / "brand"

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# Gold reads well on the light and the dark themes; the secondary colour is
# only used for the subtitle of the logo.
LIGHT_PRIMARY = (176, 141, 87, 255)
LIGHT_SECONDARY = (110, 110, 115, 255)
DARK_PRIMARY = (232, 217, 188, 255)
DARK_SECONDARY = (168, 170, 178, 255)

# Sizes required by the Home Assistant brand rules: ``icon`` is a square of
# 256 px, ``@2x`` variants are twice as large, and a logo is landscape with
# its short side in the same range as the icon.
SIZES = {
    "icon": (256, 256),
    "icon@2x": (512, 512),
    "logo": (512, 256),
    "logo@2x": (1024, 512),
}


def _draw_mark(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], colour) -> None:
    """Draw the amplifier mark into ``box``, preserving the aspect ratio."""
    left, top, right, bottom = box
    side = min(right - left, bottom - top)
    scale = side / 256
    origin_x = left + ((right - left) - side) / 2
    origin_y = top + ((bottom - top) - side) / 2

    def point(x: float, y: float) -> tuple[float, float]:
        return origin_x + x * scale, origin_y + y * scale

    stroke = max(2, round(11 * scale))
    draw.rounded_rectangle(
        [*point(14, 58), *point(242, 198)],
        radius=18 * scale,
        outline=colour,
        width=stroke,
    )
    knob = 38 * scale
    knob_x, knob_y = point(148, 128)
    draw.ellipse(
        [knob_x - knob, knob_y - knob, knob_x + knob, knob_y + knob],
        outline=colour,
        width=stroke,
    )
    draw.line(
        [knob_x, knob_y, knob_x, knob_y - knob + 11 * scale],
        fill=colour,
        width=stroke,
    )
    small = 13 * scale
    for offset in (100, 156):
        centre_x, centre_y = point(58, offset)
        draw.ellipse(
            [centre_x - small, centre_y - small, centre_x + small, centre_y + small],
            outline=colour,
            width=max(2, stroke - 2),
        )


def _render_icon(size: int, colour) -> Image.Image:
    """Return the square icon at ``size`` pixels."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_mark(draw, (0, 0, size, size), colour)
    return image


def _render_logo(size: tuple[int, int], primary, secondary) -> Image.Image:
    """Return the landscape logo with the mark and the name of the integration."""
    width, height = size
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    mark = round(height * 0.72)
    _draw_mark(draw, (0, 0, mark, height), primary)

    title = ImageFont.truetype(FONT_BOLD, round(height * 0.30))
    subtitle = ImageFont.truetype(FONT_REGULAR, round(height * 0.13))
    text_x = mark + round(height * 0.10)
    draw.text(
        (text_x, height * 0.26),
        "Rotel",
        font=title,
        fill=primary,
        anchor="lm",
    )
    draw.text(
        (text_x + 2, height * 0.62),
        "Amplifier Control",
        font=subtitle,
        fill=secondary,
        anchor="lm",
    )
    return image


def main() -> None:
    """Write every brand image, both light and dark, into the brand folder."""
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for prefix, primary, secondary in (
        ("", LIGHT_PRIMARY, LIGHT_SECONDARY),
        ("dark_", DARK_PRIMARY, DARK_SECONDARY),
    ):
        for name, size in SIZES.items():
            if name.startswith("icon"):
                image = _render_icon(size[0], primary)
            else:
                image = _render_logo(size, primary, secondary)
            path = BRAND_DIR / f"{prefix}{name}.png"
            image.save(path, "PNG", optimize=True)
            print(f"{path.relative_to(REPO_ROOT)} {image.size[0]}x{image.size[1]}")


if __name__ == "__main__":
    main()