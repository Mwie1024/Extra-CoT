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
RE_THINK = re.compile(r"<think>([\s\S]*?)</think>", flags=re.IGNORECASE)

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

def extract_post_think_suffix(full_output: str) -> str:
    """
    从原始 model_output 中截取“最后一个 </think> 之后的所有文本”（不含 </think> 本身）。
    若没有 <think>…</think>，返回原文（表示整个都是“可见部分”）。
    """
    if not full_output:
        return ""
    # 搜索最后一个 </think>
    close_tags = [m for m in re.finditer(r"</think>", full_output, flags=re.IGNORECASE)]
    if not close_tags:
        return full_output.strip()
    end_idx = close_tags[-1].end()
    return full_output[end_idx:].lstrip()

def strip_existing_final_answer(text: str) -> str:
    """
    去掉可见部分里已有的 "The final answer is: ..." 行，避免重复。
    也顺带去掉非常见变体（大小写差异）。
    """
    if not text:
        return ""
    lines = text.splitlines()
    kept = []
    for ln in lines:
        if re.search(r"^\s*the\s+final\s+answer\s+is\s*:", ln, flags=re.IGNORECASE):
            continue
        kept.append(ln)
    return "\n".join(kept).rstrip()

# ----------------- Ratio encoding (numeric / special) -----------------

def parse_special_buckets(s: str) -> List[float]:
    """
    支持两种写法：
      "1.0,0.9,0.8,0.7,0.6,0.5"  或  "100,90,80,70,60,50"
    """
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if float(tok) > 1.0:  # 以百分整数给的
            out.append(float(tok) / 100.0)
        else:
            out.append(float(tok))
    # 去重并排序，大到小或小到大都可，这里统一降序
    out = sorted(set(out), reverse=True)
    return out

def build_special_token_map(buckets: List[float], prefix: str = "comp") -> Dict[float, str]:
    """
    0.8 -> <comp80>, 1.0 -> <comp100>
    """
    mp = {}
    for b in buckets:
        label = int(round(b * 100))
        mp[b] = f"<{prefix}{label}>"
    return mp

def nearest_bucket(r: float, buckets: List[float]) -> float:
    return min(buckets, key=lambda b: abs(b - r))

def make_input_text(
    question: str,
    ratio: float,
    model_type: str,
    ratio_encoding: str,
    special_map: Dict[float, str] = None,
    special_buckets: List[float] = None,
) -> str:
    """
    - numeric: Qwen => 在末尾注入 <|eot_id|>{ratio:.1f}<|eot_id|>（ratio<1.0 时注入；1.0 不注入）
              Llama3 => 追加一行 "compression_ratio: {ratio:.1f}"（ratio<1.0 时注入；1.0 不注入）
    - special: 总是前缀一个 special token（包括 1.0 的 <comp100>）
    """
    model_type = (model_type or "qwen").lower()
    ratio_encoding = (ratio_encoding or "numeric").lower()
    ratio = float(ratio)

    if ratio_encoding == "special":
        if not special_map or not special_buckets:
            raise ValueError("special token encoding 需要提供 special_map 与 special_buckets")
        r = nearest_bucket(ratio, special_buckets)
        tok = special_map[r]
        # Special token 作为独立前缀行（不要放进 eot 控制符里）
        return f"{question} {tok}\n"

    # numeric
    if model_type == "qwen":
        if ratio >= 0.999:
            return question
        return f"{question}<|eot_id|>{ratio:.1f}<|eot_id|>"
    else:  # llama3 or others
        if ratio >= 0.999:
            return question
        return f"{question}\ncompression_ratio: {ratio:.1f}"

# ----------------- Main builder -----------------

