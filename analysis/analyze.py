#!/usr/bin/env python3
"""Score The RealReal suit listings exported by the TRR Suit Scout userscript.

Usage:
    python analysis/analyze.py --config config.json data/*.json [--out report]

Stdlib only. Merges any number of exports (price histories are unioned by item key),
filters to your size / designers / fabrics / budget, and ranks by estimated mispricing.
"""
from __future__ import annotations

import argparse
import csv
import glob
import html
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONDITION_ORDER = ["Fair", "Good", "Very Good", "Excellent", "Pristine"]
DEFAULT_CONDITION_FACTOR = {"Pristine": 1.0, "Excellent": 0.92, "Very Good": 0.82, "Good": 0.68, "Fair": 0.5}

# Measurement label aliases as they may appear under "Estimated item measurements".
MEASURE_ALIASES = {
    "chest": ["chest", "bust", "pit to pit", "pit-to-pit", "chest width"],
    "shoulder": ["shoulder", "shoulders", "shoulder to shoulder", "shoulder width", "across shoulders"],
    "sleeve": ["sleeve", "sleeves", "sleeve length"],
    "length": ["length", "jacket length", "back length", "body length"],
    "waist": ["waist", "trouser waist", "pant waist", "pants waist"],
    "inseam": ["inseam", "inside leg"],
    "rise": ["rise", "front rise"],
    "leg_opening": ["leg opening", "hem", "leg"],
}
_ALIAS_LOOKUP = {a: k for k, al in MEASURE_ALIASES.items() for a in al}

DEFAULT_SCORING = {
    "min_comps": 4,
    "weight_comps": 1.0,
    "weight_retail": 0.7,
    "markdown_weight": 0.25,
    "final_sale_penalty": 5,
    "wear_notes_penalty": 3,
    "fit_unverified_penalty": 8,
    "condition_factor": DEFAULT_CONDITION_FACTOR,
}

# ---------------------------------------------------------------- loading


def load_items(paths: list[str]) -> list[dict]:
    """Load exports and merge by key; union price histories."""
    merged: dict[str, dict] = {}
    for p in paths:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for it in data.get("items", data if isinstance(data, list) else []):
            key = it.get("key") or it.get("item_id") or it.get("url")
            if not key:
                continue
            prev = merged.get(key)
            if prev is None or (it.get("last_seen") or "") >= (prev.get("last_seen") or ""):
                newer, older = it, prev
            else:
                newer, older = prev, it
            out = dict(newer)
            hist = {(h["t"], h["price"]) for h in (newer.get("price_history") or [])}
            if older:
                hist |= {(h["t"], h["price"]) for h in (older.get("price_history") or [])}
                out["first_seen"] = min(filter(None, [older.get("first_seen"), newer.get("first_seen")]), default=None)
            out["price_history"] = [{"t": t, "price": p} for t, p in sorted(hist)]
            out["key"] = key
            merged[key] = out
    return list(merged.values())


# ---------------------------------------------------------------- normalisation


def norm_measurements(raw: dict | None) -> tuple[dict, list[str]]:
    """Map labels to canonical keys; convert flat (half) chest/waist to full circumference."""
    out, notes = {}, []
    for label, val in (raw or {}).items():
        if val is None:
            continue
        lab = re.sub(r"\s+", " ", label.lower()).strip()
        key = _ALIAS_LOOKUP.get(lab)
        if key is None:
            key = next((k for a, k in _ALIAS_LOOKUP.items() if lab.startswith(a)), None)
        if key and key not in out:
            out[key] = float(val)
    if "chest" in out and out["chest"] < 30:
        out["chest"] *= 2
        notes.append("chest measured flat, doubled")
    if "waist" in out and out["waist"] < 24:
        out["waist"] *= 2
        notes.append("waist measured flat, doubled")
    return out, notes


