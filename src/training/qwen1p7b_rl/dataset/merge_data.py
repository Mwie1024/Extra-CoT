#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import json, os, re, argparse, sys, hashlib
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

# ---------------- tiktoken（可选）----------------
try:
    import tiktoken  # type: ignore
    _ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(s: str) -> int:
        return len(_ENC.encode(s or ""))
    TOKENIZER_NAME = "tiktoken:cl100k_base"
except Exception:
    def count_tokens(s: str) -> int:
        return max(1, len(s or "") // 4)
    TOKENIZER_NAME = "fallback:len//4"

# ---------------- Regex 工具 ----------------
BOXED_LAST  = re.compile(r"\\boxed\s*\{([^}]*)\}.*$", flags=re.I | re.S)
GT_LASTLINE = re.compile(r"The\s+answer\s+is\s*:\s*(.+?)(?=$|\n)", flags=re.I)
THINK_SPAN  = re.compile(r"<think>(.*?)</think>", flags=re.I | re.S)

def _last_match(regex: re.Pattern, s: str) -> Optional[re.Match]:
    last = None
    for m in regex.finditer(s or ""):
        last = m
    return last

def extract_last_boxed(text: str) -> Optional[str]:
    m = _last_match(BOXED_LAST, text or "")
    return m.group(1).strip() if m else None

def extract_gold_from_response(response: str) -> Optional[str]:
    matches = list(GT_LASTLINE.finditer((response or "").strip()))
    return matches[-1].group(1).strip() if matches else None

def extract_think_inner(text: str) -> str:
    m = THINK_SPAN.search(text or "")
    return m.group(1) if m else ""

# ---- 轻量等价判定（与评测脚本思路一致）----
def _strip_latex(s: str) -> str:
    if s is None: return ""
    s = s.strip()
    if s.startswith("$") and s.endswith("$"): s = s[1:-1].strip()
    if s.startswith(r"\(") and s.endswith(r"\)"): s = s[2:-2].strip()
    if s.startswith(r"\[") and s.endswith(r"\]"): s = s[2:-2].strip()
    s = (s.replace(r"\left","").replace(r"\right","")
           .replace(r"\,","").replace(r"\;","").replace(r"\!","")
           .replace("−","-").replace("–","-").replace("—","-"))
    return s.strip()

def _normalize_str_eq(s: str) -> str:
    s = _strip_latex(s)
    s = (s.replace(r"\cdot","*").replace(r"\times","*")
           .replace(r"\div","/").replace(r"\pi","pi"))
    s = re.sub(r"\s+","",s)
    s = s.strip(" .;,:，。")
    return s

def _to_number(s: str) -> Optional[float]:
    try:
        if "/" in s and not re.search(r"[a-zA-Z]", s):
            a,b = s.split("/",1); return float(a)/float(b)
        return float(s)
    except Exception:
        return None

def answers_equal(pred: str, gold: str) -> bool:
    if pred is None or gold is None: return False
    p = _normalize_str_eq(pred); g = _normalize_str_eq(gold)
    if not p or not g: return False
    pn, gn = _to_number(p), _to_number(g)
    if pn is not None and gn is not None:
        return abs(pn - gn) <= 1e-9
    return p == g

# ---------------- I/O ----------------
def load_jsonl(path: str) -> List[dict]:
    data = []
    if not os.path.isfile(path):
        return data
    with open(path,"r",encoding="utf-8") as f:
        for ln, line in enumerate(f,1):
            s = line.strip()
            if not s: continue
            try:
                data.append(json.loads(s))
            except Exception as e:
                sys.stderr.write(f"[WARN] JSON parse fail {path}:{ln}: {e}\n")
    return data

def dump_jsonl(path: str, rows: List[dict]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def norm_space(s: str) -> str:
    return re.sub(r"\s+"," ", (s or "")).strip()

def get_id(obj: dict) -> str:
    sid = obj.get("id") or obj.get("example_id") or obj.get("uid")
    if sid: return str(sid)
    q = obj.get("query") or obj.get("original_question") or ""
    qn = norm_space(q)
    return f"h_{hashlib.md5(qn.encode('utf-8')).hexdigest()[:16]}"

RATIO_TAGS = [("020",0.2),("040",0.4),("060",0.6),("080",0.8),("100",1.0)]

def load_dir_as_index(d: str) -> Dict[float, Dict[str, dict]]:
    idx: Dict[float, Dict[str, dict]] = {}
    for tag, r in RATIO_TAGS:
        p = os.path.join(d, f"r{tag}.best.jsonl")
        arr = load_jsonl(p)
        idx[r] = { get_id(o): o for o in arr }
    return idx

def available_ratios_for_id(idx: Dict[float, Dict[str, dict]], sid: str) -> List[float]:
    return sorted([r for r in idx if sid in idx[r]])

# 计算某目录内，某 id 的“最小实际压缩比”
# 返回：(min_actual, supporting_r, pass_count)
def compute_actual_min_for_id(idx: Dict[float, Dict[str, dict]], sid: str) -> Tuple[Optional[float], Optional[float], int]:
    base = idx.get(1.0, {}).get(sid)
    if not base:
        return (None, None, 0)
    base_think = extract_think_inner(base.get("model_output",""))
    base_len = count_tokens(base_think)
    if base_len <= 0:
        return (None, None, 0)
    min_actual = None
    min_r = None
    pass_count = 0
    for r in [0.2,0.4,0.6,0.8,1.0]:
        entry = idx.get(r, {}).get(sid)
        if not entry:
            continue
        gold = entry.get("answer") or extract_gold_from_response(entry.get("response",""))
        pred = extract_last_boxed(entry.get("model_output",""))
        if not (gold and pred):
            continue
        if answers_equal(pred, gold):
            pass_count += 1
            think = extract_think_inner(entry.get("model_output",""))
            cur_len = max(1, count_tokens(think))
            actual = cur_len / max(1, base_len)
            if (min_actual is None) or (actual < min_actual):
                min_actual, min_r = actual, r
    return (min_actual, min_r, pass_count)

def merge_runs(dir_a: str, dir_b: str, out_dir: str, policy: str = "prefer-b") -> dict:
    idx_a = load_dir_as_index(dir_a)
    idx_b = load_dir_as_index(dir_b)
    ids_a = set(idx_a.get(1.0, {}).keys())
    ids_b = set(idx_b.get(1.0, {}).keys())
    only_a = ids_a - ids_b
    only_b = ids_b - ids_a
    both   = ids_a & ids_b

    chosen_dir_by_id: Dict[str, str] = {}

    # 1) 按策略决定交集去向
    if policy in ("prefer-a","prefer-b"):
        pref = "a" if policy=="prefer-a" else "b"
        for sid in only_a: chosen_dir_by_id[sid] = "a"
        for sid in only_b: chosen_dir_by_id[sid] = "b"
        for sid in both:   chosen_dir_by_id[sid] = pref

    elif policy == "coverage":
        # 谁的该 id 覆盖的 r 档更多选谁（平手选 B）
        for sid in only_a: chosen_dir_by_id[sid] = "a"
        for sid in only_b: chosen_dir_by_id[sid] = "b"
        for sid in both:
            cov_a = len(available_ratios_for_id(idx_a, sid))
            cov_b = len(available_ratios_for_id(idx_b, sid))
            chosen_dir_by_id[sid] = "a" if cov_a > cov_b else "b"

    elif policy == "best-actual":
        # 比较同一目录内该 id 的“最小实际压缩比”；相等再比通过次数/覆盖度；仍相等选 B
        for sid in only_a: chosen_dir_by_id[sid] = "a"
        for sid in only_b: chosen_dir_by_id[sid] = "b"
        for sid in both:
            a_min, a_r, a_pass = compute_actual_min_for_id(idx_a, sid)
            b_min, b_r, b_pass = compute_actual_min_for_id(idx_b, sid)
            if a_min is None and b_min is None:
                cov_a = len(available_ratios_for_id(idx_a, sid))
                cov_b = len(available_ratios_for_id(idx_b, sid))
                chosen_dir_by_id[sid] = "a" if cov_a > cov_b else "b"
            elif a_min is None:
                chosen_dir_by_id[sid] = "b"
            elif b_min is None:
                chosen_dir_by_id[sid] = "a"
            else:
                if a_min < b_min - 1e-9:
                    chosen_dir_by_id[sid] = "a"
                elif b_min < a_min - 1e-9:
                    chosen_dir_by_id[sid] = "b"
                else:
                    if a_pass != b_pass:
                        chosen_dir_by_id[sid] = "a" if a_pass > b_pass else "b"
                    else:
                        cov_a = len(available_ratios_for_id(idx_a, sid))
                        cov_b = len(available_ratios_for_id(idx_b, sid))
                        if cov_a != cov_b:
                            chosen_dir_by_id[sid] = "a" if cov_a > cov_b else "b"
                        else:
                            chosen_dir_by_id[sid] = "b"
    else:
        raise ValueError(f"Unknown policy: {policy}")

    # 2) 若选定的目录内该 id 缺 r100，则剔除该 id（保持分母一致性）
    for sid, dflag in list(chosen_dir_by_id.items()):
        if dflag=="a" and sid not in idx_a.get(1.0, {}):
            chosen_dir_by_id.pop(sid, None)
        elif dflag=="b" and sid not in idx_b.get(1.0, {}):
            chosen_dir_by_id.pop(sid, None)

    # 3) 写出各档合并后的 JSONL
    os.makedirs(out_dir, exist_ok=True)
    written_counts = {0.2:0, 0.4:0, 0.6:0, 0.8:0, 1.0:0}
    missing_by_ratio = defaultdict(int)

    for tag, r in RATIO_TAGS:
        rows = []
        for sid, dflag in chosen_dir_by_id.items():
            entry = (idx_a if dflag=="a" else idx_b).get(r, {}).get(sid)
            if entry is None:
                # 该 id 在被选中的目录里缺这个 r 档 → 直接跳过这一条
                missing_by_ratio[(sid, r)] += 1
                continue
            rows.append(entry)
        out_path = os.path.join(out_dir, f"r{tag}.best.jsonl")
        dump_jsonl(out_path, rows)
        written_counts[r] = len(rows)

    # 4) 输出摘要
    summary = {
        "tokenizer_used": TOKENIZER_NAME,
        "dir_a": os.path.abspath(dir_a),
        "dir_b": os.path.abspath(dir_b),
        "out_dir": os.path.abspath(out_dir),
        "policy": policy,
        "ids_a_r100": len(set(idx_a.get(1.0, {}).keys())),
        "ids_b_r100": len(set(idx_b.get(1.0, {}).keys())),
        "overlap_ids": len(set(idx_a.get(1.0, {}).keys()) & set(idx_b.get(1.0, {}).keys())),
        "only_a": len(set(idx_a.get(1.0, {}).keys()) - set(idx_b.get(1.0, {}).keys())),
        "only_b": len(set(idx_b.get(1.0, {}).keys()) - set(idx_a.get(1.0, {}).keys())),
        "chosen_total_ids": len(chosen_dir_by_id),
        "written_per_ratio": {str(k): v for k, v in written_counts.items()},
        "ratios_missing_entries_skipped": len(missing_by_ratio)  # 记录 (id, r) 层面的缺失条目数
    }
    with open(os.path.join(out_dir, "merge_log.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="目录 A（例如 50k 运行结果）")
    ap.add_argument("--b", required=True, help="目录 B（例如 60k 运行结果）")
    ap.add_argument("--out", required=True, help="输出目录（合并后）")
    ap.add_argument("--policy", default="prefer-b",
                    choices=["prefer-a","prefer-b","coverage","best-actual"],
                    help="重叠样本的选择策略")
    args = ap.parse_args()
    merge_runs(args.a, args.b, args.out, args.policy)

if __name__ == "__main__":
    main()
