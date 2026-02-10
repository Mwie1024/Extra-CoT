#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import random
import argparse
import numpy as np
from typing import Dict, Any, List
import re
from tqdm import tqdm

# ----------------- IO -----------------

def load_jsonl(file, encoding='utf-8') -> List[Dict[str, Any]]:
    data = []
    with open(file, 'r', encoding=encoding) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data.append(json.loads(line))
            except Exception:
                pass
    return data

def write_list_to_json(lst, file_path):
    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(lst, f, ensure_ascii=False, indent=1)

# ----------------- Seed -----------------

def seed_everything(seed: int):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)

# ----------------- Answer extraction -----------------

BOXED_RE = re.compile(r"\\boxed\s*\{([^}]*)\}", flags=re.IGNORECASE)
ANS_RE   = re.compile(r"the\s+answer\s+is:\s*(.+)\s*$", flags=re.IGNORECASE | re.DOTALL)

def extract_boxed(text: str) -> str:
    if not text:
        return ""
    m = BOXED_RE.findall(text)
    return m[-1].strip() if m else ""

def extract_response_ans(text: str) -> str:
    if not text:
        return ""
    last = None
    for m in ANS_RE.finditer(text):
        last = m.group(1)
    return last.strip() if last else ""

def pick_answer(rec: Dict[str, Any]) -> str:
    # 常见字段兜底
    for k in ["prediction", "model_answer", "answer"]:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    ans = extract_response_ans(rec.get("response", "") or "")
    if ans:
        return ans
    boxed = extract_boxed(rec.get("model_output", "") or "")
    if boxed:
        return boxed
    return ""

# ----------------- Helpers -----------------

def load_ratio_file(dir_path: str, ratio: float) -> List[Dict[str, Any]]:
    # 文件名示例：train_outputs_compressed_ratio_0.6.jsonl
    path = os.path.join(dir_path, f"train_outputs_compressed_ratio_{ratio:.1f}.jsonl")
    if not os.path.exists(path):
        return []
    return load_jsonl(path)

