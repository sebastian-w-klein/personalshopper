"""Run: python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
import analyze as A  # noqa: E402

CFG = json.loads((Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8"))


def item(key, designer, price, retail=None, cond="Excellent", size="40R", meas=None, hist=None, **kw):
    return {"key": key, "designer": designer, "title": f"{designer} Wool Suit", "price": price, "est_retail": retail,
            "condition": cond, "size_label": size, "measurements": meas or {}, "fabrics": ["wool"],
            "price_history": hist or [{"t": "2026-11-01", "price": price}], "url": f"https://www.therealreal.com/products/men/clothing/suiting/{key.lower()}", **kw}


GOOD_MEAS = {"chest": 20, "shoulder": 18, "sleeve": 25, "length": 30, "waist": 32, "inseam": 31}


class TestNormalise(unittest.TestCase):
    def test_flat_chest_doubled(self):
        m, notes = A.norm_measurements({"chest": 20.5, "shoulder to shoulder": 18})
        self.assertEqual(m, {"chest": 41.0, "shoulder": 18.0})
        self.assertTrue(notes)

    def test_size_labels(self):
        self.assertEqual(A.parse_size_label("40R"), {"num": 40, "len": "R"})
        self.assertEqual(A.parse_size_label("IT 50"), {"num": 40, "len": None})
        self.assertEqual(A.parse_size_label("Size: 52L"), {"num": 42, "len": "L"})

    def test_designer_alias(self):
        ds = A.norm_designers(CFG["designers"])
        self.assertEqual(A.canon_designer("Zegna", None, ds), "Ermenegildo Zegna")
        self.assertEqual(A.canon_designer(None, "Brioni Wool Suit", ds), "Brioni")


class TestFit(unittest.TestCase):
    prof = CFG["profile"]

    def test_fits(self):
        m, _ = A.norm_measurements(GOOD_MEAS)
        self.assertEqual(A.assess_fit(m, {}, self.prof).status, "fits")

    def test_shoulder_fail(self):
        m, _ = A.norm_measurements({**GOOD_MEAS, "shoulder": 19.5})
        self.assertEqual(A.assess_fit(m, {}, self.prof).status, "fails")

    def test_label_only(self):
        self.assertEqual(A.assess_fit({}, A.parse_size_label("50"), self.prof).status, "unverified")
        self.assertEqual(A.assess_fit({}, A.parse_size_label("44R"), self.prof).status, "fails")


class TestScoring(unittest.TestCase):
    def test_cheap_item_ranks_first(self):
        items = [item(f"B{i}", "Brioni", 1000 + 50 * i, retail=6000, meas=GOOD_MEAS) for i in range(6)]
        items.append(item("CHEAP", "Brioni", 600, retail=6000, meas=GOOD_MEAS,
                          hist=[{"t": "2026-10-01", "price": 800}, {"t": "2026-11-01", "price": 600}]))
        res = A.analyze(items, {**CFG, "max_price": 2000})
        top = res[0]
        self.assertEqual(top["key"], "CHEAP")
        self.assertTrue(top["candidate"])
        self.assertGreater(top["discount_vs_expected"], 0.3)
        self.assertAlmostEqual(top["markdown_pct"], 0.25)
        self.assertEqual(top["n_markdowns"], 1)

    def test_filters(self):
        items = [item("A", "Brioni", 900, meas=GOOD_MEAS),             # over $800 budget
                 item("B", "Hugo Boss", 200, meas=GOOD_MEAS),          # designer not wanted
                 item("C", "Kiton", 500, cond="Good", meas=GOOD_MEAS), # below min condition
                 item("D", "Kiton", 500, meas=GOOD_MEAS, sold=True)]
        res = {r["key"]: r for r in A.analyze(items, CFG)}
        self.assertIn("over budget", res["A"]["excluded_because"])
        self.assertIn("designer", res["B"]["excluded_because"])
        self.assertIn("condition", res["C"]["excluded_because"])
        self.assertIn("sold", res["D"]["excluded_because"])

    def test_merge_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d, "a.json"); b = Path(d, "b.json")
            a.write_text(json.dumps({"items": [item("X", "Kiton", 700, last_seen="2026-11-01")]}))
            b.write_text(json.dumps({"items": [item("X", "Kiton", 650, last_seen="2026-11-10",
                                                    hist=[{"t": "2026-11-10", "price": 650}])]}))
            merged = A.load_items([str(a), str(b)])
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["price"], 650)
            self.assertEqual([h["price"] for h in merged[0]["price_history"]], [700, 650])
            cfgp = Path(d, "c.json"); cfgp.write_text(json.dumps(CFG))
            self.assertEqual(A.main([str(a), str(b), "--config", str(cfgp), "--out", str(Path(d, "r"))]), 0)
            self.assertTrue(Path(d, "r.html").exists())


if __name__ == "__main__":
    unittest.main()
