"""Generate the application icon (no designer tool needed).

Redraws ``packaging/icons/app.png`` (256px master) and ``app.ico``
(multi-size Windows icon) with Pillow primitives only — no font files, so the
result is byte-reproducible on any machine:

* teal rounded square (the product colour used in the desktop shell),
* a white photo card with a folded corner,
* teal mountains + sun inside the card (a space *of pictures* manager).

Usage::

    python packaging/make_icon.py

The ``.spec`` points the frozen executable at ``app.ico``; the running window
loads ``app.png`` from the bundled ``icons/`` data directory.
"""

from __future__ import annotations

from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent / "icons"
MASTER = ICON_DIR / "app.png"
WINDOWS_ICON = ICON_DIR / "app.ico"

SIZE = 256
TEAL = (15, 118, 110, 255)
WHITE = (255, 255, 255, 255)
TEAL_DARK = (13, 92, 86, 255)


def draw():
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)

    # Teal rounded-square tile.
    canvas.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=52, fill=TEAL)

    # White photo card, slightly rotated by construction (offset corners).
    card = [56, 72, 200, 196]
    canvas.rounded_rectangle(card, radius=14, fill=WHITE)
    # Folded corner.
    canvas.polygon([(200, 72), (200, 104), (168, 72)], fill=TEAL_DARK)
    # Sun.
    canvas.ellipse([150, 100, 172, 122], fill=TEAL)
    # Mountains.
    canvas.polygon([(64, 188), (112, 128), (150, 176), (132, 188)], fill=TEAL)
    canvas.polygon([(120, 188), (160, 140), (196, 188)], fill=TEAL_DARK)
    return image


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    master = draw()
    master.save(MASTER, format="PNG")
    master.save(
        WINDOWS_ICON,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"[icon] wrote {MASTER} and {WINDOWS_ICON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
