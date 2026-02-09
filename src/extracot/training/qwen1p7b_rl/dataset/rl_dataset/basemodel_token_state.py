#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jsonl_token_stats.py — 统计 JSONL 文件中某个字段（默认: model_output）的 token 数等统计信息

用法示例:
    python jsonl_token_stats.py path/to/file.jsonl
    # 指定字段（支持点路径）：
    python jsonl_token_stats.py data.jsonl --field model_output
    python jsonl_token_stats.py data.jsonl --field choices.0.message.content
    # 指定 tokenizer：
    python jsonl_token_stats.py data.jsonl --model gpt-4o-mini
    python jsonl_token_stats.py data.jsonl --encoding cl100k_base
    # 更多选项：
    python jsonl_token_stats.py data.jsonl --percentiles 5,50,95 --topk 10 --preview 120
"""

import argparse, gzip, math, statistics, sys
try:
    import ujson as json
except Exception:
    import json

try:
    import tiktoken
except Exception:
    tiktoken = None


def get_encoding(encoding_name=None, model=None):
    if tiktoken is None:
        return None
    enc = None
    if encoding_name:
        try:
            enc = tiktoken.get_encoding(encoding_name)
        except Exception:
            enc = None
    if enc is None and model:
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = None
    if enc is None:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            enc = None
    return enc


def get_by_path(obj, path):
    """支持 a.b.c 的点路径取值；若不存在返回 None。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
                cur = cur[idx]
            except Exception:
                return None
        else:
            return None
    return cur


def count_tokens(text, enc):
    """返回 (token_count, char_count)"""
    if not isinstance(text, str):
        if isinstance(text, list):
            text = "\n".join(
                x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
                for x in text
            )
        elif isinstance(text, dict):
            text = json.dumps(text, ensure_ascii=False)
        else:
            text = str(text)
    if enc is not None:
        return len(enc.encode(text)), len(text)
    # 退化：空白近似
    return len(text.split()), len(text)


def open_maybe_gzip(path):
    return gzip.open(path, "rt", encoding="utf-8", errors="ignore") if path.endswith(".gz") \
        else open(path, "r", encoding="utf-8", errors="ignore")


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals)-1) * (p/100.0)
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="JSONL 文件路径（支持 .gz）")
    ap.add_argument("--field", default="model_output", help="要统计的字段（支持点路径）")
    ap.add_argument("--model", default=None, help="tiktoken 的模型名，如 gpt-4o, gpt-4o-mini, gpt-4.1, gpt-3.5-turbo 等")
    ap.add_argument("--encoding", default=None, help="tiktoken 编码名，如 o200k_base, cl100k_base；与 --model 二选一")
    ap.add_argument("--percentiles", default="50,95", help="分位点，逗号分隔，如 5,50,95")
    ap.add_argument("--topk", type=int, default=5, help="列出 token 数最多的样本个数")
    ap.add_argument("--preview", type=int, default=80, help="Top 样本内容预览字符数")
    args = ap.parse_args()

    enc = get_encoding(args.encoding, args.model)
    if enc is None:
        print("[警告] 未安装/加载 tiktoken，将用空白近似计数。建议: pip install tiktoken", file=sys.stderr)

    # 解析分位点
    pcts = []
    for x in args.percentiles.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            v = float(x)
            if 0 <= v <= 100:
                pcts.append(v)
        except Exception:
            pass
    if not pcts:
        pcts = [50, 95]

    total_lines = json_errors = missing_field = empty_text = 0
    token_counts, char_counts = [], []
    total_tokens = total_chars = 0

    import heapq
    top_heap = []  # (tokens, line_no, rec_id, preview)
    K = max(0, args.topk)

    with open_maybe_gzip(args.path) as f:
        for line_no, line in enumerate(f, start=1):
            s = line.strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except Exception:
                json_errors += 1
                continue
            total_lines += 1

            val = get_by_path(rec, args.field)
            if val is None:
                missing_field += 1
                continue

            tokens, n_chars = count_tokens(val, enc)
            if n_chars == 0:
                empty_text += 1

            token_counts.append(tokens)
            char_counts.append(n_chars)
            total_tokens += tokens
            total_chars += n_chars

            if K > 0:
                if isinstance(val, str):
                    preview_src = val
                elif isinstance(val, (dict, list)):
                    preview_src = json.dumps(val, ensure_ascii=False)
                else:
                    preview_src = str(val)
                preview = preview_src[:args.preview] + ("…" if len(preview_src) > args.preview else "")
                rec_id = rec.get("id") or rec.get("uuid") or rec.get("example_id") or str(line_no)
                heapq.heappush(top_heap, (tokens, line_no, str(rec_id), preview))
                if len(top_heap) > K:
                    heapq.heappop(top_heap)

    n = len(token_counts)
    if n == 0:
        print("没有可统计的数据；请检查 --field 是否正确。", file=sys.stderr)
        return 1

    mean_tokens = total_tokens / n
    med_tokens = statistics.median(token_counts)
    std_tokens = statistics.pstdev(token_counts) if n > 1 else 0.0
    min_tokens, max_tokens = min(token_counts), max(token_counts)
    token_counts_sorted = sorted(token_counts)
    pct_values = {p: percentile(token_counts_sorted, p) for p in pcts}

    # 输出
    print("\n=== 基本信息 ===")
    print(f"文件: {args.path}")
    print(f"字段: {args.field}")
    print(f"总行数(有效解析): {total_lines}")
    print(f"JSON 解析失败: {json_errors}")
    print(f"缺少字段: {missing_field}")
    print(f"空文本: {empty_text}")
    print(f"tokenizer: {'tiktoken/' + getattr(enc, 'name', 'unknown') if enc else 'whitespace(近似)'}")

    print("\n=== Token 统计（按样本）===")
    print(f"样本数: {n}")
    print(f"平均 tokens: {mean_tokens:.2f}")
    print(f"中位数: {med_tokens:.2f}")
    print(f"标准差(总体): {std_tokens:.2f}")
    print(f"最小值: {min_tokens}")
    print(f"最大值: {max_tokens}")
    for p in sorted(pct_values):
        print(f"P{int(p)}: {pct_values[p]:.2f}")

    print("\n=== 其他 ===")
    avg_chars = total_chars / n if n else float('nan')
    ratio = (total_tokens / total_chars) if total_chars else float('nan')  # Token/字符
    print(f"总 tokens: {total_tokens}")
    print(f"总字符数: {total_chars}")
    print(f"平均字符数: {avg_chars:.2f}")
    if ratio == ratio:  # 非 NaN
        print(f"平均 Token/字符 比: {ratio:.4f}")
        print(f"平均 字符/Token 比: {1/ratio:.2f}")
    else:
        print("平均 Token/字符 比: n/a")

    if K > 0 and top_heap:
        print(f"\n=== Token 数最多的前 {len(top_heap)} 条 ===")
        for tokens, line_no, rec_id, preview in sorted(top_heap, key=lambda x: (-x[0], x[1])):
            print(f"[tokens={tokens:>6}] line={line_no:>7} id={rec_id} preview=\"{preview}\"")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
