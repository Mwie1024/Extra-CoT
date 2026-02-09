#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import argparse
import sys

def iter_jsonl(path):
    """Yield (lineno, raw_line, obj) per non-empty line; skip malformed JSON."""
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            raw = line.rstrip("\n")
            if not raw.strip():
                continue
            try:
                yield i, raw, json.loads(raw)
            except Exception as e:
                print(f"# WARN: skip malformed JSON at {path}:{i}: {e}", file=sys.stderr)

def normalize(text: str, case_sensitive: bool) -> str:
    if not case_sensitive:
        text = text.lower()
    # collapse internal whitespace
    return " ".join(text.split())

def collect_query_set(b_path: str, key: str, case_sensitive: bool) -> set:
    s = set()
    for _, _, obj in iter_jsonl(b_path):
        val = obj.get(key)
        if isinstance(val, str):
            s.add(normalize(val, case_sensitive))
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    s.add(normalize(item, case_sensitive))
    return s

def intersects(val, qset: set, case_sensitive: bool) -> bool:
    if isinstance(val, str):
        return normalize(val, case_sensitive) in qset
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and normalize(item, case_sensitive) in qset:
                return True
    return False

def main():
    ap = argparse.ArgumentParser(
        description="Keep lines from A.jsonl whose `query` intersects queries in B.jsonl."
    )
    ap.add_argument("A", help="Path to A.jsonl (will be filtered)")
    ap.add_argument("B", help="Path to B.jsonl (source of query set)")
    ap.add_argument("-o", "--output", help="Output .jsonl (default: stdout)")
    ap.add_argument("--keyA", default="query", help="Field name in A to compare (default: query)")
    ap.add_argument("--keyB", default="query", help="Field name in B to read (default: query)")
    ap.add_argument("--case-sensitive", action="store_true",
                    help="Use case-sensitive matching (default: case-insensitive)")
    args = ap.parse_args()

    qset = collect_query_set(args.B, args.keyB, args.case_sensitive)
    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    total = kept = 0
    for _, raw, obj in iter_jsonl(args.A):
        total += 1
        if intersects(obj.get(args.keyA), qset, args.case_sensitive):
            out.write(raw + "\n")
            kept += 1

    if out is not sys.stdout:
        out.close()
    print(f"# Done. total_in_A={total}, kept={kept}, unique_B_queries={len(qset)}", file=sys.stderr)

if __name__ == "__main__":
    main()
