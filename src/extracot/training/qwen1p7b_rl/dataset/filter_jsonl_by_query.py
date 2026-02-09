#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, argparse, sys

def get_by_dotted(obj, path: str):
    cur = obj
    for p in path.split('.'):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur

def iter_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield ln, json.loads(s)
            except Exception as e:
                print(f"# WARN: skip malformed JSON at {path}:{ln}: {e}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="Keep records from A.jsonl whose id is NOT in B.jsonl")
    ap.add_argument("A", help="Path to A.jsonl (to be filtered)")
    ap.add_argument("B", help="Path to B.jsonl (provides the id set)")
    ap.add_argument("-o", "--output", help="Output .jsonl (default: stdout)")
    ap.add_argument("--id-key", default="id", help="Id field or dotted path (default: id)")
    ap.add_argument("--drop-missing", action="store_true",
                    help="Drop A records that have no id under --id-key")
    args = ap.parse_args()

    # Build id set from B
    b_ids = set()
    for _, obj in iter_jsonl(args.B):
        v = get_by_dotted(obj, args.id_key)
        if v is not None:
            b_ids.add(str(v))

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    total = kept = 0
    for _, obj in iter_jsonl(args.A):
        total += 1
        v = get_by_dotted(obj, args.id_key)
        if v is None and args.drop_misserror if hasattr(args, "drop_missing") else False:
            continue
        if v is None or str(v) not in b_ids:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            kept += 1

    if out is not sys.stdout:
        out.close()
    print(f"# Done. total_in_A={total} kept={kept} unique_B_ids={len(b_ids)}", file=sys.stderr)

if __name__ == "__main__":
    main()
