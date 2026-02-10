#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
sample_by_token_bins.py
依区间近似均匀地从 jsonl 中抽取 K 条样本：
    使用 tiktoken 统计 tokens(query + think_between_tags),
    仅保留 token_count < max_tokens，按给定 bins 尽量均匀抽样。

用法示例：
    python3 sample_by_token_bins.py data.jsonl -o out.jsonl \
        --k 50000 --max-tokens 4070 \
        --bins 0,512,1024,2048,3072,4070 \
        --encoding cl100k_base \
        --query-keys query,question,prompt \
        --output-keys model_output,output

依赖：
    pip install tiktoken
"""

import argparse, json, random, re, sys
from typing import List, Dict, Any, Optional

# -------- tiktoken ----------
try:
    import tiktoken
except Exception as e:
    print("请先安装 tiktoken: pip install tiktoken", file=sys.stderr)
    raise

RE_THINK = re.compile(r"(?is)<think>(.*?)</think>")

def get_encoding(encoding_name: Optional[str], model_name: Optional[str]):
    if model_name:
        try:
            return tiktoken.encoding_for_model(model_name)
        except KeyError:
            print(f"# WARN: 未识别的模型名 {model_name}，改用编码名。", file=sys.stderr)
    if encoding_name:
        return tiktoken.get_encoding(encoding_name)
    # 默认 cl100k_base（GPT-3.5/4 常用）
    return tiktoken.get_encoding("cl100k_base")

def stringify(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        # 常见字段兜底
        for k in ("reasoning", "cot", "content", "response", "text"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v
        ch = x.get("choices")
        if isinstance(ch, list) and ch:
            m = ch[0].get("message", {})
            for k in ("reasoning", "content"):
                v = m.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        return json.dumps(x, ensure_ascii=False)
    if isinstance(x, list):
        return "\n".join((y.get("text", "") if isinstance(y, dict) else str(y)) for y in x)
    return "" if x is None else str(x)

def extract_query(rec: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""  # 没有就空串

def extract_think(rec: Dict[str, Any], output_keys: List[str]) -> str:
    out = ""
    for k in output_keys:
        if k in rec:
            out = stringify(rec[k])
            break
    if not isinstance(out, str):
        return ""
    thinks = RE_THINK.findall(out)
    if not thinks:
        return ""  # 无 <think> 段则返回空串（将被过滤）
    # 若多段，则合并
    return "\n".join(s.strip() for s in thinks if s and s.strip())

def token_len(enc, text: str) -> int:
    return len(enc.encode(text))

def find_bin(count: int, edges: List[int]) -> Optional[int]:
    # 半开区间：[e[i], e[i+1])
    for i in range(len(edges) - 1):
        if edges[i] <= count < edges[i + 1]:
            return i
    return None

def fair_allocate(available: List[int], k: int) -> List[int]:
    """尽可能均匀地把总量 k 分配到各 bin，受限于 available。"""
    n = len(available)
    if n == 0:
        return []
    assign = [0] * n
    base = k // n
    for i in range(n):
        assign[i] = min(base, available[i])
    remaining = k - sum(assign)
    # 轮转补齐：优先给剩余空间多的 bin
    while remaining > 0:
        leftovers = [(available[i] - assign[i], i) for i in range(n)]
        leftovers = [x for x in leftovers if x[0] > 0]
        if not leftovers:
            break
        leftovers.sort(reverse=True)  # 按“可再分配”从大到小
        progressed = False
        for _, i in leftovers:
            if assign[i] < available[i]:
                assign[i] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
        if not progressed:  # 理论不会到这
            break
    return assign

def iter_jsonl(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield ln, json.loads(s)
            except Exception as e:
                print(f"# WARN: 跳过非法 JSON 行 {ln}: {e}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="按 token 区间近似均匀抽样 jsonl")
    ap.add_argument("input", help="输入 .jsonl")
    ap.add_argument("-o", "--output", required=True, help="输出 .jsonl")
    ap.add_argument("--k", type=int, default=50000, help="抽样总数（默认 50000）")
    ap.add_argument("--max-tokens", type=int, default=4070, help="上限（独占，默认 4070）")
    ap.add_argument("--bins", default="0,512,1024,2048,3072,4070",
                    help="分箱边界（逗号分隔，升序，默认 0,512,1024,2048,3072,4070）")
    ap.add_argument("--encoding", default="cl100k_base", help="tiktoken 编码名（默认 cl100k_base）")
    ap.add_argument("--model-name", default=None, help="可选：按模型名选择编码（优先于 --encoding）")
    ap.add_argument("--query-keys", default="query,question,prompt", help="query 字段候选（逗号分隔）")
    ap.add_argument("--output-keys", default="model_output,output", help="输出字段候选（逗号分隔）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    args = ap.parse_args()

    edges = [int(x) for x in args.bins.split(",") if x.strip()]
    if len(edges) < 2 or sorted(edges) != edges:
        raise ValueError("--bins 必须为升序整数列表，如 0,512,1024,2048,3072,4070")
    if edges[-1] != args.max_tokens:
        print(f"# WARN: bins 上界 {edges[-1]} 与 --max-tokens {args.max_tokens} 不同。", file=sys.stderr)

    enc = get_encoding(args.encoding, args.model_name)
    qkeys = [x.strip() for x in args.query_keys.split(",") if x.strip()]
    okeys = [x.strip() for x in args.output_keys.split(",") if x.strip()]
    rng = random.Random(args.seed)

    # 分箱缓存
    bin_buckets: List[List[Dict[str, Any]]] = [[] for _ in range(len(edges) - 1)]
    total, skipped_no_think, skipped_too_long = 0, 0, 0

    for ln, rec in iter_jsonl(args.input):
        total += 1
        q = extract_query(rec, qkeys)
        think = extract_think(rec, okeys)
        if not think:  # 必须有 <think> 段
            skipped_no_think += 1
            continue
        tok_cnt = token_len(enc, (q.strip() + "\n" + think.strip()).strip())
        if tok_cnt >= args.max_tokens:
            skipped_too_long += 1
            continue
        b = find_bin(tok_cnt, edges)
        if b is None:
            # 理论上不会出现（因为 < max_tokens），但稳妥起见仍然跳过
            continue
        # 可以把 token 数附加上（便于后续检查），不想改动结构可注释掉
        rec = dict(rec)
        rec["_token_count_q_plus_think"] = tok_cnt
        rec["_bin"] = f"[{edges[b]},{edges[b+1]})"
        bin_buckets[b].append(rec)

    avail = [len(x) for x in bin_buckets]
    total_candidates = sum(avail)
    target_total = min(args.k, total_candidates)
    assign = fair_allocate(avail, target_total)

    chosen: List[Dict[str, Any]] = []
    for i, need in enumerate(assign):
        if need <= 0:
            continue
        bucket = bin_buckets[i]
        if len(bucket) <= need:
            chosen.extend(bucket)
        else:
            chosen.extend(rng.sample(bucket, need))

    rng.shuffle(chosen)

    with open(args.output, "w", encoding="utf-8") as f:
        for obj in chosen:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # 统计信息
    print("# Stats", file=sys.stderr)
    print(f"  total_read={total}", file=sys.stderr)
    print(f"  candidates(<{args.max_tokens})={total_candidates}", file=sys.stderr)
    print(f"  selected={len(chosen)} (target={args.k})", file=sys.stderr)
    print(f"  skipped_no_think={skipped_no_think}", file=sys.stderr)
    print(f"  skipped_too_long(>=max_tokens)={skipped_too_long}", file=sys.stderr)
    for i, (a, n) in enumerate(zip(avail, assign)):
        print(f"  bin {i} [{edges[i]},{edges[i+1]}): available={a} selected={n}", file=sys.stderr)

if __name__ == "__main__":
    main()
