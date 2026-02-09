#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, argparse, math, glob, collections
from typing import List, Dict, Any, Tuple, Optional

# ---------- tiktoken 计数 ----------
def get_encoder(name: str = "o200k_base"):
    try:
        import tiktoken
        return tiktoken.get_encoding(name)
    except Exception as e:
        print(f"[WARN] tiktoken init failed ({e}); fallback to whitespace count.")
        return None

def count_tokens(encoder, text: str) -> int:
    if not text:
        return 0
    if encoder is None:
        # 退化：按空白切词的粗略近似
        return len(text.split())
    return len(encoder.encode(text))

# ---------- IO ----------
def read_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out

def write_jsonl(path: str, items: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# ---------- 抽字段 ----------
def get_question(rec: Dict[str, Any]) -> str:
    for k in ["question", "query", "prompt", "original_question"]:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def get_comp_text(rec: Dict[str, Any]) -> str:
    """
    压缩后的推理文本：严格取 compressed_cot 字段（若不存在/为空则视为无效）
    """
    v = rec.get("compressed_cot")
    return v if isinstance(v, str) and v.strip() else ""

def get_orig_text(rec: Dict[str, Any]) -> str:
    """
    原始推理文本：严格取 cot 字段（若不存在/为空则视为无效）
    """
    v = rec.get("cot")
    return v if isinstance(v, str) and v.strip() else ""

# ---------- 近邻分桶 ----------
def parse_buckets(s: str) -> List[float]:
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = float(tok)
        if val > 1.0:
            val = val / 100.0
        out.append(val)
    return sorted(set(out))

def nearest_bucket(x: float, buckets: List[float]) -> float:
    return min(buckets, key=lambda b: abs(b - x))

def in_band(x: float, target: float, tol: float) -> bool:
    return abs(x - target) <= tol

# ---------- 主流程 ----------
def process_dir(
    in_dir: str,
    out_dir: str,
    buckets: List[float],
    tol: float,
    tokenizer_name: str = "o200k_base",
    overwrite: bool = True
):
    os.makedirs(out_dir, exist_ok=True)
    encoder = get_encoder(tokenizer_name)

    # 收集待处理文件（r020…r100）
    files = sorted(glob.glob(os.path.join(in_dir, "train_outputs_compressed_ratio_*.jsonl")))
    files += sorted(glob.glob(os.path.join(in_dir, "r*.jsonl")))  # 允许别名 r020.jsonl
    files = sorted(list(set(files)))

    # 聚合： per question, per bucket -> best sample
    selected: Dict[str, Dict[float, Dict[str, Any]]] = collections.defaultdict(dict)

    # 统计
    meta = {
        "buckets": buckets,
        "tolerance": tol,
        "tokenizer": tokenizer_name,
        "inputs": {},
        "movement": {os.path.basename(p): {str(b): 0 for b in buckets} for p in files},
        "dropped": {
            "no_question": 0,
            "no_cot": 0,
            "no_compressed_cot": 0,
            "zero_len_cot": 0,
            "zero_len_compressed_cot": 0,
            "out_of_band": 0,
        }
    }

    # 遍历各压缩文件
    for path in files:
        base = os.path.basename(path)
        data = read_jsonl(path)
        meta["inputs"][base] = {"total": len(data), "assigned": 0, "skipped": 0}

        for rec in data:
            q = get_question(rec)
            if not q:
                meta["dropped"]["no_question"] += 1
                continue

            orig_text = get_orig_text(rec)          # 原文：cot
            comp_text = get_comp_text(rec)          # 压缩：compressed_cot

            if not orig_text:
                meta["dropped"]["no_cot"] += 1
                continue
            if not comp_text:
                meta["dropped"]["no_compressed_cot"] += 1
                continue

            orig_len = count_tokens(encoder, orig_text)
            if orig_len <= 0:
                meta["dropped"]["zero_len_cot"] += 1
                continue

            comp_len = count_tokens(encoder, comp_text)
            if comp_len <= 0:
                meta["dropped"]["zero_len_compressed_cot"] += 1
                continue

            r_hat = comp_len / float(orig_len)

            # 决定目标桶
            b = nearest_bucket(r_hat, buckets)
            if not in_band(r_hat, b, tol):
                meta["dropped"]["out_of_band"] += 1
                continue

            # 构造输出样本，补上新字段
            new_rec = dict(rec)  # 浅拷贝
            new_rec["actual_ratio"] = round(r_hat, 6)
            new_rec["orig_tokens"] = int(orig_len)   # 对应 cot
            new_rec["comp_tokens"] = int(comp_len)   # 对应 compressed_cot
            new_rec["rebucket"] = b
            new_rec["source_file"] = base

            # 同题、同桶：择优保留（更接近目标；再者更短）
            old = selected[q].get(b)
            better = False
            if old is None:
                better = True
            else:
                old_gap = abs(old["actual_ratio"] - b)
                new_gap = abs(new_rec["actual_ratio"] - b)
                if new_gap < old_gap:
                    better = True
                elif math.isclose(new_gap, old_gap):
                    if new_rec["comp_tokens"] < old["comp_tokens"]:
                        better = True
            if better:
                selected[q][b] = new_rec
                meta["movement"][base][str(b)] += 1
                meta["inputs"][base]["assigned"] += 1

        meta["inputs"][base]["skipped"] = meta["inputs"][base]["total"] - meta["inputs"][base]["assigned"]

    # 按桶位输出
    bucket_to_items: Dict[float, List[Dict[str, Any]]] = {b: [] for b in buckets}
    for q, by_b in selected.items():
        for b, rec in by_b.items():
            bucket_to_items[b].append(rec)

    for b, lst in bucket_to_items.items():
        tag = int(round(b * 100))
        out_path = os.path.join(out_dir, f"recat_r{tag:03d}.jsonl")
        if (not overwrite) and os.path.exists(out_path):
            raise FileExistsError(out_path)
        write_jsonl(out_path, lst)
        print(f"[WRITE] {out_path}  ({len(lst)} samples)")

    # 写 metadata
    meta["totals"] = {
        "unique_questions": len(selected),
        "total_selected_pairs": sum(len(v) for v in selected.values()),
        "per_bucket_counts": {str(b): len(bucket_to_items[b]) for b in buckets}
    }
    with open(os.path.join(out_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(json.dumps(meta["totals"], ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(
        description="Re-bucket by actual ratio computed as token(compressed_cot) / token(cot) per-sample."
    )
    ap.add_argument("--in_dir", required=True, help="目录：含 train_outputs_compressed_ratio_*.jsonl 或 r*.jsonl")
    ap.add_argument("--out_dir", required=True, help="输出目录")
    ap.add_argument("--buckets", default="0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9", help="目标桶位，逗号分隔；可用百分数")
    ap.add_argument("--tol", type=float, default=0.05, help="容忍带（与目标桶的最大偏差）")
    ap.add_argument("--tokenizer", default="cl100k_base", help="tiktoken 编码名（如 o200k_base / cl100k_base）")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出")
    args = ap.parse_args()

    buckets = parse_buckets(args.buckets)
    process_dir(
        in_dir=args.in_dir,
        out_dir=args.out_dir,
        buckets=buckets,
        tol=float(args.tol),
        tokenizer_name=args.tokenizer,
        overwrite=bool(args.overwrite),
    )

if __name__ == "__main__":
    main()
