#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
fix_and_preview_compress.py
- 读取 JSON/JSONL（UTF-8 严格），抽取 <think>…</think> 的 COT
- 用已训练的 Longformer 压缩器做 top-k 选择（按 keep logits）
- 用 tokenizer 的 offset_mapping 在“原始 Unicode 字符串”上做**字符级**重建
- 过程全程检测 U+FFFD（�），一旦发现立即报错
- 写出预览 JSONL（UTF-8，ensure_ascii=False）

依赖:
  pip install transformers ftfy
"""

import re, os, json, argparse, unicodedata
from typing import List, Dict, Any, Tuple
from transformers import AutoTokenizer, AutoModelForTokenClassification
import torch

RE_THINK = re.compile(r"<think>([\s\S]*?)</think>", flags=re.IGNORECASE)
REPLACEMENT = "\uFFFD"

def read_any_utf8(path: str) -> List[dict]:
    # 先按 JSON 试，失败再 JSONL；严格 UTF-8
    with open(path, "r", encoding="utf-8", errors="strict") as f:
        text = f.read()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, list) else [obj]
    except Exception:
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s: continue
            if s.endswith(","): s = s[:-1].strip()
            try:
                rec = json.loads(s)
                if isinstance(rec, dict): out.append(rec)
            except Exception:
                raise RuntimeError(f"[JSONL] line {i} 解析失败")
        return out

def must_no_replacement(where: str, s: str):
    if REPLACEMENT in s:
        # 定位前后文，帮助回溯
        i = s.index(REPLACEMENT)
        ctx = s[max(0,i-40): i+40]
        raise RuntimeError(f"[{where}] 出现 U+FFFD 替换字符，说明上游已有损坏：...{ctx}...")

def extract_think_span(raw: str) -> str:
    m = RE_THINK.search(raw)
    return (m.group(1).strip() if m else raw.strip())

def char_spans_from_topk(full_text: str,
                         offsets: List[Tuple[int,int]],
                         keep_mask: torch.Tensor,
                         start_char: int = 0,
                         merge_gap_chars: int = 1) -> List[Tuple[int,int]]:
    """
    根据 token offset（字符级）与 keep_mask，生成字符级区间；
    - 仅保留 [start_char, end) 内的字符（用于跳过 <Q>...<SEP> 前缀）
    - 将小于等于 merge_gap_chars 的间隙合并，减少碎片
    """
    L = len(full_text)
    kept = [0]*L
    for (L0,R0), k in zip(offsets, keep_mask.tolist()):
        if L0==R0:  # special
            continue
        if k==1:
            a = max(L0, start_char); b = R0
            for i in range(a, b):
                kept[i] = 1

    # 汇聚成 [a,b) 区间并做 gap 合并
    spans = []
    i = start_char
    while i < L:
        if kept[i]==1:
            j=i+1
            while j<L and kept[j]==1:
                j+=1
            spans.append([i,j])  # 暂存 list，方便修改
            i=j
        else:
            i+=1

    if not spans: return []

    # 合并邻近小 gap
    merged = [spans[0]]
    for s,e in spans[1:]:
        gap = s - merged[-1][1]
        if gap <= merge_gap_chars:
            merged[-1][1] = e
        else:
            merged.append([s,e])
    return [(s,e) for s,e in merged]

def render_by_spans(text: str, spans: List[Tuple[int,int]]) -> str:
    # 将多个字符区间切出后用单空格拼接，避免硬拼导致乱码
    parts = [text[s:e] for (s,e) in spans]
    out = " ".join(p.strip() for p in parts if p.strip()!="")
    must_no_replacement("render_by_spans", out)
    return out

def compress_one(question: str,
                 cot: str,
                 tok,
                 model,
                 keep_ratio: float,
                 q_prefix: str = "<Q> {} </Q> <SEP> "):
    # 构造前缀 + 原始 COT（不改字符编码，不做 bytes 操作）
    prefix = q_prefix.format(question.strip())
    full_text = prefix + cot.strip()
    must_no_replacement("input_full_text", full_text)

    enc = tok(full_text,
              return_offsets_mapping=True,
              padding=False,
              truncation=True,
              max_length=4096)
    input_ids = torch.tensor([enc["input_ids"]], dtype=torch.long, device=model.device)
    attn_mask = torch.tensor([enc["attention_mask"]], dtype=torch.long, device=model.device)
    offsets   = enc["offset_mapping"]

    with torch.no_grad():
        out = model(input_ids=input_ids, attention_mask=attn_mask)
        logits = out.logits[0]              # [L,2]
        keep_scores = logits[:,1]           # [L]

    # 只在有效位置（非 special，且在 COT 段）上取 Top‑k
    valid_idx = []
    for i,(L0,R0) in enumerate(offsets):
        if (L0,R0)!=(0,0) and R0>len(prefix):
            valid_idx.append(i)

    if not valid_idx:
        return "", 0.0

    k = max(1, int(round(len(valid_idx)*keep_ratio)))
    scores = keep_scores[valid_idx]
    topk = torch.topk(scores, k=k, largest=True, sorted=False).indices.tolist()
    chosen_token_idx = set(valid_idx[i] for i in topk)

    keep_mask = torch.zeros(len(offsets), dtype=torch.long)
    for i in chosen_token_idx:
        keep_mask[i]=1

    print("============" * 10)
    print(keep_ratio, " ", len(chosen_token_idx), " ", len(valid_idx))
    print("============" * 10)

    # 以字符级“掩膜→区间”，并做小间隙合并（减少碎片）
    spans = char_spans_from_topk(full_text, offsets, keep_mask, start_char=len(prefix), merge_gap_chars=1)
    compressed = render_by_spans(full_text, spans)
    return compressed, len(spans)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i","--input", required=True, help="JSON/JSONL，含 id/question/cot")
    ap.add_argument("-m","--model-dir", required=True, help="已训练的 Longformer 压缩器目录")
    ap.add_argument("-o","--output", required=True, help="输出预览 JSONL（UTF-8）")
    ap.add_argument("--ratios", default="0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    data = read_any_utf8(args.input)
    tok = AutoTokenizer.from_pretrained(args.model_dir, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(args.model_dir, num_labels=2)
    model.to("cuda" if torch.cuda.is_available() else "cpu").eval()

    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    out_path = args.output
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    n = min(args.limit, len(data))
    with open(out_path, "w", encoding="utf-8", errors="strict") as f:
        for i, rec in enumerate(data[:n]):
            q = (rec.get("messages") or "")[0]
            
            q = q["content"]
            raw = (rec.get("model_output") or "").strip()
            cot = extract_think_span(raw)  # 仅提取 <think>…</think> 内文本；若没有则用全串
            must_no_replacement("question", q)
            must_no_replacement("cot", cot)

            row = {
                "id": rec.get("id", i),
                "question": q,
            }
            for r in ratios:
                comp, nsp = compress_one(q, cot, tok, model, keep_ratio=r)
                row[f"keep@{r:.2f}"] = comp
                row[f"spans@{r:.2f}"] = nsp
            # 严格 UTF-8 写出 + ensure_ascii=False
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"✅ 预览已写出：{out_path}")

if __name__ == "__main__":
    main()
