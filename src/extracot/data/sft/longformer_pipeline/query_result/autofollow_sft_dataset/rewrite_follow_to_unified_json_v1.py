#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rewrite rxxx.jsonl files (xxx in {020,040,060,080,100}) into a single JSON file with format:
For 020-080:
{
  "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
  "input":  question + " " + <COMP_xx>,
  "output": <COMP_xx> + "\n" + output
}
  (xx is derived from the file name; by default 020->"20" token renders as <COMP_20> unless --keep_zero_pad)

For 100:
{
  "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
  "input":  query,
  "output": model_output
}

All rewritten samples are concatenated and randomly shuffled, then saved as one JSON array.
"""

import argparse
import json
import random
import sys
from pathlib import Path
from glob import glob

RATIOS = ["020", "040", "060", "080", "100"]
INSTRUCTION = r"Please reason step by step, and put your final answer within \boxed{}."

def find_ratio_file(dir_path: Path, ratio: str) -> Path | None:
    """
    Try common file name patterns to locate the file for a ratio.
    Priority order:
      - r{ratio}.jsonl
      - recat_r{ratio}.jsonl
      - any *{ratio}.jsonl (first match in sorted order)
    """
    candidates = [
        dir_path / f"r{ratio}.jsonl",
        dir_path / f"recat_r{ratio}.jsonl",
    ]
    for c in candidates:
        if c.exists():
            return c

    # Fallback: glob any file with the ratio digits
    matches = sorted(Path(p) for p in glob(str(dir_path / f"*{ratio}.jsonl")))
    for m in matches:
        if m.exists():
            return m
    return None

def load_jsonl(p: Path):
    with p.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                sys.stderr.write(f"[WARN] {p.name}:{ln} JSON parse error: {e}\n")

def first_nonempty(obj: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip() != "":
            return v
    return None

def comp_token(ratio: str, keep_zero_pad: bool) -> str:
    if keep_zero_pad:
        # <COMP_020> style
        return f"<COMP_{ratio}>"
    # default: <COMP_20> style
    try:
        pct = int(ratio)  # "020"->20
    except Exception:
        pct = 0
    return f"<COMP_{pct}>"

def rewrite_sample_ratio_20_80(obj: dict, token: str) -> dict | None:
    """Use question and output; add token to input/output as specified."""
    q = first_nonempty(obj, ["question", "question_raw", "question_text", "query"])
    o = first_nonempty(obj, ["output", "model_output"])
    if q is None or o is None:
        return None
    return {
        "instruction": INSTRUCTION,
        "input": f"{q} {token}",
        "output": f"{token}\n{o}",
    }

def rewrite_sample_ratio_100(obj: dict) -> dict | None:
    """Use query for input and model_output for output (fallbacks used if missing)."""
    q = first_nonempty(obj, ["query", "question"])
    o = first_nonempty(obj, ["model_output", "output"])
    if q is None or o is None:
        return None
    return {
        "instruction": INSTRUCTION,
        "input": q,
        "output": o,
    }

def main():
    ap = argparse.ArgumentParser(description="Rewrite rxxx.jsonl files and merge into a single shuffled JSON dataset.")
    ap.add_argument("--dir", required=True, type=Path, help="Directory containing rxxx.jsonl files")
    ap.add_argument("--outfile", required=True, type=Path, help="Output JSON file path (single array)")
    ap.add_argument("--seed", type=int, default=0, help="Random seed for shuffling")
    ap.add_argument("--keep_zero_pad", default=False, action="store_true", help="Use <COMP_020> instead of <COMP_20> style tokens")
    args = ap.parse_args()

    random.seed(args.seed)

    all_items = []
    stats = {"per_ratio": {}, "missing_files": []}

    for ratio in RATIOS:
        path = find_ratio_file(args.dir, ratio)
        if not path:
            stats["missing_files"].append(ratio)
            sys.stderr.write(f"[WARN] No file found for ratio {ratio}\n")
            continue

        tok = comp_token(ratio, args.keep_zero_pad)
        is_hundred = (ratio == "100")
        rewritten_count = 0
        read_count = 0
        skipped = 0

        for obj in load_jsonl(path):
            read_count += 1
            if is_hundred:
                new_obj = rewrite_sample_ratio_100(obj)
            else:
                new_obj = rewrite_sample_ratio_20_80(obj, tok)

            if new_obj is None:
                skipped += 1
                continue

            all_items.append(new_obj)
            rewritten_count += 1

        stats["per_ratio"][ratio] = {
            "path": str(path),
            "read": read_count,
            "rewritten": rewritten_count,
            "skipped": skipped,
            "token_used": tok if not is_hundred else None,
        }
        sys.stderr.write(f"[INFO] {ratio}: read={read_count} rewritten={rewritten_count} skipped={skipped}\n")

    # Shuffle and write
    random.shuffle(all_items)
    args.outfile.parent.mkdir(parents=True, exist_ok=True)
    with args.outfile.open("w", encoding="utf-8") as w:
        json.dump(all_items, w, ensure_ascii=False, indent=2)

    # Save stats next to outfile
    stats_path = args.outfile.with_suffix(args.outfile.suffix + ".stats.json")
    stats["total_written"] = len(all_items)
    stats["outfile"] = str(args.outfile)
    with stats_path.open("w", encoding="utf-8") as w:
        json.dump(stats, w, ensure_ascii=False, indent=2)

    print(f"[DONE] Wrote {len(all_items)} items to {args.outfile}")
    print(f"[INFO] Stats saved to {stats_path}")

if __name__ == "__main__":
    main()
