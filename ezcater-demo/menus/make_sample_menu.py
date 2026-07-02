"""
Generate a realistic catering-menu PDF fixture so the PDF adapter is always demoable
(no need to source a real caterer PDF to see the pipeline run).

Drop REAL caterer PDFs into this folder too — the adapter ingests every *.pdf here.

    ../capstone/.venv/bin/python menus/make_sample_menu.py
"""

from pathlib import Path
import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent

CATERER = "Marigold & Sage Catering"
TAGLINE = "Boston, MA  |  Corporate catering, delivered  |  orders@marigoldsage.example"

SECTIONS = [
    ("BREAKFAST", [
        ("Sunrise Bagel Board", "Assorted bagels with cream cheese, butter and preserves. Serves 10.", 42.00),
        ("Garden Veggie Frittata", "Baked eggs with peppers, spinach and cheddar, sliced for sharing. Serves 8.", 56.00),
        ("Seasonal Fruit Platter", "Melon, berries and grapes, fresh and light. Serves 12.", 48.00),
    ]),
    ("APPETIZERS & SALADS", [
        ("Mediterranean Mezze", "Hummus, baba ganoush, olives, feta and warm pita for sharing. Serves 10.", 64.00),
        ("Kale & Quinoa Power Salad", "Superfood greens with quinoa, almonds and lemon vinaigrette. Vegan, gluten-free. Serves 8.", 52.00),
        ("Buffalo Cauliflower Bites", "Crispy roasted cauliflower tossed in spicy buffalo sauce. Vegan. Serves 10.", 46.00),
    ]),
    ("MAINS", [
        ("Chicken Tikka Masala", "Char-grilled chicken in a spiced creamy tomato gravy, with basmati rice. Serves 10.", 120.00),
        ("Green Curry with Tofu", "Coconut green curry with tofu and vegetables, fragrant and spicy. Vegan. Serves 8.", 96.00),
        ("Carnitas Taco Bar", "Slow-braised pork with corn tortillas, salsa, guacamole and lime. Gluten-free. Serves 12.", 132.00),
        ("Three-Cheese Baked Ziti", "Ziti in marinara with mozzarella, ricotta and parmesan. Vegetarian. Serves 10.", 88.00),
    ]),
    ("DESSERTS", [
        ("Chocolate Chip Cookie Tray", "Soft-baked cookies loaded with chocolate chunks. Serves 20.", 36.00),
        ("Baklava Bites", "Layered phyllo with walnuts and honey syrup. Serves 15.", 44.00),
    ]),
]


def build():
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # US Letter
    x, y = 54, 60
    page.insert_text((x, y), CATERER, fontsize=22, fontname="helv", color=(0.15, 0.2, 0.15))
    y += 22
    page.insert_text((x, y), TAGLINE, fontsize=9, fontname="helv", color=(0.4, 0.4, 0.4))
    y += 26
    for title, items in SECTIONS:
        if y > 720:
            page = doc.new_page(width=612, height=792); y = 60
        page.insert_text((x, y), title, fontsize=13, fontname="hebo", color=(0.6, 0.3, 0.1))
        y += 6
        page.draw_line((x, y), (558, y), color=(0.8, 0.8, 0.8))
        y += 16
        for name, desc, price in items:
            if y > 730:
                page = doc.new_page(width=612, height=792); y = 60
            page.insert_text((x, y), name, fontsize=11, fontname="hebo", color=(0.1, 0.1, 0.1))
            page.insert_text((520, y), f"${price:,.2f}", fontsize=11, fontname="helv", color=(0.1, 0.1, 0.1))
            y += 14
            page.insert_text((x + 8, y), desc, fontsize=9, fontname="helv", color=(0.35, 0.35, 0.35))
            y += 22
        y += 8
    out = HERE / "sample_catering_menu.pdf"
    doc.save(out)
    doc.close()
    print(f"wrote {out}  ({sum(len(s[1]) for s in SECTIONS)} items across {len(SECTIONS)} sections)")


if __name__ == "__main__":
    build()