def parse_size_label(label: str | None) -> dict:
    """'40R' → {num: 40, len: 'R'}; 'IT 50' / '50' → US 40; 'XL' kept as alpha."""
    if not label:
        return {}
    s = label.upper()
    m = re.search(r"\b(\d{2})(?:\s*(R|S|L|XL|REG|SHORT|LONG))?\b", s)
    if m:
        n = int(m.group(1))
        is_eu = bool(re.search(r"\b(IT|EU|FR|DE)\b", s)) or n >= 44
        if is_eu and 44 <= n <= 62:
            n -= 10
        ln = (m.group(2) or "")[:1] or None
        return {"num": n, "len": ln}
    m = re.search(r"\b(XXS|XS|S|M|L|XL|XXL|XXXL)\b", s)
    return {"alpha": m.group(1)} if m else {}


def canon_designer(name: str | None, title: str | None, designers: list[dict]) -> str | None:
    hay = " ".join(filter(None, [name, title])).lower()
    for d in designers:
        for a in d["aliases"]:
            if re.search(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])", hay):
                return d["name"]
    return name.strip() if name else None


def norm_designers(cfg_designers: list) -> list[dict]:
    out = []
    for d in cfg_designers or []:
        if isinstance(d, str):
            d = {"name": d}
        aliases = [a.lower() for a in d.get("aliases", [])] + [d["name"].lower()]
        out.append({"name": d["name"], "aliases": sorted(set(aliases), key=len, reverse=True)})
    return out


def wants_designer(d: str | None, designers: list[dict]) -> bool:
    return not designers or any(d == x["name"] for x in designers)


def item_fabrics(it: dict) -> set[str]:
    fab = {f.lower() for f in (it.get("fabrics") or [])}
    for c in it.get("composition") or []:
        fab.add(c["fiber"].lower())
    if "virgin wool" in fab:
        fab.add("wool")
    return fab


# ---------------------------------------------------------------- fit


@dataclass
class Fit:
    status: str  # "fits", "fails", "unverified"
    score: float  # 0..1, 1 = perfect
    reasons: list[str] = field(default_factory=list)


def assess_fit(meas: dict, size: dict, profile: dict) -> Fit:
    """Each profile dim is {target, min, max}; min/max should include alteration room."""
    reasons, devs, checked = [], [], 0
    for part in ("jacket", "trousers"):
        for dim, spec in (profile.get(part) or {}).items():
            if not isinstance(spec, dict) or dim not in meas:
                continue
            v = meas[dim]
            checked += 1
            lo, hi = spec.get("min", -math.inf), spec.get("max", math.inf)
            if not (lo <= v <= hi):
                return Fit("fails", 0.0, [f"{dim} {v:g}\" outside {lo:g}–{hi:g}"])
            if "target" in spec:
                span = max(spec["target"] - lo, hi - spec["target"], 0.5)
                devs.append(abs(v - spec["target"]) / span)
                if abs(v - spec["target"]) > 0.5:
                    reasons.append(f"{dim} {v:g}\" (target {spec['target']:g})")
    if checked:
        has_core = any(k in meas for k in ("chest", "shoulder"))
        status = "fits" if has_core else "unverified"
        if not has_core:
            reasons.append("no chest/shoulder measurement")
        return Fit(status, 1 - (sum(devs) / len(devs) if devs else 0), reasons)
    # No usable measurements: fall back to the label.
    want = parse_size_label((profile.get("jacket") or {}).get("size_label"))
    alt = [parse_size_label(s) for s in (profile.get("jacket") or {}).get("also_accept_labels", [])]
    if size and want:
        accepted = [want] + alt
        nums = {w.get("num") for w in accepted if w.get("num")}
        lens = {w["len"] for w in accepted if w.get("len")}
        # A length letter on the item (S/R/L) must be one we accept; no letter → number match is enough.
        if size.get("num") in nums and (not size.get("len") or not lens or size["len"] in lens):
            return Fit("unverified", 0.6, ["label match only, no measurements"])
        return Fit("fails", 0.0, ["size label mismatch"])
    return Fit("unverified", 0.3, ["no size info"])


# ---------------------------------------------------------------- scoring


def median_or_none(xs: list[float], n: int) -> float | None:
    return statistics.median(xs) if len(xs) >= n else None


