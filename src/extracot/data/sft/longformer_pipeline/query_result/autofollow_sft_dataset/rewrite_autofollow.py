#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewrite a JSONL file into a single JSON array with entries:
{
  "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
  "input":  "<question> <COMP_AUTO>",
  "output": "<special_token>\\n<output>"
}

- <special_token> is taken directly from each sample's special token field (default: "special_token").
- "question" falls back to "query" if missing.
- "output" falls back to "model_output" if missing.
- Items missing any of (question/query, output/model_output, special_token) are skipped.
- Writes a stats JSON next to the output for auditing.
"""

import argparse
import json
import sys
from pathlib import Path

INSTRUCTION = r"Please reason step by step, and put your final answer within \boxed{}."

def load_jsonl(p: Path):
    with p.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception as e:
                sys.stderr.write(f"[WARN] {p.name}:{ln} JSON parse error: {e}\n")

def first_nonempty(obj: dict, keys: list[str]):
    for k in keys:
        if k in obj and obj[k] is not None:
            v = obj[k]
            if isinstance(v, (str, int, float)):
                v = str(v)
            if isinstance(v, str) and v.strip() != "":
                return v
    return None

def main():
    ap = argparse.ArgumentParser(description="Convert JSONL to a unified JSON array using <COMP_AUTO> and per-item special_token.")
    ap.add_argument("-i", "--input", required=True, type=Path, help="Input .jsonl path")
    ap.add_argument("-o", "--output", required=True, type=Path, help="Output .json path (JSON array)")
    ap.add_argument("--question_keys", type=str, default="question,query", help="Comma-separated keys to read question text (first present used)")
    ap.add_argument("--output_keys", type=str, default="output,model_output", help="Comma-separated keys to read model output (first present used)")
    ap.add_argument("--special_keys", type=str, default="special_token", help="Comma-separated keys to read special token (first present used)")
    args = ap.parse_args()

    q_keys = [k.strip() for k in args.question_keys.split(",") if k.strip()]
    o_keys = [k.strip() for k in args.output_keys.split(",") if k.strip()]
    s_keys = [k.strip() for k in args.special_keys.split(",") if k.strip()]

    results = []
    total = 0
    skipped_missing_q = skipped_missing_o = skipped_missing_s = 0

    for obj in load_jsonl(args.input):
        total += 1
        q = first_nonempty(obj, q_keys)
        o = first_nonempty(obj, o_keys)
        s = first_nonempty(obj, s_keys)

        if q is None:
            skipped_missing_q += 1
            continue
        if o is None:
            skipped_missing_o += 1
            continue
        if s is None:
            skipped_missing_s += 1
            continue

        item = {
            "instruction": INSTRUCTION,
            "input": f"{q} <COMP_AUTO>",
            "output": f"{s}\n{o}",
        }
        results.append(item)

    # Write JSON array
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as w:
        json.dump(results, w, ensure_ascii=False, indent=2)

    # Stats
    stats = {
        "input_path": str(args.input),
        "output_path": str(args.output),
        "total_read": total,
        "total_written": len(results),
        "skipped_missing_question_or_query": skipped_missing_q,
        "skipped_missing_output_or_model_output": skipped_missing_o,
        "skipped_missing_special_token": skipped_missing_s,
        "question_keys": q_keys,
        "output_keys": o_keys,
        "special_token_keys": s_keys,
    }
    stats_path = args.output.with_suffix(args.output.suffix + ".stats.json")
    with stats_path.open("w", encoding="utf-8") as w:
        json.dump(stats, w, ensure_ascii=False, indent=2)

    print(f"[DONE] wrote {len(results)} items to {args.output}")
    print(f"[INFO] stats at {stats_path}")

if __name__ == "__main__":
    main()
