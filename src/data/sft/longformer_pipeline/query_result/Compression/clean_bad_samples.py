#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_ratios_tiktoken.py
- 读取目录中的 recat_r020/040/060/080/100.jsonl
- 仅评估 <think>...</think> 区间的质量（tiktoken 计数、轻量复读）
- 默认跳过 0.2 档（可用 --include_r020 开启）
- 输出到 --outdir 下：recat_rxxx.cleaned.jsonl
"""
import argparse, json, re, sys
from pathlib import Path
from typing import Dict, Any, Iterable, Tuple, List
import random
from collections import Counter

# --------- tiktoken ----------
def get_encoding(name: str):
    try:
        import tiktoken
        return tiktoken.get_encoding(name)
        return tiktoken.get_encoding(name)
    except Exception:
        # 兜底：cl100k_base
        import tiktoken
        sys.stderr.write("[WARN] fall back to cl100k_base\n")
        return tiktoken.get_encoding("cl100k_base")

# --------- IO ----------
def load_jsonl(p: Path) -> Iterable[Dict[str, Any]]:
    with p.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception as e:
                sys.stderr.write(f"[WARN] {p.name}:{ln} JSON parse error: {e}\n")

def write_jsonl(items: Iterable[Dict[str, Any]], p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as w:
        for obj in items:
            w.write(json.dumps(obj, ensure_ascii=False) + "\n")

# --------- text utils ----------
THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)
LATEX_CMD = re.compile(r"\\[a-zA-Z]+")           # \frac \sqrt \left ...
NUM_PAT    = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:/\d+)?(?![A-Za-z])")

def extract_think_span(text: str) -> Tuple[str, bool]:
    if not text:
        return "", False
    low = text.lower()
    m1 = THINK_OPEN.search(low)
    m2 = THINK_CLOSE.search(low)
    if m1 and m2 and m1.start() < m2.start():
        return text[m1.end():m2.start()], True
    return "", False

def normalize_for_ngram(s: str) -> List[str]:
    if not s:
        return []
    s = s.replace("$", " ")                 # 去 LaTeX 行内数学符
    s = LATEX_CMD.sub(" CMD ", s)           # LaTeX 命令-> CMD
    s = NUM_PAT.sub(" NUM ", s)             # 数字/分数字符串-> NUM
    s = re.sub(r"[{}^_#~]", " ", s)         # 常见控制字符
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lower()
    # 粗分词：保留运算符有利于区分步骤
    toks = re.split(r"[ \t\r\n]+", s)
    # 过滤太短的 token
    toks = [t for t in toks if t]
    return toks

def ngram_counts(tokens: List[str], n: int) -> Counter:
    from collections import Counter
    if len(tokens) < n:
        return Counter()
    return Counter(tuple(tokens[i:i+n]) for i in range(len(tokens)-n+1))

def repetition_profile(tokens: List[str]) -> Dict[str, float]:
    # Bi/Tri-gram 重复占比： sum_{count>1}(count) / total
    prof = {}
    for n in (2, 3):
        cnts = ngram_counts(tokens, n)
        total = sum(cnts.values())
        rep = sum(c for c in cnts.values() if c > 1)
        prof[f"rep{n}"] = (rep / total) if total > 0 else 0.0
    # TTR
    total_tok = len(tokens)
    uniq = len(set(tokens))
    prof["ttr"] = (uniq / total_tok) if total_tok > 0 else 0.0
    # 最常见 token 占比
    mc = Counter(tokens).most_common(1)
    prof["mcr"] = (mc[0][1] / total_tok) if mc and total_tok > 0 else 0.0
    # 最长相邻重复（单 token）
    longest = 1; cur = 1
    for i in range(1, len(tokens)):
        if tokens[i] == tokens[i-1]:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 1
    prof["max_adj_run"] = float(longest)
    return prof

def count_tokens(enc, text: str) -> int:
    if not text:
        return 0
    # 禁止额外 special tokens，尽量贴近你其它统计
    return len(enc.encode(text, allowed_special=set()))

# --------- ratio-specific thresholds (lenient) ----------
def get_thresholds(ratio: str, include_r020: bool) -> Dict[str, float]:
    # 轻量阈值：尽可能少误杀 0.6/0.8/1.0
    # 解释：min_tok 是 <think> 内的 tiktoken 数；rep2/rep3 为重复占比上限；mcr 为最常见 token 占比上限；
    #      max_adj_run 为“最长相邻重复”上限。
    if ratio == "020":
        if not include_r020:
            # 不使用 0.2 档时，这里不会被调用；给一个较严格的默认
            return dict(min_tok=80, max_rep2=0.65, max_rep3=0.45, max_mcr=0.25, max_adj_run=12)
        return dict(min_tok=100, max_rep2=0.68, max_rep3=0.48, max_mcr=0.28, max_adj_run=12)
    if ratio == "040":
        return dict(min_tok=80,  max_rep2=0.70, max_rep3=0.50, max_mcr=0.30, max_adj_run=16)
    if ratio == "060":
        return dict(min_tok=60,  max_rep2=0.80, max_rep3=0.60, max_mcr=0.35, max_adj_run=24)
    if ratio == "080":
        return dict(min_tok=50,  max_rep2=0.82, max_rep3=0.62, max_mcr=0.38, max_adj_run=28)
    if ratio == "100":
        return dict(min_tok=50,  max_rep2=0.85, max_rep3=0.65, max_mcr=0.40, max_adj_run=32)
    # 兜底
    return dict(min_tok=50, max_rep2=0.85, max_rep3=0.65, max_mcr=0.40, max_adj_run=32)

def should_drop(think_text: str, enc, ratio: str, include_r020: bool) -> Tuple[bool, str, Dict[str, float]]:
    tok_n = count_tokens(enc, think_text)
    toks = normalize_for_ngram(think_text)
    prof = repetition_profile(toks)
    th = get_thresholds(ratio, include_r020)
    # 硬条件
    if tok_n < th["min_tok"]:
        return True, f"short({tok_n}<{th['min_tok']})", prof
    # 轻量复读：只有当“同时”非常高才踢（降低误杀）
    if prof["rep2"] >= th["max_rep2"] and prof["rep3"] >= th["max_rep3"]:
        return True, f"ngram(rep2={prof['rep2']:.2f},rep3={prof['rep3']:.2f})", prof
    if prof["mcr"] >= th["max_mcr"]:
        return True, f"mcr({prof['mcr']:.2f}>={th['max_mcr']:.2f})", prof
    if prof["max_adj_run"] >= th["max_adj_run"]:
        return True, f"adjrun({prof['max_adj_run']:.0f}>={th['max_adj_run']:.0f})", prof
    return False, "", prof

# --------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True, help="包含 recat_rxxx.jsonl 的目录")
    ap.add_argument("--outdir", type=Path, required=True, help="输出清洗文件目录")
    ap.add_argument("--encoding", default="cl100k_base", help="tiktoken 编码名，默认 cl100k_base")
    ap.add_argument("--include_r020", action="store_true", help="并入 0.2 档（默认不并入）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    random.seed(args.seed)
    enc = get_encoding(args.encoding)

    ratios = ["040","060","080","100"]
    if args.include_r020:
        ratios = ["020"] + ratios

    for ratio in ratios:
        in_path = args.dir / f"recat_r{ratio}.jsonl"
        if not in_path.exists():
            sys.stderr.write(f"[WARN] missing {in_path}\n")
            continue

        kept, dropped = [], []
        no_think = 0

        for ex in load_jsonl(in_path):
            text = ex.get("output") or ex.get("model_output") or ""
            think, ok = extract_think_span(text)
            if not ok:
                no_think += 1
                # 0.6+ 档对“没闭合”严格淘汰；0.4 档也淘汰；（你数据里一般都有闭合）
                dropped.append((ex, "no_think", {}))
                continue

            drop, reason, prof = should_drop(think, enc, ratio, args.include_r020)
            if drop:
                dropped.append((ex, reason, prof))
            else:
                kept.append(ex)

        out_path = args.outdir / f"recat_r{ratio}.cleaned.jsonl"
        write_jsonl(kept, out_path)

        # 汇总
        total = len(kept) + len(dropped)
        print(f"[{ratio}] kept={len(kept)} dropped={len(dropped)} total={total} no_think={no_think} -> {out_path}")

        # 取几条示例原因
        if dropped:
            head = dropped[:5]
            print(f"  examples of drop reasons:")
            for i, (_, rsn, pf) in enumerate(head):
                print(f"   - {i+1}. {rsn} | rep2={pf.get('rep2',0):.2f} rep3={pf.get('rep3',0):.2f} "
                      f"ttr={pf.get('ttr',0):.2f} mcr={pf.get('mcr',0):.2f} adj={pf.get('max_adj_run',0):.0f}")

if __name__ == "__main__":
    main()
