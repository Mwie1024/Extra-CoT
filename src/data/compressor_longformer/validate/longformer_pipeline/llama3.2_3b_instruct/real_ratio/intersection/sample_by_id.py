#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import argparse

def load_ids(b_path: str, id_key: str = "id"):
    ids = set()
    with open(b_path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise RuntimeError(f"[B.jsonl] 第 {ln} 行解析失败: {e}")
            v = obj.get(id_key)
            if v is not None:
                ids.add(str(v))
    return ids

def filter_a_by_ids(a_path: str, ids: set, out_path: str, id_key: str = "id"):
    kept, total, miss = 0, 0, 0
    with open(a_path, "r", encoding="utf-8") as fin, \
         open(out_path, "w", encoding="utf-8") as fout:
        for ln, line in enumerate(fin, 1):
            total += 1
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise RuntimeError(f"[A.jsonl] 第 {ln} 行解析失败: {e}")
            k = obj.get(id_key)
            if k is None:
                miss += 1
                continue
            if str(k) in ids:
                fout.write(json.dumps(obj, ensure_ascii=False) + "\n")
                kept += 1
    print(f"从 B 读到 {len(ids)} 个 id；扫描 A 共 {total} 行，输出 {kept} 行，A 中缺少 {id_key} 的行 {miss} 行。保存至：{out_path}")

def main():
    ap = argparse.ArgumentParser(description="按 B.jsonl 的 id 从 A.jsonl 中筛选样本")
    ap.add_argument("A_jsonl", help="A.jsonl 路径（被筛选）")
    ap.add_argument("B_jsonl", help="B.jsonl 路径（提供 id）")
    ap.add_argument("out_jsonl", help="输出文件路径")
    ap.add_argument("--id_key", default="id", help="id 字段名（默认 id）")
    args = ap.parse_args()

    ids = load_ids(args.B_jsonl, args.id_key)
    filter_a_by_ids(args.A_jsonl, ids, args.out_jsonl, args.id_key)

if __name__ == "__main__":
    main()
