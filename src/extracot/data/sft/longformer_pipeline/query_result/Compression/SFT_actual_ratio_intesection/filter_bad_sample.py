#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, argparse, math, statistics
from collections import Counter, defaultdict

import tiktoken

THINK_OPEN  = re.compile(r"<think>", flags=re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", flags=re.IGNORECASE)

def extract_think(text: str) -> str:
    if not text: return ""
    m1 = THINK_OPEN.search(text); m2 = THINK_CLOSE.search(text)
    if m1 and m2 and m1.start() < m2.start():
        return text[m1.end():m2.start()]
    return ""

def words_for_ngrams(s: str):
    # 更宽松：仅用空白切词并小写；保留数字/latex 符号
    return [w for w in re.split(r"\s+", s.lower().strip()) if w]

def ngrams(seq, n):
    return [tuple(seq[i:i+n]) for i in range(len(seq)-n+1)] if len(seq) >= n else []

def repetition_ratio(grams):
    if not grams: return 0.0
    uniq = len(set(grams))
    return max(0.0, 1.0 - uniq / len(grams))

def max_consecutive_run(grams):
    if not grams: return 0
    mx, cur = 1, 1
    for i in range(1, len(grams)):
        if grams[i] == grams[i-1]:
            cur += 1
            mx = max(mx, cur)
        else:
            cur = 1
    return mx

def digit_like_fraction(words):
    # 数字/latex 占比（降权用）
    digitish = 0
    for w in words:
        if w.isdigit() or re.fullmatch(r"[0-9\.\-+/=]+", w) or w.startswith("\\"):
            digitish += 1
    return digitish / max(1, len(words))

def build_tokenizer(enc_name: str):
    try:
        return tiktoken.get_encoding(enc_name)
    except Exception:
        # 兜底
        return tiktoken.get_encoding("cl100k_base")

def count_tokens(enc, s: str) -> int:
    if not s: return 0
    return len(enc.encode(s))

def parse_ratio_from_name(fname: str) -> float:
    # recat_r020.jsonl / recat_r100.jsonl
    m = re.search(r"r(\d{3})", fname)
    if not m: return -1.0
    v = int(m.group(1))
    return {20:0.2, 40:0.4, 60:0.6, 80:0.8, 100:1.0}.get(v, -1.0)

def load_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s: continue
            try:
                yield json.loads(s)
            except Exception:
                continue

def save_jsonl(items, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def risk_score(feats: dict) -> float:
    # 温和权重：更看 tri-gram 循环，其次 bi-gram；数字密集降权
    tri = feats["tri_rep_ratio"]
    bi  = feats["bi_rep_ratio"]
    mr3 = feats["max_run_3"]
    mr2 = feats["max_run_2"]
    ttr = feats["ttr"]
    dig = feats["digit_frac"]
    # run 长度归一 & 轻度惩罚低 ttr；数字多 → 降权（避免误杀数学）
    score = (0.70 * tri) + (0.45 * bi) + 0.12 * (mr3/8.0) + 0.08 * (mr2/12.0) + 0.15 * (1.0 - ttr)
    score *= (1.0 - 0.25 * min(0.9, dig))  # 至多降 22.5%
    return float(score)

def is_hard_bad(feats: dict, ratio: float, min_len_by_ratio: dict) -> (bool, str):
    if feats["tok_len"] == 0:
        return True, "empty_think"
    if ratio in min_len_by_ratio and feats["tok_len"] < min_len_by_ratio[ratio]:
        return True, f"too_short({feats['tok_len']})"
    if feats["max_run_3"] >= 5 or feats["max_run_2"] >= 8 or feats["max_run_1"] >= 25:
        return True, f"long_loop(run1={feats['max_run_1']},run2={feats['max_run_2']},run3={feats['max_run_3']})"
    return False, ""

def analyze_record(rec, enc):
    # 兼容字段：优先 output；若无则看 messages/assistant
    raw = rec.get("output") or ""
    if not raw and "messages" in rec:
        for m in rec["messages"]:
            if m.get("role") == "assistant":
                raw = m.get("content") or ""
                break
    think = extract_think(raw)
    words = words_for_ngrams(think)
    bi = ngrams(words, 2); tri = ngrams(words, 3)
    feats = {
        "tok_len": count_tokens(enc, think),
        "ttr": (len(set(words)) / max(1, len(words))) if words else 0.0,
        "bi_rep_ratio": repetition_ratio(bi),
        "tri_rep_ratio": repetition_ratio(tri),
        "max_run_1": max_consecutive_run(ngrams(words, 1)),
        "max_run_2": max_consecutive_run(bi),
        "max_run_3": max_consecutive_run(tri),
        "digit_frac": digit_like_fraction(words),
    }
    feats["risk"] = risk_score(feats)
    return feats, think

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", required=True, help="包含 recat_r020.jsonl ... recat_r100.jsonl 的目录")
    ap.add_argument("--out_dir",  required=True, help="清洗后的输出目录")
    ap.add_argument("--encoding", default="cl100k_base", help="tiktoken 编码名")
    # ↓↓↓ 可按需微调：整体非常温和 ↓↓↓
    ap.add_argument("--drop020", type=float, default=0.08)
    ap.add_argument("--drop040", type=float, default=0.06)
    ap.add_argument("--drop060", type=float, default=0.03)
    ap.add_argument("--drop080", type=float, default=0.015)
    ap.add_argument("--drop100", type=float, default=0.01)
    args = ap.parse_args()

    enc = build_tokenizer(args.encoding)

    # 仅对 0.2/0.4 设很低的保底长度，其它挡位不设（尽量少动）
    min_len_by_ratio = {0.2: 80, 0.4: 100}

    drop_rate_map = {
        0.2: max(0.0, min(0.25, args.drop020)),
        0.4: max(0.0, min(0.25, args.drop040)),
        0.6: max(0.0, min(0.25, args.drop060)),
        0.8: max(0.0, min(0.25, args.drop080)),
        1.0: max(0.0, min(0.25, args.drop100)),
    }

    files = [f for f in os.listdir(args.data_dir) if re.search(r"recat_r(020|040|060|080|100)\.jsonl$", f)]
    files.sort()

    for fname in files:
        ratio = parse_ratio_from_name(fname)
        path = os.path.join(args.data_dir, fname)
        data = list(load_jsonl(path))
        feats_list = []
        hard_bad_idx = []
        for i, rec in enumerate(data):
            feats, _ = analyze_record(rec, enc)
            bad, why = is_hard_bad(feats, ratio, min_len_by_ratio)
            feats["_why"] = why
            feats_list.append(feats)
            if bad:
                hard_bad_idx.append(i)

        N = len(data)
        hard_bad_set = set(hard_bad_idx)

        # 软淘汰候选：排除已命中硬规则的，且满足“确有复读征兆”的样本
        candidates = []
        for i, feats in enumerate(feats_list):
            if i in hard_bad_set: continue
            cond_flag = (
                (feats["tri_rep_ratio"] >= 0.42 and feats["max_run_3"] >= 3) or
                (feats["bi_rep_ratio"]  >= 0.60 and feats["max_run_2"] >= 4 and feats["ttr"] <= 0.38)
            )
            if cond_flag:
                candidates.append((i, feats["risk"]))

        # 按风险从高到低，最多剔除目标比例
        target_drop = int(math.ceil(N * drop_rate_map.get(ratio, 0.05)))
        candidates.sort(key=lambda x: x[1], reverse=True)
        soft_drop_idx = set([i for i, _ in candidates[:target_drop]])

        # 汇总
        drop_idx = sorted(hard_bad_set | soft_drop_idx)
        keep = []
        drop = []
        for i, rec in enumerate(data):
            record = dict(rec)
            record["_metrics"] = feats_list[i]
            if i in drop_idx:
                record["_drop_reason"] = feats_list[i]["_why"] or "soft_risk_topK"
                drop.append(record)
            else:
                keep.append(record)

        kept, dropped = len(keep), len(drop)
        print(f"[{fname}] ratio={ratio:.1f} kept={kept} dropped={dropped} total={N} "
              f"(hard={len(hard_bad_set)} soft={len(soft_drop_idx)})")

        # 写盘
        save_jsonl(keep, os.path.join(args.out_dir, f"filtered_{fname}"))
        save_jsonl(drop, os.path.join(args.out_dir, f"dropped_{fname}"))

if __name__ == "__main__":
    main()