def build_llamafactory_dataset(
    original_path: str,
    longformer_dir: str = None,
    lingua_dir: str = None,
    ratios: List[float] = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5),
    model_type: str = "qwen",
    ratio_encoding: str = "numeric",          # "numeric" | "special"
    special_buckets_str: str = "1.0,0.9,0.8,0.7,0.6,0.5",
    special_token_prefix: str = "comp",
    sample_limit: int = 0,
) -> List[Dict[str, Any]]:

    # special-token 相关准备
    special_buckets: List[float] = None
    special_map: Dict[float, str] = None
    if ratio_encoding.lower() == "special":
        special_buckets = parse_special_buckets(special_buckets_str)
        special_map = build_special_token_map(special_buckets, prefix=special_token_prefix)

    # 1) 原始数据
    if not os.path.exists(original_path):
        raise FileNotFoundError(original_path)
    original = load_jsonl(original_path)
    if sample_limit and sample_limit > 0:
        original = original[:sample_limit]

    answer_by_id: Dict[str, str] = {}
    orig_by_id: Dict[str, Dict[str, Any]] = {}
    for rec in original:
        id_ = rec.get("id") or ""
        if id_:
            orig_by_id[id_] = rec
            answer_by_id[id_] = pick_answer(rec)

    # 2) 压缩数据加载并按 id 建索引
    # Longformer
    lf_maps: Dict[float, Dict[str, Any]] = {}
    if longformer_dir:
        for r in ratios:
            if r >= 0.999:
                continue
            lst = load_ratio_file(longformer_dir, r)
            lf_maps[r] = build_id_map(lst)

    # LLMLingua-2
    ll2_maps: Dict[float, Dict[str, Any]] = {}
    if lingua_dir:
        for r in ratios:
            if r >= 0.999:
                continue
            lst = load_ratio_file(lingua_dir, r)
            ll2_maps[r] = build_id_map(lst)

    # 3) 构造 LLaMA-Factory 行
    datalines: List[Dict[str, Any]] = []
    for rec in tqdm(original, desc="Packing LLaMA-Factory samples"):
        id_ = rec.get("id") or ""
        if not id_:
            continue

        # 可选来源列表：("src", ratio, record)
        candidates = [("original", 1.0, rec)]

        for r in ratios:
            if r >= 0.999:
                continue
            if longformer_dir and id_ in lf_maps.get(r, {}):
                candidates.append(("longformer", r, lf_maps[r][id_]))
            if lingua_dir and id_ in ll2_maps.get(r, {}):
                candidates.append(("llmlingua2", r, ll2_maps[r][id_]))

        src, r, chosen = random.choice(candidates)

        # 组装 input / output
        if src == "original":
            question = get_question_from_original(chosen)
            input_text = make_input_text(
                question, r, model_type=model_type,
                ratio_encoding=ratio_encoding,
                special_map=special_map,
                special_buckets=special_buckets
            )
            # 原始样本直接用原 model_output（若没有则退回 cot）
            full_out = chosen.get("model_output") or chosen.get("output") or ""
            if not full_out:
                # 极端回退：把已有 cot + 统一 final answer
                cot_text = chosen.get("cot") or ""
                ans = answer_by_id.get(id_, "") or pick_answer(chosen)
                full_out = f"{cot_text}\n\nThe final answer is: " + "$\\boxed{" + ans + "}$" if ans else cot_text
            out_text = full_out

        else:
            # 压缩记录：需要 <think>包装 + 原可见部分 + 统一 final answer
            question = chosen.get("question") or get_question_from_original(orig_by_id.get(id_, {}) or {})
            input_text = make_input_text(
                question, r, model_type=model_type,
                ratio_encoding=ratio_encoding,
                special_map=special_map,
                special_buckets=special_buckets
            )

            compressed_cot = (chosen.get("compressed_cot") or "").strip()
            # 从压缩条目自身带的 output（原始 model_output）优先取；没有就去原始表找
            orig_full_output = (chosen.get("output") or
                                (orig_by_id.get(id_, {}) or {}).get("model_output") or
                                (orig_by_id.get(id_, {}) or {}).get("output") or "")

            post_visible = strip_existing_final_answer(extract_post_think_suffix(orig_full_output))
            # 包装 think
            think_wrapped = f"<think>\n{compressed_cot}\n</think>"
            # 统一 final answer
            ans = pick_answer(chosen) or answer_by_id.get(id_, "") or pick_answer(orig_by_id.get(id_, {}) or {})
            if not ans:
                # 若实在提不出答案，也先不加最终行
                out_text = f"{think_wrapped}\n{post_visible}".rstrip()
            else:
                out_text = f"{think_wrapped}\n{post_visible}\n\nThe final answer is: " + "$\\boxed{" + ans + "}$"

        datalines.append({
            "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
            "input": input_text,
            "output": out_text
        })

    return datalines

# ----------------- CLI -----------------

def main():
    ap = argparse.ArgumentParser(description="Build LLaMA-Factory dataset from original + Longformer/LLMLingua2 compressed outputs, with ratio encoding switch and post-think concat.")
    ap.add_argument("--original_path", required=True, help="原始 predictions_formatted.jsonl")
    ap.add_argument("--longformer_dir", default="", help="Longformer 压缩结果目录（含 train_outputs_compressed_ratio_*.jsonl），可留空")
    ap.add_argument("--lingua_dir", default="", help="LLMLingua-2 压缩结果目录（含 train_outputs_compressed_ratio_*.jsonl），可留空")
    ap.add_argument("--ratios", default="1.0,0.9,0.8,0.7,0.6,0.5", help="候选压缩比（含1.0代表原始）")
    ap.add_argument("--model_type", choices=["qwen", "llama3"], default="qwen", help="决定数值注入的风格")
    ap.add_argument("--ratio_encoding", choices=["numeric", "special"], default="numeric", help="压缩比注入方案开关")
    ap.add_argument("--special_buckets", default="1.0,0.9,0.8,0.7,0.6,0.5", help="special token 桶位（可用百分整数或小数），如 100,90,80 或 1.0,0.9,0.8")
    ap.add_argument("--special_token_prefix", default="comp", help="special token 前缀，生成形如 <comp90>")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--sample_limit", type=int, default=0, help="只取前N条原始样本（0=不限）")
    ap.add_argument("--output_path", required=True, help="输出 JSON（LLaMA-Factory 格式）")
    args = ap.parse_args()

    seed_everything(args.seed)

    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    longformer_dir = args.longformer_dir or None
    lingua_dir = args.lingua_dir or None

    lines = build_llamafactory_dataset(
        original_path=args.original_path,
        longformer_dir=longformer_dir,
        lingua_dir=lingua_dir,
        ratios=ratios,
        model_type=args.model_type,
        ratio_encoding=args.ratio_encoding,
        special_buckets_str=args.special_buckets,
        special_token_prefix=args.special_token_prefix,
        sample_limit=args.sample_limit,
    )

    random.shuffle(lines)
    write_list_to_json(lines, args.output_path)
    print(f"Done. Wrote {len(lines)} samples to {args.output_path}")

if __name__ == "__main__":
    main()
