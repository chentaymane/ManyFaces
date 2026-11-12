"""Build the ManyFaces dashboard logo and Windows icon with Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw


def build() -> Image.Image:
    scale = 8
    image = Image.new("RGBA", (64 * scale, 64 * scale))
    draw = ImageDraw.Draw(image)

    def box(*coords: int) -> tuple[int, ...]:
        return tuple(coord * scale for coord in coords)

    draw.rounded_rectangle(box(0, 0, 63, 63), radius=15 * scale, fill="#242043")
    draw.rounded_rectangle(box(22, 13, 50, 44), radius=8 * scale, fill="#7867c9")
    draw.rounded_rectangle(box(13, 20, 42, 51), radius=8 * scale, fill="#d2c7ff")
    draw.ellipse(box(20, 30, 24, 34), fill="#242043")
    draw.ellipse(box(31, 30, 35, 34), fill="#242043")
    draw.rounded_rectangle(box(23, 40, 32, 42), radius=scale, fill="#242043")
    return image


def main() -> None:
    assets = Path(__file__).resolve().parent
    image = build()
    icon = image.resize((256, 256), Image.Resampling.LANCZOS)
    icon.save(assets / "icon.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    image.resize((128, 128), Image.Resampling.LANCZOS).save(assets.parent / "web" / "logo.png", optimize=True)


if __name__ == "__main__":
    main()
