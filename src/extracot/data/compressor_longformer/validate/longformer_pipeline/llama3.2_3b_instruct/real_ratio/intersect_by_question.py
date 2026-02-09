#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Find the intersection of multiple JSONL files by matching 'query' or 'question' text (configurable).
For each input file, save only the entries that belong to the intersection into a corresponding new JSONL.

Default behavior:
- Uses the first available field among: query, question, question_raw, question_text.
- Normalizes text via Unicode NFKC, removes zero-width/non-breaking/full-width spaces,
  collapses whitespace, and lowercases ASCII.
- Computes the strict intersection across all files (keys present in every file).
- Writes one output per input (e.g., file.jsonl -> file.intersect.jsonl).
- Writes a summary stats JSON next to the first output file.

You can relax intersection to "present in at least K files" using --min_files.

Usage:
  python jsonl_intersect_by_question.py \
    --inputs f1.jsonl f2.jsonl f3.jsonl \
    --out_dir ./out \
    --keys query,question,question_raw,question_text \
    --min_files 3

"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from collections import Counter

DEFAULT_KEYS = ["query", "question", "question_raw", "question_text"]

def normalize_text(s: str) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    # remove zero-width & non-breaking & full-width spaces
    s = s.replace("\u200b", "").replace("\u00A0", " ").replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lower()
    return s

def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                print(f"[WARN] {path.name}:{ln} JSON parse error: {e}", file=sys.stderr)

def extract_key(obj: dict, keys: list[str]):
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k]
    # fallback: any field starting with 'question'
    for k in obj.keys():
        if k.lower().startswith("question"):
            return obj[k]
    return None

def make_out_path(in_path: Path, out_dir: Path | None, suffix: str) -> Path:
    base = in_path.name
    if base.endswith(".jsonl"):
        out_name = base[:-6] + suffix
    else:
        out_name = base + suffix
    return (out_dir / out_name) if out_dir else in_path.with_name(out_name)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, type=Path, help="Input JSONL files")
    ap.add_argument("--out_dir", type=Path, default=None, help="Optional output directory")
    ap.add_argument("--suffix", type=str, default=".intersect.jsonl", help="Suffix for output files")
    ap.add_argument("--keys", type=str, default=",".join(DEFAULT_KEYS),
                    help="Comma-separated candidate keys to match (first found is used)")
    ap.add_argument("--min_files", type=int, default=None,
                    help="Keep items that appear in at least this many files; default is strict intersection across all inputs")
    args = ap.parse_args()

    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    key_list = [k.strip() for k in args.keys.split(",") if k.strip()]
    if not key_list:
        key_list = DEFAULT_KEYS

    # First pass: build per-file key sets and basic stats
    per_file_keysets = []
    per_file_stats = []
    key_counts = Counter()

    for p in args.inputs:
        total = valid = 0
        keys_set = set()
        for obj in load_jsonl(p):
            total += 1
            val = extract_key(obj, key_list)
            if val is None:
                continue
            valid += 1
            nkey = normalize_text(val)
            if nkey:
                keys_set.add(nkey)
        per_file_keysets.append((p, keys_set))
        per_file_stats.append({
            "file": str(p),
            "total_lines": total,
            "valid_key_lines": valid,
            "unique_keys": len(keys_set),
        })
        key_counts.update(keys_set)

    # Determine threshold for intersection
    if args.min_files is None:
        threshold = len(args.inputs)  # strict intersection
    else:
        threshold = max(1, min(args.min_files, len(args.inputs)))

    # Compute the set of keys present in at least 'threshold' files
    intersection_keys = {k for k, c in key_counts.items() if c >= threshold}

    # Second pass: filter each file to the intersection keys and write outputs
    written_stats = []
    for p, _ in per_file_keysets:
        out_path = make_out_path(p, args.out_dir, args.suffix)
        written = 0
        with out_path.open("w", encoding="utf-8") as w:
            for obj in load_jsonl(p):
                val = extract_key(obj, key_list)
                if val is None:
                    continue
                nkey = normalize_text(val)
                if nkey in intersection_keys:
                    w.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    written += 1
        written_stats.append({"file": str(p), "out_file": str(out_path), "written_lines": written})

    # Write a summary next to the first output or in out_dir
    summary_path = None
    if args.out_dir:
        summary_path = args.out_dir / "intersection_stats.json"
    else:
        first_out = make_out_path(args.inputs[0], args.out_dir, args.suffix)
        summary_path = first_out.with_suffix(first_out.suffix + ".stats.json")

    summary = {
        "n_inputs": len(args.inputs),
        "threshold_min_files": threshold,
        "intersection_unique_keys": len(intersection_keys),
        "per_file_input_stats": per_file_stats,
        "per_file_output_stats": written_stats,
        "keys_used": key_list,
        "output_suffix": args.suffix,
        "out_dir": str(args.out_dir) if args.out_dir else None,
        "sample_intersection_keys": list(sorted(intersection_keys))[:50],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
