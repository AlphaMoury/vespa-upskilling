"""
Generate IMAGE-based catering-menu PDFs (decorative menus rendered to an image, with NO
text layer) — so PyMuPDF's get_text() returns nothing and ONLY a vision-LLM can read them.

Contrast with make_sample_menu.py (text-based). Use these to demo that the vision model is
genuinely required. Produces several varied menus for upload-demo variety.

    ../capstone/.venv/bin/python menus/make_image_menu.py
"""

from pathlib import Path
import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H = 1000, 1360

# name -> (caterer, tagline, header color, section-title color, [(SECTION, [(name, desc, price)])])
MENUS = {
    "fireside_grill_menu": (
        "Fireside Grill & Catering", "Austin, TX   ·   Wood-fired catering for events & offices",
        (140, 58, 30), (240, 182, 120), [
            ("STARTERS", [
                ("Smoked Brisket Sliders", "Slow-smoked brisket, pickles and chipotle aioli on brioche. Serves 10.", 78),
                ("Charred Street Corn", "Grilled corn, cotija, lime and chili butter. Vegetarian. Serves 12.", 54),
                ("Loaded Nacho Bar", "Tortilla chips, queso, jalapenos and pico de gallo. Serves 20.", 88)]),
            ("MAINS", [
                ("Texas Brisket Platter", "14-hour oak-smoked brisket with pickles and Texas toast. Serves 15.", 210),
                ("Grilled Chicken Fajitas", "Marinated chicken, peppers, onions and warm tortillas. Serves 12.", 156),
                ("Smoked Portobello Bowl", "Portobello, quinoa, black beans and avocado. Vegan, gluten-free. Serves 10.", 120)]),
            ("DESSERTS", [
                ("Pecan Pie Bites", "Buttery mini pecan tarts. Contains nuts. Serves 24.", 60),
                ("Campfire Brownies", "Fudge brownies with toasted marshmallow. Vegetarian. Serves 20.", 48)]),
        ]),
    "bella_napoli_menu": (
        "Bella Napoli Trattoria", "Boston, MA   ·   Family-style Italian catering",
        (110, 30, 40), (235, 200, 120), [
            ("ANTIPASTI", [
                ("Caprese Skewers", "Cherry tomato, fresh mozzarella and basil with balsamic glaze. Vegetarian. Serves 12.", 58),
                ("Arancini", "Crispy risotto balls stuffed with mozzarella and peas. Vegetarian. Serves 15.", 66),
                ("Antipasto Platter", "Prosciutto, salami, provolone, olives and roasted peppers. Serves 20.", 110)]),
            ("PRIMI & SECONDI", [
                ("Chicken Parmigiana Tray", "Breaded chicken cutlets baked with marinara and mozzarella. Serves 12.", 168),
                ("Baked Ziti", "Ziti in marinara with ricotta and mozzarella. Vegetarian. Serves 15.", 96),
                ("Eggplant Caponata", "Sweet-and-sour Sicilian eggplant with capers. Vegan, gluten-free. Serves 10.", 84)]),
            ("DOLCI", [
                ("Tiramisu Cups", "Espresso-soaked ladyfingers with mascarpone. Serves 20.", 72),
                ("Cannoli Tray", "Crisp shells with sweet ricotta and pistachio. Contains nuts. Serves 24.", 68)]),
        ]),
    "sakura_izakaya_menu": (
        "Sakura Izakaya Catering", "Seattle, WA   ·   Japanese small plates & platters",
        (40, 60, 96), (150, 200, 240), [
            ("SMALL PLATES", [
                ("Edamame Bowl", "Steamed young soybeans with sea salt. Vegan, gluten-free. Serves 12.", 40),
                ("Chicken Gyoza", "Pan-seared dumplings with soy-vinegar dip. Serves 15.", 72),
                ("Vegetable Tempura", "Lightly battered seasonal vegetables. Vegetarian. Serves 10.", 78)]),
            ("PLATTERS", [
                ("Assorted Sushi Platter", "Chef's selection of nigiri and maki. Serves 15.", 220),
                ("Chicken Teriyaki Bowls", "Grilled chicken glazed in teriyaki over rice. Serves 12.", 150),
                ("Tofu Poke Bowls", "Marinated tofu, edamame, avocado over rice. Vegan. Serves 10.", 128)]),
            ("SWEETS", [
                ("Mochi Ice Cream", "Chewy rice-cake bites filled with ice cream. Serves 20.", 66),
                ("Matcha Cheesecake", "Green-tea cheesecake squares. Vegetarian. Serves 16.", 58)]),
        ]),
}


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


def build(stem, caterer, tagline, header_rgb, title_rgb, sections):
    img = Image.new("RGB", (W, H), (26, 22, 20))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 156], fill=header_rgb)
    d.text((50, 40), caterer, font=_font(46, True), fill=(250, 244, 236))
    d.text((52, 106), tagline, font=_font(20), fill=(244, 232, 220))
    y = 200
    for title, items in sections:
        d.text((50, y), title, font=_font(29, True), fill=title_rgb)
        y += 46
        d.line([50, y, W - 50, y], fill=(92, 80, 72), width=2)
        y += 20
        for name, desc, price in items:
            d.text((60, y), name, font=_font(24, True), fill=(246, 240, 232))
            d.text((W - 170, y), f"${price}", font=_font(24, True), fill=(240, 205, 150))
            y += 34
            d.text((66, y), desc, font=_font(17), fill=(202, 194, 186))
            y += 46
        y += 26
    png = HERE / f"_{stem}.png"
    img.save(png, "PNG")
    doc = fitz.open()
    page = doc.new_page(width=W * 0.6, height=H * 0.6)
    page.insert_image(page.rect, filename=str(png))   # image only — no text layer
    out = HERE / f"{stem}.pdf"
    doc.save(out)
    doc.close()
    png.unlink(missing_ok=True)
    print(f"wrote {out.name}  (image-based, no text layer -> requires vision-LLM)")


if __name__ == "__main__":
    for stem, (caterer, tagline, hdr, ttl, secs) in MENUS.items():
        build(stem, caterer, tagline, hdr, ttl, secs)
