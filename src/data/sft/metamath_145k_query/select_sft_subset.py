#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, random, re, sys
from collections import defaultdict
from typing import List, Dict, Any, Tuple

# ---- tokenizer (tiktoken proxy for Qwen) ----
def get_encoder(enc_name: str = "auto"):
    try:
        import tiktoken
    except Exception as e:
        print("[ERROR] tiktoken not installed. pip install tiktoken", file=sys.stderr)
        raise
    if enc_name == "auto":
        # 优先更新的词表；不可用再回退
        for name in ("o200k_base", "cl100k_base"):
            try:
                return tiktoken.get_encoding(name)
            except Exception:
                continue
        # 最后兜底
        return tiktoken.get_encoding("cl100k_base")
    else:
        return tiktoken.get_encoding(enc_name)

def count_tokens(encoder, text: str) -> int:
    if not text:
        return 0
    return len(encoder.encode(text, disallowed_special=()))

# ---- I/O ----
def read_jsonl(path: str) -> List[Dict[str, Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: 
                continue
            try:
                data.append(json.loads(line))
            except Exception:
                pass
    return data

def write_jsonl(path: str, rows: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")

def write_text(path: str, text: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def write_json(path: str, obj: Any):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# ---- think extraction ----
THINK_RE = re.compile(r"<think>(.*?)</think>", flags=re.DOTALL | re.IGNORECASE)

def extract_think(text: str) -> str:
    if not text:
        return ""
    m = THINK_RE.search(text)
    return (m.group(1).strip() if m else "")

def norm_question(q: str) -> str:
    # 用于去重：大小写与空白归一
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    return q.lower()

# ---- bucketing & sampling ----
def make_bins(edges: List[int]) -> List[Tuple[int,int]]:
    """edges 如 [0,512,1024,2048,3072,4070] -> [(0,512),(512,1024),...]"""
    bins = []
    for i in range(len(edges)-1):
        bins.append((edges[i], edges[i+1]))
    return bins

def bucket_index(val: int, bins: List[Tuple[int,int]]) -> int:
    for i,(lo,hi) in enumerate(bins):
        if lo <= val < hi:
            return i
    # 边界等于最后上限时，归入最后一桶
    if bins and val == bins[-1][1]:
        return len(bins) - 1
    return -1

def balanced_take_per_bucket(candidates_by_bin: Dict[int, List[int]], target_n: int, seed: int) -> List[int]:
    """尽量均匀；不足再二次分配。返回被选样本的全局索引列表。"""
    rng = random.Random(seed)
    K = len(candidates_by_bin)
    base = target_n // K if K > 0 else 0
    take = {b: min(base, len(candidates_by_bin[b])) for b in candidates_by_bin}
    used = sum(take.values())
    # 把剩余名额按剩余容量比例分配
    remain = target_n - used
    if remain > 0:
        # 计算每桶还能拿多少
        caps = {b: max(0, len(candidates_by_bin[b]) - take[b]) for b in candidates_by_bin}
        order = sorted(caps.items(), key=lambda kv: (-kv[1], kv[0]))  # 先给容量大的
        ptr = 0
        while remain > 0 and any(c > 0 for _, c in order):
            b, cap = order[ptr]
            if cap > 0:
                take[b] += 1
                cap -= 1
                remain -= 1
                order[ptr] = (b, cap)
            ptr = (ptr + 1) % len(order)
    # 最终抽样
    selected_idx = []
    for b, lst in candidates_by_bin.items():
        if not lst: 
            continue
        k = min(take[b], len(lst))
        rng.shuffle(lst)
        selected_idx.extend(lst[:k])
    return selected_idx

# ---- main pipeline ----
def main():
    ap = argparse.ArgumentParser(description="Select SFT base questions from Qwen3-1.7B outputs by (question + <think>) token budget and CoT-length bucketing.")
    ap.add_argument("--input", required=True, help="输入 JSONL（57k 正确样本）")
    ap.add_argument("--output", required=True, help="输出 JSONL（去重后目标题目集）")
    ap.add_argument("--stats_dir", default=".", help="统计输出目录（bucket_stats.json/txt）")
    ap.add_argument("--target_n", type=int, default=15000, help="目标题目数（例如 15000 或 20000）")
    ap.add_argument("--max_total_tokens", type=int, default=4070, help="阈值：question + <think> token 总数上限")
    ap.add_argument("--bins", default="0,512,1024,2048,3072,4070", help="CoT（<think>）长度分桶边界（逗号分隔，单位=token）")
    ap.add_argument("--encoder", default="auto", help="tiktoken 编码器：auto/o200k_base/cl100k_base 等")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    enc = get_encoder(args.encoder)
    edges = [int(x) for x in args.bins.split(",") if x.strip()]
    assert len(edges) >= 2 and edges == sorted(edges), "bins 必须是升序边界列表"
    bins = make_bins(edges)

    rows = read_jsonl(args.input)
    print(f"[INFO] Loaded {len(rows)} rows from {args.input}")

    # 逐条计算 (question + think) token、仅 CoT token，过滤与分桶
    # 用 question 文本去重
    dedup_seen = set()
    candidates = []  # 保存保留索引与统计
    per_bin = defaultdict(list)

    for idx, r in enumerate(rows):
        q = r.get("question") or r.get("query") or r.get("prompt") or ""
        mo = r.get("model_output") or r.get("response") or r.get("cot") or ""
        if not q or not mo:
            continue

        think_text = extract_think(mo)
        # 可选：若无 <think>，视为不合格
        if think_text == "":
            continue

        q_tok = count_tokens(enc, q)
        c_tok = count_tokens(enc, think_text)
        total_tok = q_tok + c_tok

        if total_tok > args.max_total_tokens:
            continue

        # 去重（按 question）
        q_norm = norm_question(q)
        if q_norm in dedup_seen:
            continue
        dedup_seen.add(q_norm)

        # 记录候选
        r["_sft_stats"] = {
            "q_tokens": q_tok,
            "cot_tokens": c_tok,
            "total_tokens": total_tok
        }
        candidates.append(r)

    print(f"[INFO] After filter & dedup: {len(candidates)} candidates (question + think <= {args.max_total_tokens})")

    # 分桶
    for i, r in enumerate(candidates):
        c_tok = r["_sft_stats"]["cot_tokens"]
        b = bucket_index(c_tok, bins)
        if b >= 0:
            per_bin[b].append(i)
    # 统计
    bin_stats = []
    for bi, (lo, hi) in enumerate(bins):
        bin_stats.append({
            "bin_index": bi,
            "range": [lo, hi],
            "candidates": len(per_bin.get(bi, []))
        })

    # 采样
    goal = min(args.target_n, len(candidates))
    selected_idx = balanced_take_per_bucket(per_bin, goal, args.seed)
    selected = [candidates[i] for i in selected_idx]

    # 输出
    write_jsonl(args.output, selected)
    print(f"[INFO] Wrote {len(selected)} selected rows to {args.output}")

    # 统计输出
    sel_bins = defaultdict(int)
    for i in selected_idx:
        c_tok = candidates[i]["_sft_stats"]["cot_tokens"]
        b = bucket_index(c_tok, bins)
        if b >= 0:
            sel_bins[b] += 1

    stats = {
        "input_rows": len(rows),
        "candidates_after_filter_dedup": len(candidates),
        "target_n": args.target_n,
        "selected_n": len(selected),
        "max_total_tokens": args.max_total_tokens,
        "bins": edges,
        "per_bin": [
            {
                "bin_index": bi,
                "range": bins[bi],
                "candidates": len(per_bin.get(bi, [])),
                "selected": sel_bins.get(bi, 0)
            } for bi in range(len(bins))
        ]
    }
    os.makedirs(args.stats_dir, exist_ok=True)
    write_json(os.path.join(args.stats_dir, "bucket_stats.json"), stats)

    # 也写个可读版
    lines = []
    lines.append(f"TOTAL candidates: {len(candidates)}   SELECTED: {len(selected)} / target {args.target_n}")
    lines.append(f"Constraint: question + <think> <= {args.max_total_tokens} tokens")
    lines.append("Bins (CoT tokens only):")
    for bi in range(len(bins)):
        lo, hi = bins[bi]
        lines.append(f"  [{bi}] [{lo}, {hi})  candidates={len(per_bin.get(bi, []))}  selected={sel_bins.get(bi, 0)}")
    write_text(os.path.join(args.stats_dir, "bucket_stats.txt"), "\n".join(lines))
    print("[INFO] Stats written to", args.stats_dir)

if __name__ == "__main__":
    main()
