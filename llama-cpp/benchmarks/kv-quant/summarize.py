#!/usr/bin/env python3
"""Aggregate bench_kv.py result files into one summary table."""
import json
import sys
from pathlib import Path

def load(path):
    d = json.loads(Path(path).read_text())
    rs = d["results"]
    ok = sum(1 for r in rs if r.get("correct"))
    gtps = [r["gen_tps"] for r in rs if r.get("gen_tps")][1:]  # skip cold query
    ptps = [r["prefill_tps"] for r in rs if r.get("prefill_tps")]
    return {
        "tag": d["tag"],
        "accuracy": ok,
        "n": len(rs),
        "gen_tps": round(sum(gtps) / len(gtps), 2) if gtps else None,
        "prefill_first": round(ptps[0], 1) if ptps else None,
        "prefill_cached": round(sum(ptps[1:]) / len(ptps[1:]), 1) if len(ptps) > 1 else None,
        "missed": [r["codename"] for r in rs if not r.get("correct")],
        "prompt_tokens": rs[0].get("prompt_tokens"),
    }

def main(paths):
    rows = [load(p) for p in paths]
    # group by ctx: rows with same token count belong to one A/B pair
    by_tokens = {}
    for r in rows:
        by_tokens.setdefault(r["prompt_tokens"], []).append(r)
    for tokens, group in sorted(by_tokens.items()):
        print(f"\n--- context ~{tokens:,} tokens (identical corpus) ---")
        print(f"{'tag':24s} {'acc':>5s} {'gen t/s':>8s} {'prefill t/s':>12s}  missed")
        for r in group:
            print(f"{r['tag']:24s} {r['accuracy']}/{r['n']:>2d} {r['gen_tps']:>8.2f} "
                  f"{r['prefill_first']:>12.1f}  {','.join(r['missed']) or '-'}")
        fastest = max((r['gen_tps'] for r in group if r['gen_tps']), default=0)
        for r in group:
            if r['gen_tps'] and fastest:
                print(f"   {r['tag']}: gen speed vs fastest = {r['gen_tps']/fastest*100:.0f}%")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        files = sorted(Path(__file__).parent.glob("results/*.json"))
        files = [f for f in files if f.name != "summary.json"]
    else:
        files = sys.argv[1:]
    main([str(f) for f in files])