def build_id_map(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mp = {}
    for x in items:
        id_ = x.get("id")
        if id_:
            mp[id_] = x
    return mp

def get_question_from_original(rec: Dict[str, Any]) -> str:
    for k in ["original_question", "question", "query", "prompt"]:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

# ----------------- Special-token encoding -----------------

def parse_special_buckets(s: str) -> List[float]:
    """
    支持两种写法：
      '1.0,0.8,0.6,0.4,0.2'  或  '100,80,60,40,20'
    返回降序去重列表（例如 [1.0,0.8,0.6,0.4,0.2]）
    """
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        val = float(tok)
        if val > 1.0:  # 以百分整数给的
            val = val / 100.0
        out.append(val)
    out = sorted(set(out), reverse=True)
    return out

def build_special_token_map(buckets: List[float], prefix: str = "COMP_") -> Dict[float, str]:
    """0.8 -> <COMP_80>, 1.0 -> <COMP_100>"""
    mp = {}
    for b in buckets:
        label = int(round(b * 100))
        mp[b] = f"<{prefix}{label}>"
    return mp

def nearest_bucket(r: float, buckets: List[float]) -> float:
    return min(buckets, key=lambda b: abs(b - r))

def make_input_text_special(question: str, ratio: float, special_map: Dict[float, str], special_buckets: List[float]) -> str:
    r = nearest_bucket(float(ratio), special_buckets)
    tok = special_map[r]
    # 只保留 question + <COMP_xx>
    return f"{question} {tok}"

# ----------------- 组装一条样本 -----------------

def build_sample_for_ratio(
    id_: str,
    ratio: float,
    special_map: Dict[float, str],
    special_buckets: List[float],
    orig_by_id: Dict[str, Dict[str, Any]],
    lf_maps: Dict[float, Dict[str, Any]],
    ll2_maps: Dict[float, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    只做两件事：
      1) input = 原始样本的 question + 对应 <COMP_xx>（1.0 -> <COMP_100>）
      2) output = compressed_cot + "\\n\\nThe final answer is: $\\boxed{ans}$"
         - 若当前比值缺 compressed_cot，则回落到该样本的 cot（可能为空字符串）
    """
    r = float(ratio)
    orig = orig_by_id.get(id_, {})
    if not orig:
        return {}

    # 输入：始终取“原样本”的 question
    question = get_question_from_original(orig)
    if not question:
        return {}

    input_text = make_input_text_special(question, r, special_map, special_buckets)

    # 输出素材的选择：r<1.0 优先 longformer 其次 llmlingua2；r==1.0 用原样本
    chosen = orig
    if r < 0.999:
        if id_ in lf_maps.get(r, {}):
            chosen = lf_maps[r][id_]
        elif id_ in ll2_maps.get(r, {}):
            chosen = ll2_maps[r][id_]
        else:
            # 没有该比值的压缩结果，也照样产出（compressed_cot 回落为 orig.cot 或空）
            chosen = orig

    # 组织 output：compressed_cot（或回落 cot） + final answer
    compressed_cot = (chosen.get("compressed_cot") or orig.get("compressed_cot") or
                      chosen.get("model_output") or orig.get("cot") or "").strip()
    ans = pick_answer(chosen) or pick_answer(orig)

    if ans:
        out_text = f"{compressed_cot}\n\nThe final answer is: " + "$\\boxed{" + ans + "}$"
    else:
        # 没答案就不拼 final 行，只保留 cot（避免产生空的 boxed）
        out_text = compressed_cot

    return {
        "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
        "input": input_text,
        "output": out_text,
    }

# ----------------- 主构建器 -----------------

def build_llamafactory_dataset_special(
    original_path: str,
    longformer_dir: str = None,
    lingua_dir: str = None,
    ratios: List[float] = (1.0, 0.8, 0.6, 0.4, 0.2),
    special_buckets_str: str = "1.0,0.8,0.6,0.4,0.2",
    special_token_prefix: str = "COMP_",
    sample_limit: int = 0,
) -> (List[Dict[str, Any]], Dict[float, str], List[float]):

    # 1) special-token 相关准备
    special_buckets = parse_special_buckets(special_buckets_str)
    special_map = build_special_token_map(special_buckets, prefix=special_token_prefix)

    # 2) 载入原始
    if not os.path.exists(original_path):
        raise FileNotFoundError(original_path)
    original = load_jsonl(original_path)
    if sample_limit and sample_limit > 0:
        original = original[:sample_limit]

    orig_by_id: Dict[str, Dict[str, Any]] = {}
    for rec in original:
        id_ = rec.get("id") or ""
        if id_:
            orig_by_id[id_] = rec

    # 3) 载入压缩并建索引
    lf_maps: Dict[float, Dict[str, Any]] = {}
    if longformer_dir:
        for r in ratios:
            if r >= 0.999:
                continue
            lst = load_ratio_file(longformer_dir, r)
            lf_maps[r] = build_id_map(lst)

    ll2_maps: Dict[float, Dict[str, Any]] = {}
    if lingua_dir:
        for r in ratios:
            if r >= 0.999:
                continue
            lst = load_ratio_file(lingua_dir, r)
            ll2_maps[r] = build_id_map(lst)

    # 4) 为“每条原文 × 每个比值”构样本
    datalines: List[Dict[str, Any]] = []
    for rec in tqdm(original, desc="Packing <COMP_xx> samples"):
        id_ = rec.get("id") or ""
        if not id_:
            continue
        for r in ratios:
            sample = build_sample_for_ratio(
                id_=id_,
                ratio=r,
                special_map=special_map,
                special_buckets=special_buckets,
                orig_by_id=orig_by_id,
                lf_maps=lf_maps,
                ll2_maps=ll2_maps,
            )
            if sample:
                datalines.append(sample)

    return datalines, special_map, special_buckets

# ----------------- 导出 tokenizer special tokens -----------------

def dump_special_tokens_file(path: str, special_map: Dict[float, str]):
    """
    输出一个 JSON（便于直接传给 tokenizer 的 additional_special_tokens）：
    {
      "additional_special_tokens": ["<COMP_100>","<COMP_80>",...]
    }
    """
    toks = list(sorted(set(special_map.values()),
                       key=lambda x: (-int(re.findall(r"\d+", x)[0]), x)))
    obj = {"additional_special_tokens": toks}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# ----------------- CLI -----------------

def main():
    ap = argparse.ArgumentParser(description="Build dataset: input=question+<COMP_xx>, output=compressed_cot + final answer.")
    ap.add_argument("--original_path", required=True, help="原始 predictions_formatted.jsonl")
    ap.add_argument("--longformer_dir", default="", help="Longformer 压缩结果目录（含 train_outputs_compressed_ratio_*.jsonl），可留空")
    ap.add_argument("--lingua_dir", default="", help="LLMLingua-2 压缩结果目录（含 train_outputs_compressed_ratio_*.jsonl），可留空")
    ap.add_argument("--ratios", default="0.8,0.6,0.4,0.2", help="目标压缩比（含1.0代表原始）")
    ap.add_argument("--special_buckets", default="1.0,0.8,0.6,0.4,0.2", help="special token 桶位（用于近邻映射）")
    ap.add_argument("--special_token_prefix", default="COMP_", help="special token 前缀，形如 <COMP_60>")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sample_limit", type=int, default=0, help="只取前N条原始样本（0=不限）")
    ap.add_argument("--output_path", required=True, help="输出 JSON（LLaMA-Factory 格式）")
    ap.add_argument("--special_tokens_out", default="", help="若提供路径，则额外输出 tokenizer special tokens JSON")
    args = ap.parse_args()

    seed_everything(args.seed)
    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    longformer_dir = args.longformer_dir or None
    lingua_dir = args.lingua_dir or None

    lines, special_map, special_buckets = build_llamafactory_dataset_special(
        original_path=args.original_path,
        longformer_dir=longformer_dir,
        lingua_dir=lingua_dir,
        ratios=ratios,
        special_buckets_str=args.special_buckets,
        special_token_prefix=args.special_token_prefix,
        sample_limit=args.sample_limit,
    )

    random.shuffle(lines)
    write_list_to_json(lines, args.output_path)
    print(f"Done. Wrote {len(lines)} samples to {args.output_path}")

    if args.special_tokens_out:
        dump_special_tokens_file(args.special_tokens_out, special_map)
        print(f"Also wrote special tokens to {args.special_tokens_out}")

if __name__ == "__main__":
    main()
