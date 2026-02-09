#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import gzip
import os
import sys

def smart_open(path, mode='rt'):
    """支持 .gz 与普通文件"""
    if path.endswith('.gz'):
        return gzip.open(path, mode, encoding='utf-8')
    return open(path, mode, encoding='utf-8')

def extract_id(obj, key):
    """从对象里取 id；若 obj 不是 dict，则把整行当作 id 值（兼容B只有id的情形）"""
    if isinstance(obj, dict):
        return obj.get(key)
    return obj  # e.g. 行本身就是 "id"

def load_id_set(b_path, id_key):
    ids = set()
    bad = 0
    with smart_open(b_path, 'rt') as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                bad += 1
                continue
            idv = extract_id(rec, id_key)
            if idv is not None:
                ids.add(idv)
            else:
                bad += 1
    if bad:
        print(f"[warn] {bad} lines in B had no usable id", file=sys.stderr)
    return ids

def main():
    ap = argparse.ArgumentParser(description="Filter A.jsonl by ids listed in B.jsonl and save to output path.")
    ap.add_argument('--a', required=True, help='Path to A.jsonl (optionally .gz)')
    ap.add_argument('--b', required=True, help='Path to B.jsonl (optionally .gz)')
    ap.add_argument('--out', required=True, help='Output path (.jsonl or .jsonl.gz)')
    ap.add_argument('--id-key', default='id', help='Field name for id inside JSON objects (default: id)')
    ap.add_argument('--allow-duplicates', action='store_true',
                    help='Keep all matches from A with the same id; default: only first match per id.')
    args = ap.parse_args()

    ids = load_id_set(args.b, args.id_key)
    if not ids:
        print("[error] No ids loaded from B; nothing to do.", file=sys.stderr)
        sys.exit(1)

    # 确保输出目录存在
    out_dir = os.path.dirname(os.path.abspath(args.out)) or '.'
    os.makedirs(out_dir, exist_ok=True)

    seen = set()
    n_in = n_out = n_bad = 0
    with smart_open(args.a, 'rt') as fa, smart_open(args.out, 'wt') as fo:
        for i, line in enumerate(fa, 1):
            if not line.strip():
                continue
            n_in += 1
            try:
                rec = json.loads(line)
            except Exception:
                n_bad += 1
                continue
            idv = extract_id(rec, args.id_key)
            if idv in ids:
                if args.allow_duplicates or idv not in seen:
                    fo.write(line if line.endswith('\n') else line + '\n')
                    n_out += 1
                    seen.add(idv)
    print(f"[done] Loaded {len(ids)} ids from B; scanned {n_in} lines from A; "
          f"wrote {n_out} lines to {args.out}; skipped {n_bad} bad JSON lines.",
          file=sys.stderr)

if __name__ == '__main__':
    main()