def analyze(items: list[dict], cfg: dict) -> list[dict]:
    sc = {**DEFAULT_SCORING, **(cfg.get("scoring") or {})}
    cf = {**DEFAULT_CONDITION_FACTOR, **(sc.get("condition_factor") or {})}
    designers = norm_designers(cfg.get("designers"))
    profile = cfg.get("profile") or {}

    rows = []
    for it in items:
        if it.get("price") in (None, 0):
            continue
        meas, mnotes = norm_measurements(it.get("measurements"))
        size = parse_size_label(it.get("size_label"))
        cond = it.get("condition")
        rows.append({
            **it,
            "designer_c": canon_designer(it.get("designer"), it.get("title"), designers),
            "meas": meas,
            "meas_notes": mnotes,
            "size": size,
            "size_bucket": size.get("num") or size.get("alpha"),
            "cf": cf.get(cond, cf["Very Good"]),
            "norm_price": it["price"] / cf.get(cond, cf["Very Good"]),
        })

    # Comparable sets. Sold items count: their last price is a real clearing price.
    by_designer: dict[str, list[dict]] = {}
    for r in rows:
        by_designer.setdefault(r["designer_c"] or "?", []).append(r)
    all_ratios = [r["norm_price"] / r["est_retail"] for r in rows if r.get("est_retail")]
    global_ratio = statistics.median(all_ratios) if all_ratios else None

    results = []
    max_price = cfg.get("max_price")
    min_cond = cfg.get("min_condition")
    fab_cfg = cfg.get("fabrics") or {}
    inc = {f.lower() for f in fab_cfg.get("include_any", [])}
    exc = {f.lower() for f in fab_cfg.get("exclude", [])}

    for r in rows:
        why_not = []
        if r.get("sold"):
            why_not.append("sold")
        if max_price and r["price"] > max_price:
            why_not.append("over budget")
        if not wants_designer(r["designer_c"], designers):
            why_not.append("designer")
        if cfg.get("exclude_final_sale") and r.get("final_sale"):
            why_not.append("final sale")
        if min_cond and r.get("condition") and CONDITION_ORDER.index(r["condition"]) < CONDITION_ORDER.index(min_cond):
            why_not.append("condition")
        fabs = item_fabrics(r)
        if exc and any(any(e in f for f in fabs) for e in exc):
            why_not.append("excluded fabric")
        if inc and fabs and not any(any(i in f for f in fabs) for i in inc):
            why_not.append("fabric")
        if inc and not fabs and fab_cfg.get("require_known"):
            why_not.append("fabric unknown")
        fit = assess_fit(r["meas"], r["size"], profile)
        if fit.status == "fails":
            why_not.append("fit: " + "; ".join(fit.reasons))

        # --- expected price
        peers = [p for p in by_designer.get(r["designer_c"] or "?", []) if p["key"] != r["key"]]
        size_peers = [p for p in peers if p["size_bucket"] and p["size_bucket"] == r["size_bucket"]]
        comp_basis, comp_med = None, median_or_none([p["norm_price"] for p in size_peers], sc["min_comps"])
        if comp_med is not None:
            comp_basis = f"{len(size_peers)} {r['designer_c']} comps in size {r['size_bucket']}"
        else:
            comp_med = median_or_none([p["norm_price"] for p in peers], sc["min_comps"])
            if comp_med is not None:
                comp_basis = f"{len(peers)} {r['designer_c']} comps (all sizes)"
        comp_expected = comp_med * r["cf"] if comp_med else None

        retail_expected, ratio_basis = None, None
        if r.get("est_retail"):
            ratios = [p["norm_price"] / p["est_retail"] for p in peers if p.get("est_retail")]
            ratio = median_or_none(ratios, sc["min_comps"])
            ratio_basis = f"{r['designer_c']} median {ratio:.0%} of retail" if ratio else None
            if ratio is None and global_ratio:
                ratio, ratio_basis = global_ratio, f"all-designer median {global_ratio:.0%} of retail"
            if ratio:
                retail_expected = r["est_retail"] * ratio * r["cf"]

        ests = [(comp_expected, sc["weight_comps"]), (retail_expected, sc["weight_retail"])]
        ests = [(e, w) for e, w in ests if e]
        expected = math.exp(sum(math.log(e) * w for e, w in ests) / sum(w for _, w in ests)) if ests else None
        discount = 1 - r["price"] / expected if expected else None

        # --- markdown history
        hist = [h["price"] for h in r.get("price_history") or []]
        first = max(hist[:1] + [r.get("original_price") or 0]) if hist else (r.get("original_price") or None)
        markdown = (1 - r["price"] / first) if first and first > r["price"] else 0.0
        n_markdowns = sum(1 for a, b in zip(hist, hist[1:]) if b < a)

        # --- score
        score = None
        if discount is not None:
            score = 100 * discount + 100 * sc["markdown_weight"] * markdown
            if r.get("final_sale"):
                score -= sc["final_sale_penalty"]
            if r.get("condition_notes"):
                score -= sc["wear_notes_penalty"]
            if fit.status == "unverified":
                score -= sc["fit_unverified_penalty"]
            score += 5 * (fit.score - 0.5)

        results.append({
            "key": r["key"],
            "url": r.get("url"),
            "image": r.get("image"),
            "designer": r["designer_c"],
            "title": r.get("title"),
            "size_label": r.get("size_label"),
            "measurements": r["meas"],
            "meas_notes": r["meas_notes"],
            "fit": fit.status,
            "fit_score": round(fit.score, 2),
            "fit_notes": fit.reasons,
            "fabrics": sorted(fabs),
            "condition": r.get("condition"),
            "condition_notes": r.get("condition_notes"),
            "final_sale": bool(r.get("final_sale")),
            "price": r["price"],
            "est_retail": r.get("est_retail"),
            "retail_discount": round(1 - r["price"] / r["est_retail"], 3) if r.get("est_retail") else None,
            "comp_expected": round(comp_expected) if comp_expected else None,
            "comp_basis": comp_basis,
            "retail_expected": round(retail_expected) if retail_expected else None,
            "retail_basis": ratio_basis,
            "expected": round(expected) if expected else None,
            "discount_vs_expected": round(discount, 3) if discount is not None else None,
            "markdown_pct": round(markdown, 3),
            "n_markdowns": n_markdowns,
            "first_seen": r.get("first_seen"),
            "score": round(score, 1) if score is not None else None,
            "candidate": not why_not,
            "excluded_because": why_not,
        })
    results.sort(key=lambda x: (not x["candidate"], -(x["score"] if x["score"] is not None else -1e9)))
    return results


