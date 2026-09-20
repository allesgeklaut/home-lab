#!/usr/bin/env python3
"""Aggregate bench_kv.py result files into one summary table.

Rows are grouped by prompt-token count so different KV cache types at the
SAME context can be compared A/B. Multiple seeds of one tag are averaged,
and a per-depth hit-rate breakdown is printed when depth data is available
(bench_kv.py records it)."""
import json
import re
import sys
from pathlib import Path


def _base_tag(tag):
    return re.sub(r"_s\d+$", "", tag)


def load(path):
    d = json.loads(Path(path).read_text())
    rs = d["results"]
    ok = sum(1 for r in rs if r.get("correct"))
    gtps = [r["gen_tps"] for r in rs if r.get("gen_tps")][1:]  # skip cold query
    ptps = [r["prefill_tps"] for r in rs if r.get("prefill_tps")]
    depth_hits = {}
    for r in rs:
        dp = r.get("depth")
        if dp is None:
            continue
        h, t = depth_hits.get(round(dp, 3), (0, 0))
        depth_hits[round(dp, 3)] = (h + (1 if r.get("correct") else 0), t + 1)
    return {
        "tag": d["tag"],
        "base_tag": _base_tag(d["tag"]),
        "seed": d.get("seed"),
        "accuracy": ok,
        "n": len(rs),
        "gen_tps": (sum(gtps) / len(gtps)) if gtps else None,
        "prefill_first": ptps[0] if ptps else None,
        "prefill_cached": (sum(ptps[1:]) / len(ptps[1:])) if len(ptps) > 1 else None,
        "missed": [r["codename"] for r in rs if not r.get("correct")],
        "prompt_tokens": rs[0].get("prompt_tokens"),
        "depth_hits": depth_hits,
    }


def aggregate(rows):
    """Merge rows sharing a base_tag (same config, multiple seeds)."""
    agg = {}
    for r in rows:
        a = agg.setdefault(r["base_tag"], {
            "base_tag": r["base_tag"], "seeds": 0, "acc_sum": 0.0, "n": r["n"],
            "gen": [], "pf_first": [], "pf_cached": [], "missed": [], "depth": {},
        })
        a["seeds"] += 1
        a["acc_sum"] += r["accuracy"]
        if r["gen_tps"]:
            a["gen"].append(r["gen_tps"])
        if r["prefill_first"]:
            a["pf_first"].append(r["prefill_first"])
        if r["prefill_cached"]:
            a["pf_cached"].append(r["prefill_cached"])
        for m in r["missed"]:
            if m not in a["missed"]:
                a["missed"].append(m)
        for dp, (hit, tot) in r["depth_hits"].items():
            h, t = a["depth"].get(dp, (0, 0))
            a["depth"][dp] = (h + hit, t + tot)
    return list(agg.values())


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _record(a):
    g = _mean(a["gen"])
    pf = _mean(a["pf_first"])
    pc = _mean(a["pf_cached"])
    return {
        "tag": a["base_tag"], "seeds": a["seeds"],
        "accuracy_mean": round(a["acc_sum"] / a["seeds"], 3), "accuracy_n": a["n"],
        "gen_tps": round(g, 2) if g is not None else None,
        "prefill_first": round(pf, 1) if pf is not None else None,
        "prefill_cached": round(pc, 1) if pc is not None else None,
        "missed": a["missed"],
        "depth_hits": {f"{dp:.3f}": list(v) for dp, v in sorted(a["depth"].items())},
    }


def main(paths, json_out=None):
    rows = [load(p) for p in paths]
    by_tokens = {}
    for r in rows:
        by_tokens.setdefault(r["prompt_tokens"], []).append(r)
    out_groups = []
    for tokens, group in sorted(by_tokens.items(), key=lambda kv: (kv[0] is None, kv[0])):
        print(f"\n--- context ~{(tokens or 0):,} tokens (identical corpus) ---")
        print(f"{'tag':22s} {'seeds':>5s} {'acc':>7s} {'gen t/s':>8s} "
              f"{'prefill 1st/cached':>19s}  missed")
        aggs = aggregate(group)
        for a in aggs:
            acc = a["acc_sum"] / a["seeds"]
            g = _mean(a["gen"])
            pf = (f"{_mean(a['pf_first']):.0f}/{_mean(a['pf_cached']):.0f}"
                  if a["pf_first"] and a["pf_cached"] else "-")
            accs = f"{acc:.1f}/{a['n']}" if a["seeds"] > 1 else f"{int(acc)}/{a['n']}"
            print(f"{a['base_tag']:22s} {a['seeds']:>5d} {accs:>7s} "
                  f"{(f'{g:.2f}' if g else '-'):>8s} {pf:>19s}  {','.join(a['missed']) or '-'}")
        fastest = max((_mean(a['gen']) or 0) for a in aggs) if aggs else 0
        for a in aggs:
            g = _mean(a["gen"])
            if g and fastest:
                print(f"   {a['base_tag']}: gen speed vs fastest = {g / fastest * 100:.0f}%")
        for a in aggs:
            if a["depth"]:
                bits = " ".join(f"{dp:.2f}:{h}/{t}" for dp, (h, t) in sorted(a["depth"].items()))
                print(f"   {a['base_tag']} depth hits (depth:correct/total): {bits}")
        out_groups.append({
            "prompt_tokens": tokens,
            "runs": [_record(a) for a in aggs],
        })
    if json_out:
        Path(json_out).write_text(json.dumps({"groups": out_groups}, indent=2) + "\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="result JSON files (default: results/*.json)")
    ap.add_argument("--json-out", help="also write the aggregated table as JSON")
    args = ap.parse_args()
    files = args.files
    if not files:
        files = [str(f) for f in sorted(Path(__file__).parent.glob("results/*.json"))
                 if f.name != "summary.json"]
    main(files, args.json_out)
