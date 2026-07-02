"""
Generate an IMAGE-based catering menu PDF (a decorative menu rendered to an image, with
NO text layer) — so PyMuPDF's get_text() returns nothing and ONLY a vision-LLM can read it.

Contrast with make_sample_menu.py, which is text-based (both text-parse and vision work).
Use this to demo that the vision model is genuinely required, not reading a hidden text layer.

    ../capstone/.venv/bin/python menus/make_image_menu.py
"""

from pathlib import Path
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H = 1000, 1360

CATERER = "Fireside Grill & Catering"
TAGLINE = "Austin, TX   ·   Wood-fired catering for events & offices"
SECTIONS = [
    ("STARTERS", [
        ("Smoked Brisket Sliders", "Slow-smoked brisket, pickles and chipotle aioli on brioche. Serves 10.", 78),
        ("Charred Street Corn", "Grilled corn, cotija, lime and chili butter. Vegetarian. Serves 12.", 54),
        ("Loaded Nacho Bar", "Tortilla chips, queso, jalapenos and pico de gallo. Serves 20.", 88),
    ]),
    ("MAINS", [
        ("Texas Brisket Platter", "14-hour oak-smoked brisket with pickles and Texas toast. Serves 15.", 210),
        ("Grilled Chicken Fajitas", "Marinated chicken, peppers, onions and warm tortillas. Serves 12.", 156),
        ("Smoked Portobello Bowl", "Portobello, quinoa, black beans and avocado. Vegan, gluten-free. Serves 10.", 120),
    ]),
    ("DESSERTS", [
        ("Pecan Pie Bites", "Buttery mini pecan tarts. Contains nuts. Serves 24.", 60),
        ("Campfire Brownies", "Fudge brownies with toasted marshmallow. Vegetarian. Serves 20.", 48),
    ]),
]


def _font(size, bold=False):
    for p in ([
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]):
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def build():
    img = Image.new("RGB", (W, H), (26, 22, 20))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 156], fill=(140, 58, 30))
    d.text((50, 40), CATERER, font=_font(46, True), fill=(250, 240, 230))
    d.text((52, 106), TAGLINE, font=_font(20), fill=(242, 222, 205))
    y = 200
    for title, items in SECTIONS:
        d.text((50, y), title, font=_font(29, True), fill=(240, 182, 120))
        y += 46
        d.line([50, y, W - 50, y], fill=(92, 72, 60), width=2)
        y += 20
        for name, desc, price in items:
            d.text((60, y), name, font=_font(24, True), fill=(246, 239, 231))
            d.text((W - 170, y), f"${price}", font=_font(24, True), fill=(240, 200, 140))
            y += 34
            d.text((66, y), desc, font=_font(17), fill=(200, 190, 180))
            y += 46
        y += 26

    png = HERE / "_fireside.png"
    img.save(png, "PNG")
    doc = fitz.open()
    page = doc.new_page(width=W * 0.6, height=H * 0.6)
    page.insert_image(page.rect, filename=str(png))   # image only — no text layer
    out = HERE / "fireside_grill_menu.pdf"
    doc.save(out)
    doc.close()
    png.unlink(missing_ok=True)
    print(f"wrote {out}  (image-based, no text layer -> requires vision-LLM)")


if __name__ == "__main__":
    build()
