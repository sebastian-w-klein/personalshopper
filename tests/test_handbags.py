"""Run: python -m unittest discover tests"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analysis"))
import analyze as A  # noqa: E402

CFG = json.loads((ROOT / "handbags.example.json").read_text(encoding="utf-8"))
DIMS_OK = {"width": 10, "height": 7, "depth": 3, "shoulder strap drop": 21}


def bag(key, designer, title, price, retail=None, cond="Excellent", meas=None, desc="", **kw):
    return {"key": key, "designer": designer, "title": title, "price": price, "est_retail": retail,
            "condition": cond, "measurements": DIMS_OK if meas is None else meas, "description": desc,
            "price_history": [{"t": "2026-11-01", "price": price}],
            "url": f"https://www.therealreal.com/products/women/handbags/shoulder-bags/{key.lower()}", **kw}


class TestBagAttrs(unittest.TestCase):
    def test_detects_attributes(self):
        ds = {d["name"]: d for d in A.norm_designers(CFG["designers"])}
        it = bag("A", "Dior", "Christian Dior Medium Lady Dior Bag", 1500,
                 desc="Black Lambskin\nCannage Quilting\nGold-Tone Hardware\nIncludes Dust Bag & Shoulder Strap")
        a = A.bag_attrs(it, CFG["bag"], ds["Dior"])
        self.assertEqual(a["model"], "Lady Dior")
        self.assertEqual(a["colors"], ["black"])
        self.assertIn("lambskin", a["materials"])
        self.assertEqual(a["hardware"], ["gold"])
        self.assertEqual(a["size_words"], ["medium"])
        self.assertIn("dust bag", a["includes"])

    def test_bag_measurement_aliases(self):
        m, _ = A.norm_measurements({"Length": 10, "Height": 7, "Shoulder Strap Drop": 22, "Handle Drop": 4}, "handbags")
        self.assertEqual(m, {"width": 10, "height": 7, "strap_drop": 22, "handle_drop": 4})


class TestBagFilters(unittest.TestCase):
    def res(self, items):
        return {r["key"]: r for r in A.analyze(items, CFG)}

    def test_filters(self):
        r = self.res([
            bag("OK", "Chanel", "Chanel Black Caviar Classic Flap Bag", 1800),
            bag("PINK", "Chanel", "Chanel Pink Caviar Classic Flap Bag", 1800),
            bag("PYTHON", "Chanel", "Chanel Black Python Classic Flap Bag", 1800),
            bag("BIG", "Chanel", "Chanel Black Caviar Jumbo Classic Flap", 1800, meas={"width": 15, "height": 9}),
            bag("NANO", "Chanel", "Chanel Black Caviar Nano Flap Bag", 900),
            bag("PACK", "Prada", "Prada Black Nylon Backpack", 600),
            bag("NODIMS", "Celine", "Celine Tan Calfskin Belt Bag", 1200, meas={}),
            bag("SUIT", "Gucci", "Gucci Wool Suit", 500,
                url="https://www.therealreal.com/products/men/clothing/suiting/gucci-suit"),
        ])
        self.assertTrue(r["OK"]["candidate"], r["OK"]["excluded_because"])
        self.assertIn("color not wanted", r["PINK"]["excluded_because"])
        self.assertTrue(any("material: exotic" in x for x in r["PYTHON"]["excluded_because"]))
        self.assertTrue(any(x.startswith("dims:") for x in r["BIG"]["excluded_because"]))
        self.assertTrue(any("size: nano" in x for x in r["NANO"]["excluded_because"]))
        self.assertTrue(any("style" in x for x in r["PACK"]["excluded_because"]))
        self.assertEqual(r["NODIMS"]["fit"], "unverified")
        self.assertNotIn("SUIT", r)  # other categories are ignored via url_filter

    def test_model_comps(self):
        items = [bag(f"L{i}", "Dior", "Dior Black Lambskin Lady Dior", 2000 + 100 * i, retail=6000) for i in range(5)]
        items += [bag(f"S{i}", "Dior", "Dior Brown Calfskin Saddle Bag", 1200 + 100 * i, retail=4000) for i in range(5)]
        items.append(bag("DEAL", "Dior", "Dior Black Lambskin Lady Dior", 1400, retail=6000))
        top = A.analyze(items, {**CFG, "max_price": 3000})[0]
        self.assertEqual(top["key"], "DEAL")
        self.assertIn("model Lady Dior", top["comp_basis"])
        self.assertGreater(top["discount_vs_expected"], 0.25)


if __name__ == "__main__":
    unittest.main()