# ---------------------------------------------------------------- output

CSV_COLS = ["score", "candidate", "designer", "title", "size_label", "fit", "condition", "final_sale", "price",
            "expected", "discount_vs_expected", "est_retail", "retail_discount", "markdown_pct", "n_markdowns",
            "comp_basis", "retail_basis", "fabrics", "measurements", "excluded_because", "url", "key"]


def write_csv(results: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})


def pct(x):
    return "" if x is None else f"{x:.0%}"


def write_html(results: list[dict], path: Path, show_all: bool) -> None:
    rows = [r for r in results if r["candidate"] or show_all]
    e = html.escape
    trs = []
    for r in rows:
        meas = ", ".join(f"{k} {v:g}\"" for k, v in r["measurements"].items())
        why = "; ".join(r["excluded_because"])
        basis = " · ".join(filter(None, [r["comp_basis"], r["retail_basis"]]))
        trs.append(f"""<tr class="{'' if r['candidate'] else 'excl'}">
<td>{'' if r['score'] is None else r['score']}</td>
<td>{f'<img src="{e(r["image"])}" loading="lazy">' if r['image'] else ''}</td>
<td><a href="{e(r['url'] or '#')}" target="_blank">{e(r['designer'] or '')}<br><small>{e(r['title'] or '')}</small></a>
  <div class="sub">{e(r['key'])}{' · FINAL SALE' if r['final_sale'] else ''}{(' · ' + e(why)) if why else ''}</div></td>
<td>{e(r['size_label'] or '')}<div class="sub">{e(meas)}</div><div class="sub">{e(r['fit'])}{(': ' + e('; '.join(r['fit_notes']))) if r['fit_notes'] else ''}</div></td>
<td>{e(r['condition'] or '')}<div class="sub">{e(r['condition_notes'] or '')}</div></td>
<td>{e(', '.join(r['fabrics']))}</td>
<td data-v="{r['price']}">${r['price']:,.0f}</td>
<td data-v="{r['expected'] or ''}">{'' if r['expected'] is None else f"${r['expected']:,}"}<div class="sub">{e(basis)}</div></td>
<td data-v="{r['discount_vs_expected'] if r['discount_vs_expected'] is not None else ''}">{pct(r['discount_vs_expected'])}</td>
<td data-v="{r['est_retail'] or ''}">{'' if not r['est_retail'] else f"${r['est_retail']:,.0f}"}<div class="sub">{pct(r['retail_discount'])} off</div></td>
<td data-v="{r['markdown_pct']}">{pct(r['markdown_pct'])}<div class="sub">{r['n_markdowns']}× seen</div></td>
</tr>""")
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>TRR suit report</title>
<style>
body{{font:13px system-ui,sans-serif;margin:16px;color:#222}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ddd;padding:6px;vertical-align:top;text-align:left}} th{{cursor:pointer;background:#f5f5f5;position:sticky;top:0}}
img{{width:70px}} .sub{{color:#777;font-size:11px}} tr.excl{{opacity:.45}} a{{color:#124;text-decoration:none}}
</style></head><body>
<h2>TRR suit report</h2><p>{sum(r['candidate'] for r in results)} candidates of {len(results)} listings. Click a header to sort.
Score ≈ % below expected price + markdown bonus − penalties.</p>
<table><thead><tr><th>Score</th><th></th><th>Item</th><th>Size / fit</th><th>Condition</th><th>Fabric</th><th>Price</th>
<th>Expected</th><th>Below exp.</th><th>Est. retail</th><th>Markdown</th></tr></thead><tbody>
{''.join(trs)}</tbody></table>
<script>
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{{const tb=th.closest('table').tBodies[0];
const rows=[...tb.rows];const dir=th.dataset.dir=th.dataset.dir==='a'?'d':'a';
const val=r=>{{const c=r.cells[i];const v=c.dataset.v??c.textContent;const n=parseFloat(v);return isNaN(n)?v:n}};
rows.sort((a,b)=>{{const x=val(a),y=val(b);return (x>y?1:x<y?-1:0)*(dir==='a'?1:-1)}});rows.forEach(r=>tb.appendChild(r))}});
</script></body></html>"""
    path.write_text(doc, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exports", nargs="*", help="Export JSON files (default: data/*.json)")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="report", help="Output basename (writes .html and .csv)")
    ap.add_argument("--all", action="store_true", help="Include excluded listings in the HTML report")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)

    paths = [p for pat in (a.exports or ["data/*.json"]) for p in glob.glob(pat)]
    if not paths:
        print("No export files found. Export from the userscript into data/.", file=sys.stderr)
        return 1
    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    items = load_items(paths)
    results = analyze(items, cfg)
    out = Path(a.out)
    write_csv(results, out.with_suffix(".csv"))
    write_html(results, out.with_suffix(".html"), a.all)

    cands = [r for r in results if r["candidate"]]
    print(f"{len(items)} listings from {len(paths)} file(s); {len(cands)} candidates.")
    for r in cands[: a.top]:
        print(f"{r['score'] if r['score'] is not None else '  n/a':>6}  ${r['price']:>7,.0f}  exp {('$' + format(r['expected'], ',')) if r['expected'] else '   n/a':>8}"
              f"  {(r['designer'] or '')[:18]:<18} {r['size_label'] or '':<8} {r['fit']:<10} {r['url']}")
    print(f"Wrote {out.with_suffix('.html')} and {out.with_suffix('.csv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
