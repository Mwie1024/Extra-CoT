#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
将 GPT-4o 的 ranges（基于 masked_text 的空白分词 + [MATH_i]）精确对齐为 Longformer 的 token 级标签。
与之前的不同点：
- 不再“用空格重拼”文本，而是严格在 masked_text 上原位还原 [MATH_i] -> 原 LaTeX，
  同时构造 masked->unmasked 的字符位置映射 pos_map，实现“精确原位”。
- 基于 pos_map，把 ranges（token 级）变成“原文字符级” keep-spans，再用 overlap 规则给 Longformer 的 token 打标签。

输出 .pt（list[dict]），每条 dict 包含：
  input_ids, attention_mask, global_attention_mask, labels(0/1/-100), kept_ratio, num_effective, id
"""

import os, re, json, argparse
from typing import List, Tuple, Dict, Any
from tqdm import tqdm
import torch
from transformers import LongformerTokenizerFast

WS_TOKEN_RE = re.compile(r"\S+")
MATH_PH_RE  = re.compile(r"\[MATH_(\d+)\]")
RANGE_ITEM_RE = re.compile(r"^\s*(\d+)\s*[-~–—]\s*(\d+)\s*$")

def read_any(path: str) -> List[dict]:
    """优先 JSON，失败退回 JSONL。统一返回 list[dict]。"""
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    try:
        obj = json.loads(txt)
        if isinstance(obj, list): return obj
        if isinstance(obj, dict): return [obj]
        raise ValueError("Top-level JSON is neither list nor dict")
    except Exception:
        # JSONL
        out = []
        for i, line in enumerate(txt.splitlines(), 1):
            s = line.strip()
            if not s: continue
            if s.endswith(","): s = s[:-1].strip()
            try:
                rec = json.loads(s)
                if isinstance(rec, dict): out.append(rec)
            except Exception:
                pass
        return out

def parse_ranges_to_bool(n_tokens: int, ranges: List[str], base: int = 1, closed: bool = True) -> List[int]:
    """把 1-based 闭区间字符串列表，转成长度为 n_tokens 的 0/1 向量。"""
    keep = [0] * n_tokens
    for item in ranges or []:
        m = RANGE_ITEM_RE.match(str(item))
        if not m: continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > b: a, b = b, a
        a0 = a - base
        b0 = b - base + (1 if closed else 0)   # -> 半开
        a0 = max(0, min(a0, n_tokens))
        b0 = max(0, min(b0, n_tokens))
        for i in range(a0, b0):
            keep[i] = 1
    return keep

def tokenize_masked_with_offsets(masked_text: str) -> Tuple[List[str], List[Tuple[int,int]]]:
    tokens, offs = [], []
    for m in WS_TOKEN_RE.finditer(masked_text or ""):
        tokens.append(m.group(0))
        offs.append((m.start(), m.end()))
    return tokens, offs

def unmask_with_posmap(masked_text: str, math_map: Dict[str,str]) -> Tuple[str, List[int]]:
    """
    严格原位还原：扫描 masked_text；遇到 [MATH_k] 用 math_map 原样替换，其它字符逐字抄写。
    同时构造 pos_map，长度= len(masked_text)+1，pos_map[i] = 还原后文本中，对应 masked_text[:i] 的输出长度。
    """
    s = masked_text or ""
    out_parts = []
    pos_map = [0]*(len(s)+1)
    i = 0
    out_len = 0
    while i < len(s):
        if s[i] == '[':
            m = re.match(r"\[MATH_\d+\]", s[i:])
            if m:
                j = i + m.end(0)
                ph = s[i:j]                         # 形如 [MATH_3]
                pos_map[i] = out_len
                seg = math_map.get(ph, ph)          # 原样替换（严格还原）
                out_parts.append(seg)
                out_len += len(seg)
                # 把 (i, j] 的内部位置都映射到“段末”，确保 token end 使用 pos_map[j]
                for k in range(i+1, j+1):
                    pos_map[k] = out_len
                i = j
                continue
        # 普通字符
        pos_map[i] = out_len
        out_parts.append(s[i])
        out_len += 1
        i += 1
    pos_map[len(s)] = out_len
    return "".join(out_parts), pos_map

def merge_spans(spans: List[Tuple[int,int]]) -> List[Tuple[int,int]]:
    if not spans: return []
    spans.sort(key=lambda x: (x[0], x[1]))
    out = [list(spans[0])]
    for a,b in spans[1:]:
        last = out[-1]
        if a <= last[1]:
            last[1] = max(last[1], b)
        else:
            out.append([a,b])
    return [(a,b) for a,b in out]

def label_tokens_by_char_spans(offsets: List[Tuple[int,int]],
                               keep_spans: List[Tuple[int,int]],
                               policy: str = "any") -> List[int]:
    """
    用字符级 keep_spans 给 token（offsets）打 0/1 标签。
    policy:
      - "any": token 与任一 keep span 有非零重叠 -> 1，否则 0（最保守，最接近“精确保持”）
      - "majority": 交叠长度 ≥ (R-L)/2 -> 1
      - "center": token 中点落在 keep span 内 -> 1（近似）
    special token（(0,0)）标 -100。
    """
    if not keep_spans:
        return [(-100 if (L,R)==(0,0) else 0) for (L,R) in offsets]
    merged = merge_spans(keep_spans)
    def any_overlap(L,R):
        for a,b in merged:
            if min(R,b) > max(L,a): return True
        return False
    def majority(L,R):
        w = R-L
        if w <= 0: return False
        ov = 0
        for a,b in merged:
            ov += max(0, min(R,b) - max(L,a))
        return ov*2 >= w
    def center(L,R):
        if R<=L: return False
        mid = (L+R)//2
        for a,b in merged:
            if a <= mid < b: return True
        return False
    fn = {"any":any_overlap, "majority":majority, "center":center}[policy]
    out = []
    for (L,R) in offsets:
        if (L,R)==(0,0):
            out.append(-100)
        else:
            out.append(1 if fn(L,R) else 0)
    return out

def build_one_sample_strict(rec: dict,
                            tokenizer: LongformerTokenizerFast,
                            max_length: int = 4096,
                            label_policy: str = "any") -> Dict[str, Any]:
    """
    精确原位版本：不重写原文。把 ranges（基于 masked_text 的 token indexes）映射到原文字符级 keep spans，
    再对齐到 Longformer token。
    """
    sid = rec.get("id")
    question = (rec.get("original_question") or rec.get("question") or rec.get("problem") or "").strip()
    chunks = rec.get("chunks") or rec.get("prompt_list") or rec.get("cot_chunks") or []

    # 1) 在 masked_text 层，按空白切分并得到 offsets
    all_masked = []
    all_masked_offs = []
    all_keep_bits = []
    for ch in chunks:
        masked = ch.get("masked_text") or ""
        math_map = ch.get("math_map") or {}
        ranges  = ch.get("ranges") or []

        toks, offs = tokenize_masked_with_offsets(masked)
        keep = parse_ranges_to_bool(len(toks), ranges, base=1, closed=True)
        all_masked.append(masked)
        all_masked_offs.append(offs)
        all_keep_bits.append(keep)

    # 2) 严格原位：把所有 chunk 的 masked_text 各自还原（得到原文片段）并建立 pos_map
    #    然后把“被保留的 token（masked offsets)”映射为“原文字符级 spans”
    cot_pieces = []
    keep_spans_char: List[Tuple[int,int]] = []
    cursor = 0  # 累计拼接的“原文”长度
    for ch, offs, keep in zip(chunks, all_masked_offs, all_keep_bits):
        masked = ch.get("masked_text") or ""
        math_map = ch.get("math_map") or {}
        unmasked, pos_map = unmask_with_posmap(masked, math_map)
        cot_pieces.append(unmasked)
        # masked token -> 原文字符 span
        for (k,(Lm,Rm)) in enumerate(offs):
            if keep[k]==1:
                Lr = pos_map[Lm]
                Rr = pos_map[Rm]
                if Rr > Lr:
                    keep_spans_char.append((cursor + Lr, cursor + Rr))
        cursor += len(unmasked)
        # chunk 之间原文本就带空白/换行；不额外插入空格

    cot_text = "".join(cot_pieces)

    # 3) 组合最终输入：在前面加上 <Q> ... </Q> <SEP> 空格，与之前一致
    q_prefix = f"<Q> {question} </Q> <SEP> "
    full_text = q_prefix + cot_text
    # 问题前缀整体作为 -1，训练时会被置为 -100
    q_end = len(q_prefix)
    shifted_spans = [(s+q_end, e+q_end) for (s,e) in merge_spans(keep_spans_char)]
    # 添加问题前缀的“忽略”大 span（label=-1） —— 用于 debug 时可见；打标签阶段我们会把 -1 置为 -100
    ignore_span = (0, q_end, -1)

    # 4) 分词并根据字符级 spans 打标签
    enc = tokenizer(full_text,
                    return_offsets_mapping=True,
                    padding=False,
                    truncation=True,
                    max_length=max_length)
    input_ids = enc["input_ids"]
    attention_mask = enc["attention_mask"]
    offsets = enc["offset_mapping"]

    # global attention：问题区域置 1
    global_attention_mask = [0]*len(input_ids)
    for i,(L,R) in enumerate(offsets):
        if (L,R)!=(0,0) and L < q_end:
            global_attention_mask[i] = 1

    # 先按 keep spans 标 0/1
    labels = label_tokens_by_char_spans(offsets, shifted_spans, policy=label_policy)
    # 再把问题部分清成 -100（忽略）
    for i,(L,R) in enumerate(offsets):
        if (L,R)!=(0,0) and L < q_end:
            labels[i] = -100

    eff = sum(1 for x in labels if x != -100)
    kept = sum(1 for x in labels if x == 1)
    kept_ratio = (kept/eff) if eff>0 else 0.0

    return {
        "id": sid,
        "input_ids": torch.tensor(input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
        "global_attention_mask": torch.tensor(global_attention_mask, dtype=torch.long),
        "labels": torch.tensor(labels, dtype=torch.long),
        "kept_ratio": kept_ratio,
        "num_effective": eff,
        # 下面三项仅调试时有用，可按需注释掉以减小体积
        # "text": full_text,
        # "question_end": q_end,
        # "spans_char": shifted_spans,
    }

def main():
    ap = argparse.ArgumentParser(description="将 ranges 严格对齐为 Longformer token 级训练样本（精确原位）")
    ap.add_argument("-i","--input", required=True, help="GPT-4o 标注结果（JSON/JSONL）")
    ap.add_argument("-o","--output", required=True, help="输出 .pt（torch.save 的 list[dict]）")
    ap.add_argument("--tokenizer", default="/Users/mwie/Downloads/longformer", help="Longformer 分词器名或本地路径")
    ap.add_argument("--max-length", type=int, default=4096)
    ap.add_argument("--label-policy", choices=["any","majority","center"], default="any",
                    help="token 打 1 的判定：any(推荐)/majority/center")
    args = ap.parse_args()

    data = read_any(args.input)
    if not data:
        raise RuntimeError("输入为空或无法解析")

    tok = LongformerTokenizerFast.from_pretrained(args.tokenizer)

    out, bad = [], 0
    for rec in tqdm(data, desc="Building(strict)"):
        try:
            item = build_one_sample_strict(rec, tok, max_length=args.max_length, label_policy=args.label_policy)
            out.append(item)
        except Exception as e:
            bad += 1
            # 可按需打印：print(f"[WARN] id={rec.get('id')} -> {e}")

    torch.save(out, args.output)

    eff_tokens = sum(int(o["num_effective"]) for o in out)
    kept_tokens = 0
    for o in out:
        labels = o["labels"].tolist()
        kept_tokens += sum(1 for x in labels if x == 1)
    kept_rate = (kept_tokens / eff_tokens) if eff_tokens>0 else 0.0

    print("\n=== 转换完成（严格原位） ===")
    print(f"样本数: {len(out)}（失败跳过: {bad}）")
    print(f"总有效 tokens: {eff_tokens}；保留=1: {kept_tokens}；保留占比: {kept_rate:.3f}")
    print(f"输出: {args.output}")

if __name__ == "__main__":
    main()
