#!/usr/bin/env python3
"""KV cache quantization accuracy benchmark for llama.cpp server.

Method: needle-in-a-haystack. Builds ONE filler corpus, inserts N needle
facts at different depths, then asks for each needle against the same
cached context (prompt prefix reuse). Compares runs across server
instances started with different --cache-type-k/--cache-type-v.

Memory-safe: corpus is a few hundred KB of text, responses are tiny,
results are appended to disk incrementally. No large allocations.

Usage:
  python3 bench_kv.py --server http://127.0.0.1:8184 --tag q8_0 \
      --target-tokens 45000 --needles 6 --out results_q8.json
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request
import urllib.error

NOUNS = ["harbor", "meadow", "cellar", "lantern", "orchard", "kettle", "ledger", "bramble",
         "compass", "satchel", "granite", "willow", "cobalt", "thistle", "marble", "falcon"]
VERBS = ["collected", "catalogued", "repaired", "measured", "polished", "buried", "labelled", "traded"]

def make_filler(rng, n_lines):
    lines = []
    for i in range(n_lines):
        n1, n2 = rng.choice(NOUNS), rng.choice(NOUNS)
        if n2 == n1:
            n2 = NOUNS[(NOUNS.index(n1) + 3) % len(NOUNS)]
        v = rng.choice(VERBS)
        q = rng.randint(2, 97)
        lines.append(f"Entry {i:05d}: the archivist {v} a {n1} beside the {n2} and noted {q} items in the margin.")
    return lines

def approx_tokens(text):
    # rough first guess; real count comes from server /tokenize
    return int(len(text) / 3.8)

def server_tokenize(server, text):
    """Exact token count via llama.cpp /tokenize. Falls back to approx on error."""
    try:
        req = urllib.request.Request(server.rstrip("/") + "/tokenize",
                                     data=json.dumps({"content": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return len(json.loads(r.read())["tokens"])
    except Exception as e:
        print(f"tokenize failed ({e}), using approx", flush=True)
        return approx_tokens(text)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--tag", required=True, help="label for this run, e.g. q8_0 or q4_0")
    ap.add_argument("--target-tokens", type=int, default=45000)
    ap.add_argument("--server-ctx", type=int, default=49152, help="ctx size the server was started with")
    ap.add_argument("--needles", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-predict", type=int, default=48)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-retries", type=int, default=2)
    args = ap.parse_args()

    rng = random.Random(args.seed)

    # --- build needles ---
    needles = []
    for i in range(args.needles):
        code = f"{rng.randint(1000, 9999)}"
        codename = f"Project-{rng.choice(NOUNS).capitalize()}{i}"
        needles.append({"codename": codename, "code": code,
                        "line": f"MEMO: the access code for {codename} is {code}."})

    # --- build filler to target size, insert needles at spread depths ---
    depths = [round(0.02 + (0.93 * i) / max(1, args.needles - 1), 3) for i in range(args.needles)]
    target_chars = int(args.target_tokens * 3.8)
    filler = make_filler(rng, 12000)
    corpus = []
    used = set()
    total = 0
    fi = 0
    for d in depths:
        goal = int(target_chars * d)
        while total < goal and fi < len(filler):
            corpus.append(filler[fi]); total += len(filler[fi]); fi += 1
        n = needles[len(used)]
        corpus.append(n["line"]); used.add(id(n))
    while total < target_chars and fi < len(filler):
        corpus.append(filler[fi]); total += len(filler[fi]); fi += 1
    haystack = "\n".join(corpus)

    # --- true token budget check against the server context ---
    instructions = ("You are an archivist. Below is a long archive log. "
                    "Answer with ONLY the requested 4-digit access code, nothing else.")
    question_tpl = "What is the access code for {codename}? Answer with only the 4-digit code."

    probe = instructions + "\n\n" + haystack + "\n\n" + question_tpl.format(codename=needles[0]["codename"])
    n_ctx = server_tokenize(args.server, probe)
    # template + reply overhead margin (~120 tokens)
    overhead = 120
    budget = args.server_ctx - 64
    # measure real chars/token ratio of this filler style, rebuild once with correct target
    ratio = len(haystack) / max(1, n_ctx)
    if n_ctx + overhead > budget:
        # rebuild from scratch with corrected char target (keeps needle depths intact)
        keep = max(1.0, (budget - overhead) / max(1, n_ctx))
        target_chars = int(len(haystack) * keep * 0.98)
        filler = make_filler(rng, 12000)
        corpus = []; used = set(); total = 0; fi = 0
        for d in depths:
            goal = int(target_chars * d)
            while total < goal and fi < len(filler):
                corpus.append(filler[fi]); total += len(filler[fi]); fi += 1
            n = needles[len(used)]
            corpus.append(n["line"]); used.add(id(n))
        while total < target_chars and fi < len(filler):
            corpus.append(filler[fi]); total += len(filler[fi]); fi += 1
        haystack = "\n".join(corpus)
        probe = instructions + "\n\n" + haystack + "\n\n" + question_tpl.format(codename=needles[0]["codename"])
        n_ctx = server_tokenize(args.server, probe)
        # final safety trim, proportional
        guard = 0
        while n_ctx + overhead > budget and guard < 10 and len(corpus) > args.needles * 4:
            keep = max(0.5, (budget - overhead) / max(1, n_ctx))
            drop = max(1, int(len(corpus) * (1 - keep)))
            del corpus[:drop]
            haystack = "\n".join(corpus)
            probe = instructions + "\n\n" + haystack + "\n\n" + question_tpl.format(codename=needles[0]["codename"])
            n_ctx = server_tokenize(args.server, probe)
            guard += 1
        if n_ctx + overhead > budget:
            raise SystemExit("cannot fit corpus in ctx budget, abort")
    n_tok = server_tokenize(args.server, haystack)
    print(f"[{args.tag}] corpus lines={len(corpus)} chars={len(haystack)} tokens={n_tok} "
          f"(server-checked, prompt total={n_ctx}, ctx={args.server_ctx})", flush=True)
    # ask in shuffled order so retrieval order != insertion order
    order = list(range(args.needles))
    rng.shuffle(order)

    results = []
    out_path = args.out
    def flush():
        with open(out_path, "w") as f:
            json.dump({"tag": args.tag, "seed": args.seed, "needles": needles, "results": results}, f, indent=2)

    for qi, idx in enumerate(order):
        needle = needles[idx]
        prompt = haystack + "\n\n" + question_tpl.format(codename=needle["codename"])
        use_cache = qi > 0  # first request fills the slot, rest reuse prefix
        payload = {
            "messages": [{"role": "user", "content": instructions + "\n\n" + prompt}],
            "temperature": 0, "top_k": 1, "top_p": 1.0,
            "n_predict": args.n_predict, "cache_prompt": use_cache,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }
        t0 = time.time()
        attempt = 0
        resp = None
        while attempt <= args.max_retries:
            try:
                req = urllib.request.Request(args.server.rstrip("/") + "/v1/chat/completions",
                                             data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3600) as r:
                    resp = json.loads(r.read())
                break
            except Exception as e:
                attempt += 1
                print(f"[{args.tag}] request {qi+1}/{args.needles} attempt {attempt} failed: {e}", flush=True)
                time.sleep(5)
        if resp is None:
            results.append({"idx": idx, "codename": needle["codename"], "expected": needle["code"],
                            "answer": "<request-failed>", "correct": False, "error": True})
            flush(); continue

        msg = resp["choices"][0]["message"]
        answer = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        # exact code match (allow the code embedded in longer text)
        correct = needle["code"] in answer
        timings = resp.get("timings", {})
        usage = resp.get("usage", {})
        entry = {
            "idx": idx, "codename": needle["codename"],
            "ask_order": qi + 1, "expected": needle["code"],
            "answer": answer[:200], "correct": correct,
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": usage.get("prompt_tokens_cached"),
            "gen_tokens": usage.get("completion_tokens"),
            "gen_tps": timings.get("predicted_per_second"),
            "prefill_tps": timings.get("prompt_per_second"),
            "wall_s": round(time.time() - t0, 1),
        }
        results.append(entry)
        flush()
        print(f"[{args.tag}] q{qi+1}/{args.needles} {needle['codename']} expected={needle['code']} "
              f"correct={correct} prefill_tps={entry['prefill_tps']} gen_tps={entry['gen_tps']} "
              f"cached={entry['cached_tokens']} wall={entry['wall_s']}s", flush=True)

    n_ok = sum(1 for r in results if r.get("correct"))
    print(f"[{args.tag}] ACCURACY {n_ok}/{len(results)}", flush=True)
    flush()

if __name__ == "__main__":
    main()