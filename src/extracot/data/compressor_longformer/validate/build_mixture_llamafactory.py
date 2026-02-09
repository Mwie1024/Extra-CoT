#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build a mixed dataset for controllable CoT compression:
- Mode A (follow):   input = Q + <COMP_xx>,     output = <COMP_xx> + COT (+ boxed)
- Mode B (auto):     input = Q + <COMP_AUTO>,   output = <COMP_xx> + COT (+ boxed)

Author: you + minor refactor by ChatGPT
"""

import os, re, json, random, argparse
import numpy as np
from typing import Dict, Any, List, Tuple
from tqdm import tqdm

# ----------------- IO -----------------

def load_jsonl(file, encoding='utf-8') -> List[Dict[str, Any]]:
    data = []
    with open(file, 'r', encoding=encoding) as f:
        for line in f:
            line = line.strip()
            if not line: continue
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
    if not text: return ""
    m = BOXED_RE.findall(text)
    return m[-1].strip() if m else ""

def extract_response_ans(text: str) -> str:
    if not text: return ""
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

# ----------------- Special-token encoding -----------------

def parse_special_buckets(s: str) -> List[float]:
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok: continue
        val = float(tok)
        if val > 1.0:  # 以百分整数给的
            val = val / 100.0
        out.append(val)
    out = sorted(set(out), reverse=True)
    return out

def build_special_token_map(buckets: List[float], prefix: str = "COMP_") -> Dict[float, str]:
    mp = {}
    for b in buckets:
        label = int(round(b * 100))
        mp[b] = f"<{prefix}{label}>"
    return mp

def nearest_bucket(r: float, buckets: List[float]) -> float:
    return min(buckets, key=lambda b: abs(b - r))

# ----------------- 核心 sample 组装 -----------------

def choose_source_record(
    r: float,
    id_: str,
    orig_by_id: Dict[str, Dict[str, Any]],
    lf_maps: Dict[float, Dict[str, Any]],
    ll2_maps: Dict[float, Dict[str, Any]],
) -> Dict[str, Any]:
    """按优先级选择该压缩比的素材记录"""
    orig = orig_by_id.get(id_, {})
    if r >= 0.999:
        return orig
    if id_ in lf_maps.get(r, {}):
        return lf_maps[r][id_]
    if id_ in ll2_maps.get(r, {}):
        return ll2_maps[r][id_]
    return orig  # 回退

def build_output_text_with_leading_token(comp_tok: str, chosen: Dict[str, Any], fallback: Dict[str, Any]) -> Tuple[str, bool]:
    """
    输出文本 = <COMP_xx> + compressed_cot (或回落 cot) + optional final answer
    返回: (文本, 是否含答案)
    """
    compressed_cot = (chosen.get("compressed_cot") or fallback.get("compressed_cot") or
                      chosen.get("model_output") or fallback.get("cot") or "").strip()
    ans = pick_answer(chosen) or pick_answer(fallback)
    prefix = f"{comp_tok}\n"
    if ans:
        out_text = prefix + compressed_cot + "\n\nThe final answer is: " + "$\\boxed{" + ans + "}$"
        return out_text, True
    else:
        return prefix + compressed_cot, False

def make_input_A(question: str, comp_tok: str) -> str:
    return f"{question} {comp_tok}"

def make_input_B_auto(question: str, auto_tok: str) -> str:
    return f"{question} {auto_tok}"

# ----------------- 混合构建器 -----------------

def build_mixed_dataset(
    original_path: str,
    longformer_dir: str,
    lingua_dir: str,
    ratios: List[float],                    # e.g. [1.0,0.8,0.6,0.4,0.2]
    special_buckets_str: str,              # e.g. "1.0,0.8,0.6,0.4,0.2"
    special_token_prefix: str,             # e.g. "COMP_"
    auto_token: str,                       # e.g. "<COMP_AUTO>"
    a_multi_count: int,                    # e.g. 10000  -> A-多档
    a_single_count: int,                   # e.g. 5000   -> A-单档
    b_count: int,                          # e.g. 7000   -> B-模式（每题1~2条）
    a_single_ratio_probs: Dict[float, float] = None,    # A-单档比例分布（可空=均匀）
    b_pattern_fracs: Dict[str, float] = None,          # {"one":0.7,"two":0.2,"comp100":0.1}
    b_ratio_probs: Dict[float, float] = None,          # B-模式里选非100%的比值分布
    seed: int = 42,
) -> Tuple[List[Dict[str, Any]], Dict[float, str], List[float], Dict[str, Any]]:

    seed_everything(seed)
    # 1) special token
    special_buckets = parse_special_buckets(special_buckets_str)
    special_map = build_special_token_map(special_buckets, prefix=special_token_prefix)

    # 2) load originals
    if not os.path.exists(original_path):
        raise FileNotFoundError(original_path)
    original = load_jsonl(original_path)

    orig_by_id: Dict[str, Dict[str, Any]] = {}
    ids: List[str] = []
    for rec in original:
        id_ = rec.get("id") or ""
        if id_:
            orig_by_id[id_] = rec
            ids.append(id_)
    if len(ids) == 0:
        raise RuntimeError("No valid 'id' found in original data.")

    # 3) load compressed
    lf_maps: Dict[float, Dict[str, Any]] = {}
    if longformer_dir:
        for r in ratios:
            if r >= 0.999: continue
            lst = load_ratio_file(longformer_dir, r)
            lf_maps[r] = build_id_map(lst)

    ll2_maps: Dict[float, Dict[str, Any]] = {}
    if lingua_dir:
        for r in ratios:
            if r >= 0.999: continue
            lst = load_ratio_file(lingua_dir, r)
            ll2_maps[r] = build_id_map(lst)

    # 4) sampling ids for A/B groups (无交集)
    all_ids = ids[:]
    random.shuffle(all_ids)

    def pop_k(k: int) -> List[str]:
        k = max(0, min(k, len(all_ids)))
        picked = all_ids[:k]
        del all_ids[:k]
        return picked

    ids_A_multi = pop_k(a_multi_count)
    ids_A_single = pop_k(a_single_count)
    ids_B       = pop_k(b_count)

    datalines: List[Dict[str, Any]] = []

    # 5) helpers: prob picking
    def pick_by_probs(items: List[float], probs: Dict[float, float] = None) -> float:
        if not items: raise RuntimeError("Empty ratio list.")
        if not probs:
            return random.choice(items)
        # normalize
        ps = np.array([probs.get(x, 0.0) for x in items], dtype=np.float64)
        if ps.sum() <= 0:
            return random.choice(items)
        ps = ps / ps.sum()
        return np.random.choice(items, p=ps)

    # A) A-多档：每题产出 |ratios| 条
    for id_ in tqdm(ids_A_multi, desc="Mode-A multi (all buckets)"):
        orig = orig_by_id[id_]
        question = get_question_from_original(orig)
        if not question: continue
        for r in ratios:
            r_b = nearest_bucket(r, special_buckets)
            comp_tok = special_map[r_b]
            chosen = choose_source_record(r_b, id_, orig_by_id, lf_maps, ll2_maps)
            out_text, _ = build_output_text_with_leading_token(comp_tok, chosen, orig)
            input_text = make_input_A(question, comp_tok)
            datalines.append({
                "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
                "input": input_text,
                "output": out_text,
            })

    # B) A-单档：每题只产出 1 档（按可选分布）
    for id_ in tqdm(ids_A_single, desc="Mode-A single"):
        orig = orig_by_id[id_]
        question = get_question_from_original(orig)
        if not question: continue
        r_choice = pick_by_probs(ratios, a_single_ratio_probs)
        r_b = nearest_bucket(float(r_choice), special_buckets)
        comp_tok = special_map[r_b]
        chosen = choose_source_record(r_b, id_, orig_by_id, lf_maps, ll2_maps)
        out_text, _ = build_output_text_with_leading_token(comp_tok, chosen, orig)
        input_text = make_input_A(question, comp_tok)
        datalines.append({
            "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
            "input": input_text,
            "output": out_text,
        })

    # C) B-模式：input 用 <COMP_AUTO>，输出首 token 监督为真实 <COMP_xx>
    #    按 pattern：one/two/comp100
    if not b_pattern_fracs:
        b_pattern_fracs = {"one": 0.7, "two": 0.2, "comp100": 0.1}
    # 预处理
    patterns, pvals = zip(*b_pattern_fracs.items())
    pvals = np.array(pvals, dtype=np.float64)
    pvals = pvals / pvals.sum()
    non100_ratios = [r for r in ratios if r < 0.999]

    for id_ in tqdm(ids_B, desc="Mode-B auto"):
        orig = orig_by_id[id_]
        question = get_question_from_original(orig)
        if not question: continue
        pattern = np.random.choice(patterns, p=pvals)

        # 统一构造输入
        input_text_auto = make_input_B_auto(question, auto_token)

        def emit_sample(r_target: float):
            r_b = nearest_bucket(float(r_target), special_buckets)
            comp_tok = special_map[r_b]
            chosen = choose_source_record(r_b, id_, orig_by_id, lf_maps, ll2_maps)
            out_text, _ = build_output_text_with_leading_token(comp_tok, chosen, orig)
            datalines.append({
                "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
                "input": input_text_auto,
                "output": out_text,
            })

        if pattern == "one":
            r_choice = pick_by_probs(non100_ratios, b_ratio_probs)
            emit_sample(r_choice)
        elif pattern == "two":
            # 两档：不重复
            if len(non100_ratios) >= 2:
                # 按 b_ratio_probs 采样两个不重复桶
                pool = non100_ratios[:]
                # 第一档
                r1 = pick_by_probs(pool, b_ratio_probs)
                emit_sample(r1)
                pool.remove(nearest_bucket(r1, special_buckets))
                # 第二档
                r2 = pick_by_probs(pool, b_ratio_probs)
                emit_sample(r2)
            else:
                # 兜底退化成 one
                r_choice = pick_by_probs(non100_ratios, b_ratio_probs)
                emit_sample(r_choice)
        elif pattern == "comp100":
            emit_sample(1.0)
        else:
            # 未知模式 -> one
            r_choice = pick_by_probs(non100_ratios, b_ratio_probs)
            emit_sample(r_choice)

    # 打乱
    random.shuffle(datalines)

    # 统计
    stats = {
        "A_multi_ids": len(ids_A_multi),
        "A_single_ids": len(ids_A_single),
        "B_ids": len(ids_B),
        "total_samples": len(datalines)
    }
    return datalines, special_map, special_buckets, stats

# ----------------- 导出 tokenizer special tokens -----------------

def dump_special_tokens_file(path: str, special_map: Dict[float, str], auto_token: str):
    toks = list(sorted(set(special_map.values()),
                       key=lambda x: (-int(re.findall(r"\d+", x)[0]), x)))
    if auto_token not in toks:
        toks.append(auto_token)
    obj = {"additional_special_tokens": toks}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# ----------------- CLI -----------------

def parse_ratio_probs(s: str) -> Dict[float, float]:
    """
    "1.0:0.1,0.8:0.2,0.6:0.3,0.4:0.2,0.2:0.2"
    """
    if not s: return {}
    mp = {}
    for seg in s.split(","):
        seg = seg.strip()
        if not seg: continue
        r, p = seg.split(":")
        r = float(r)
        if r > 1.0: r = r / 100.0
        mp[r] = float(p)
    return mp

def parse_b_pattern_fracs(s: str) -> Dict[str, float]:
    """
    "one:0.7,two:0.2,comp100:0.1"
    """
    if not s: return {}
    mp = {}
    for seg in s.split(","):
        seg = seg.strip()
        if not seg: continue
        k, v = seg.split(":")
        mp[k.strip()] = float(v)
    return mp

def main():
    ap = argparse.ArgumentParser(description="Build mixed dataset for A/B modes with <COMP_xx> & <COMP_AUTO>.")
    ap.add_argument("--original_path", required=True)
    ap.add_argument("--longformer_dir", default="")
    ap.add_argument("--lingua_dir", default="")
    ap.add_argument("--ratios", default="1.0,0.8,0.6,0.4,0.2")
    ap.add_argument("--special_buckets", default="1.0,0.8,0.6,0.4,0.2")
    ap.add_argument("--special_token_prefix", default="COMP_")
    ap.add_argument("--auto_token", default="<COMP_AUTO>")
    # 配额（示例：22k 原题 -> A_multi=10k, A_single=5k, B=7k）
    ap.add_argument("--a_multi_count", type=int, default=10000)
    ap.add_argument("--a_single_count", type=int, default=5000)
    ap.add_argument("--b_count", type=int, default=7000)
    # 分布
    ap.add_argument("--a_single_ratio_probs", default="", help='e.g. "1.0:0.1,0.8:0.2,0.6:0.3,0.4:0.2,0.2:0.2"')
    ap.add_argument("--b_pattern_fracs", default="one:0.7,two:0.2,comp100:0.1")
    ap.add_argument("--b_ratio_probs", default="", help='for non-100 ratios in Mode-B, e.g. "0.8:0.4,0.6:0.3,0.4:0.2,0.2:0.1"')
    ap.add_argument("--seed", type=int, default=42)
    # 输出
    ap.add_argument("--output_path", required=True)
    ap.add_argument("--special_tokens_out", default="")
    args = ap.parse_args()

    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    longformer_dir = args.longformer_dir or None
    lingua_dir = args.lingua_dir or None

    a_single_ratio_probs = parse_ratio_probs(args.a_single_ratio_probs)
    b_pattern_fracs      = parse_b_pattern_fracs(args.b_pattern_fracs)
    b_ratio_probs        = parse_ratio_probs(args.b_ratio_probs)

    lines, special_map, special_buckets, stats = build_mixed_dataset(
        original_path=args.original_path,
        longformer_dir=longformer_dir,
        lingua_dir=lingua_dir,
        ratios=ratios,
        special_buckets_str=args.special_buckets,
        special_token_prefix=args.special_token_prefix,
        auto_token=args.auto_token,
        a_multi_count=args.a_multi_count,
        a_single_count=args.a_single_count,
        b_count=args.b_count,
        a_single_ratio_probs=a_single_ratio_probs or None,
        b_pattern_fracs=b_pattern_fracs or None,
        b_ratio_probs=b_ratio_probs or None,
        seed=args.seed,
    )

    write_list_to_json(lines, args.output_path)
    print(f"Done. Wrote {len(lines)} samples to {args.output_path}")
    print("Stats:", json.dumps(stats, ensure_ascii=False))

    if args.special_tokens_out:
        dump_special_tokens_file(args.special_tokens_out, special_map, args.auto_token)
        print(f"Also wrote special tokens to {args.special_tokens_out}")

if __name__ == "__main__":
    main()
