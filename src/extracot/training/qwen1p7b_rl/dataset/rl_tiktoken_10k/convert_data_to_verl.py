#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Convert your minimal RL jsonl to VERL-ready AUTO data for Qwen,
with ratio-aware VALIDATION sampling.

Input jsonl fields per line:
  id: str
  question: str
  gold: str
  chosen_ratio: float  (optional if bin_label present)
  bin_label: "20"|"40"|"60"|"80"|"100"  (optional if chosen_ratio present)
  comp100_think_tokens: int

Outputs:
  out_dir/train.jsonl, test.jsonl
  out_dir/train.parquet, test.parquet (if 'datasets' installed)
  out_dir/meta.json

Key additions vs previous version:
  --val-per-ratio "20:8000,40:9000,60:8000,80:3900,100:2800"
    -> turned into shares for validation allocation.

Additional guarantee:
  The final TRAIN and TEST outputs (both JSONL and Parquet) are ALWAYS shuffled
  just before writing, deterministically controlled by --seed.
"""

import argparse, json, os, random, re
from typing import Any, Dict, List, Optional, Tuple

# -------- optional parquet --------
try:
    import datasets
    _HAS_DATASETS = True
except Exception:
    _HAS_DATASETS = False

SYSTEM_TEXT = "You are a helpful assistant."
INSTR = "Please reason step by step, and put your final answer within \\boxed{}.\n"
AUTO_TOKEN = "<COMP_AUTO>"

DEFAULT_BUCKETS = [1.0, 0.8, 0.6, 0.4, 0.2]  # presented DESC; we also keep ASC copy for RM
def _sorted_asc(xs): return sorted(xs)

def ratio_to_token(r: float) -> str:
    val = int(round(float(r) * 100))
    val = max(0, min(100, val))
    return f"<COMP_{val}>"

def build_ratio_token_map(buckets: List[float]) -> Dict[str, str]:
    # keys like "0.2","0.4",..., "1.0"
    m = {}
    for r in buckets:
        key = f"{float(r):.1f}".rstrip("0").rstrip(".")
        m[key] = ratio_to_token(r)
    return m

def messages_for_qwen(system_text: str, user_text: str) -> List[Dict[str,str]]:
    return [
        {"role": "system", "content": system_text},
        {"role": "user",   "content": user_text},
    ]

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s: continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    data.append(obj)
            except Exception as e:
                print(f"[WARN] JSON parse error at line {ln}: {e}")
    return data

def save_jsonl(path: str, rows: List[Dict[str,Any]]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def norm_question(q: str) -> str:
    s = (q or "")
    s = s.replace("\u00A0"," ").replace("\t"," ").replace("\r"," ")
    s = re.sub(r"\\[a-zA-Z]+|[{}$^_]|\\", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()

def _as_ratio(val: str) -> Optional[float]:
    """Accept '20' or '0.2' → 0.2"""
    try:
        x = float(val)
        if x > 1.0: x = x/100.0
        return max(0.0, min(1.0, x))
    except Exception:
        return None

def get_ratio(obj: Dict[str,Any]) -> Optional[float]:
    # Prefer chosen_ratio; fallback to bin_label
    # cr = obj.get("chosen_ratio", None)
    # if cr is not None:
    #     try:
    #         r = float(cr)
    #         return max(0.0, min(1.0, r))
    #     except Exception:
    #         pass
    bl = obj.get("bin_label", None)
    if bl is not None:
        rr = _as_ratio(str(bl))
        return rr
    return None

def to_item(rec: Dict[str,Any],
           buckets_desc: List[float],
           comp_tol: float,
           data_source_name: str) -> Optional[Dict[str,Any]]:

    q = (rec.get("question") or "").strip()
    if not q:
        return None
    gold = rec.get("gold", None)
    if gold is None or str(gold).strip() == "":
        return None

    # reference full COT length (COMP_100 tokens)
    ref_len = rec.get("comp100_think_tokens", None)
    try:
        ref_len = int(ref_len) if ref_len is not None else None
    except Exception:
        ref_len = None

    # ratio
    r = get_ratio(rec)
    if r is None:
        return None
    r = max(0.0, min(1.0, float(r)))
    chosen_token = ratio_to_token(r)

    # prompt: instruction + question + <COMP_AUTO>
    # user_text = f"{INSTR}{q} {AUTO_TOKEN}".strip()
    user_text = f"{INSTR}{q}".strip()

    # allowed ratios ASC for RM内部比较；展示时常用 DESC
    buckets_asc = _sorted_asc(list(set(buckets_desc)))
    ratio_token_map = build_ratio_token_map(buckets_asc)
    allowed_comp_tokens = [ratio_to_token(x) for x in buckets_asc]

    item = {
        "data_source": data_source_name,
        "ability": "math",
        "prompt": messages_for_qwen(SYSTEM_TEXT, user_text),
        "reward_model": {"style": "rule", "ground_truth": str(gold).strip()},
        "extra_info": {
            # AUTO config for RM
            "auto_token": AUTO_TOKEN,
            # "allowed_ratios": buckets_asc,                 # e.g. [0.2,0.4,0.6,0.8,1.0]
            # "allowed_comp_tokens": allowed_comp_tokens,    # ["<COMP_20>",...]
            # "ratio_token_map": ratio_token_map,

            "ratio_bucket": float(r),                      # our chosen bucket as float
            "gt_ratio": float(r),                          # RM 对齐目标档
            "comp_tolerance": float(comp_tol),

            # length reference
            "ref_full_len": int(ref_len) if (isinstance(ref_len, int) and ref_len > 0) else None,
            "orig_cot_tokens": int(ref_len) if (isinstance(ref_len, int) and ref_len > 0) else None,

            # trace
            "id": rec.get("id"),
            "chosen_comp_token": chosen_token,             # <COMP_xx>
        },
        "question": q,
    }
    return item

# -------- grouping / parsing specs --------

def group_by_ratio(lst: List[Dict[str,Any]]) -> Dict[float, List[Dict[str,Any]]]:
    g: Dict[float, List[Dict[str,Any]]] = {}
    for it in lst:
        r = it.get("extra_info",{}).get("ratio_bucket", None)
        if r is None: continue
        g.setdefault(float(r), []).append(it)
    return g

def parse_share(spec: Optional[str]) -> Dict[float,float]:
    out: Dict[float,float] = {}
    if not spec: return out
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg or ":" not in seg: continue
        k, v = seg.split(":",1)
        rk = _as_ratio(k.strip())
        if rk is None: continue
        try:
            out[rk] = float(v)
        except Exception:
            pass
    return out

def parse_caps(spec: Optional[str]) -> Dict[float,int]:
    out: Dict[float,int] = {}
    if not spec: return out
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg or ":" not in seg: continue
        k, v = seg.split(":",1)
        rk = _as_ratio(k.strip())
        if rk is None: continue
        try:
            out[rk] = int(v)
        except Exception:
            pass
    return out

def parse_counts(spec: Optional[str]) -> Dict[float,int]:
    """
    For --val-per-ratio; supports '20:8000,40:9000,...' or '0.2:8000,...'
    """
    out: Dict[float,int] = {}
    if not spec: return out
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg or ":" not in seg: continue
        k, v = seg.split(":",1)
        rk = _as_ratio(k.strip())
        if rk is None: continue
        try:
            out[rk] = int(v)
        except Exception:
            pass
    return out

def to_shares_from_counts(counts: Dict[float,int]) -> Dict[float,float]:
    s = float(sum(max(0, c) for c in counts.values()))
    if s <= 0:
        return {}
    return {r: (counts[r]/s) for r in counts}

def allocate(grouped: Dict[float, List[Dict[str,Any]]], total: int,
             share: Dict[float,float], caps: Optional[Dict[float,int]] = None) -> Dict[float,int]:
    """
    Generic allocator with:
      - base on shares (if given) else proportional to availability
      - apply per-ratio caps (optional)
      - greedy redistribute remainder by remaining capacity
    """
    caps = caps or {}
    if total is None:
        return {r: len(lst) for r, lst in grouped.items()}

    alloc: Dict[float,int] = {}
    remain = total

    if share:
        for r, lst in grouped.items():
            want = int(round(total * float(share.get(r, 0.0))))
            pick = min(len(lst), want)
            alloc[r] = pick
            remain -= pick
    else:
        # proportional to availability
        total_avail = sum(len(v) for v in grouped.values())
        if total_avail == 0:
            return {r: 0 for r in grouped}
        for r, lst in grouped.items():
            want = int(round(total * (len(lst) / total_avail)))
            pick = min(len(lst), want)
            alloc[r] = pick
            remain -= pick

    # apply caps
    for r in list(alloc.keys()):
        cap = caps.get(r, None)
        if cap is not None and alloc[r] > cap:
            diff = alloc[r] - cap
            alloc[r] = cap
            remain += diff

    # greedy fill remainder by room
    if remain > 0:
        order = sorted(grouped.keys(), key=lambda rr: (len(grouped[rr]) - alloc.get(rr,0)), reverse=True)
        for rr in order:
            if remain <= 0: break
            room = len(grouped[rr]) - alloc.get(rr,0)
            if room <= 0: continue
            add = min(room, remain)
            alloc[rr] = alloc.get(rr,0) + add
            remain -= add

    # If still negative (overshoot by rounding), trim greedily
    if remain < 0:
        order = sorted(grouped.keys(), key=lambda rr: alloc.get(rr,0), reverse=True)
        for rr in order:
            if remain >= 0: break
            take = min(alloc.get(rr,0), -remain)
            if take > 0:
                alloc[rr] -= take
                remain += take

    return alloc

def hist_by_ratio(lst: List[Dict[str,Any]]):
    h: Dict[str,int] = {}
    for it in lst:
        rr = it.get("extra_info",{}).get("ratio_bucket", None)
        if rr is None: continue
        h[str(rr)] = h.get(str(rr), 0) + 1
    return h

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="path/to/minimal.jsonl")
    ap.add_argument("--out-dir", required=True, help="Directory to write VERL-ready data.")
    ap.add_argument("--val-ratio", type=float, default=0.1, help="Validation split ratio (of kept items).")
    ap.add_argument("--val-per-ratio", default="20:120,40:300,60:300,80:160,100:120",
                    help='Validation target distribution, e.g. "20:8000,40:9000,60:8000,80:3900,100:2800". Converted to shares.')
    ap.add_argument("--shuffle", action="store_true", help="Shuffle before split.")
    ap.add_argument("--seed", type=int, default=42)

    # buckets/ratios
    ap.add_argument("--buckets", default="1.0,0.8,0.6,0.4,0.2",
                    help="Allowed ratios (comma), can be 1.0 or 100 for 100% etc.")
    ap.add_argument("--comp-tolerance", type=float, default=0.1,
                    help="Band for calibration reward (|r_hat - chosen|<=band gives non-negative reward).")
    ap.add_argument("--data-source-name", default="comp_auto_rl_minimal")

    # optional balancing (train set only)
    ap.add_argument("--target-size", type=int, default=None,
                    help="Sample a TRAIN set of this size from the pool (after val split).")
    ap.add_argument("--per-ratio-share", default=None,
                    help='e.g. "0.2:0.26,0.4:0.36,0.6:0.2,0.8:0.12,1.0:0.06" used with --target-size.')
    ap.add_argument("--cap-per-ratio", default=None,
                    help='e.g. "0.2:6500,0.4:9000,0.6:5000,0.8:3000,1.0:1500" hard caps per bucket.')

    # optional dedup
    ap.add_argument("--dedup-by-question", action="store_true", help="Dedup by normalized question text.")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)

    # parse buckets -> floats (we keep user order but also use ASC internally)
    buckets_desc: List[float] = []
    for t in args.buckets.split(","):
        t = t.strip()
        if not t: continue
        try:
            v = float(t)
            if v > 1.0: v = v / 100.0
            buckets_desc.append(max(0.0, min(1.0, v)))
        except Exception:
            pass
    if not buckets_desc:
        buckets_desc = DEFAULT_BUCKETS[:]

    # load raw
    raw = load_jsonl(args.input)
    base_n = len(raw)
    print(f"[INFO] loaded {base_n} minimal items from {args.input}")

    # map to VERL items
    items: List[Dict[str,Any]] = []
    drop_stats = {"no_q":0, "no_gold":0, "no_ratio":0}
    for rec in raw:
        it = to_item(rec, buckets_desc=buckets_desc, comp_tol=args.comp_tolerance, data_source_name=args.data_source_name)
        if it is None:
            q = rec.get("question"); g = rec.get("gold"); r = get_ratio(rec)
            if not (q and str(q).strip()): drop_stats["no_q"] += 1
            elif g is None or str(g).strip() == "": drop_stats["no_gold"] += 1
            elif r is None: drop_stats["no_ratio"] += 1
            continue
        items.append(it)

    if args.shuffle:
        rng.shuffle(items)

    # (optional) dedup by question
    if args.dedup_by_question:
        seen = set()
        uniq = []
        for it in items:
            key = norm_question(it.get("question",""))
            if key in seen: continue
            seen.add(key)
            uniq.append(it)
        print(f"[INFO] dedup_by_question: {len(items)} -> {len(uniq)}")
        items = uniq

    # ---- Ratio-aware VALIDATION allocation ----
    n_total = len(items)
    n_val   = max(0, int(round(n_total * args.val_ratio)))

    # groups by ratio over ALL kept items
    all_groups = group_by_ratio(items)

    # shares for VAL: from --val-per-ratio (counts -> shares), else from current items distribution
    val_share: Dict[float,float] = {}
    if args.val_per_ratio:
        counts = parse_counts(args.val_per_ratio)
        if counts:
            val_share = to_shares_from_counts(counts)
            # 只保留出现在数据里的桶（避免目标里有空桶）
            if val_share:
                present = set(all_groups.keys())
                val_share = {r: s for r, s in val_share.items() if r in present}
    if not val_share:
        # fall back: use observed distribution
        total_avail = float(sum(len(v) for v in all_groups.values()))
        if total_avail > 0:
            val_share = {r: (len(lst) / total_avail) for r, lst in all_groups.items()}

    # 先计算验证集每桶数量（考虑不足回填）
    val_alloc = allocate(all_groups, total=n_val, share=val_share, caps=None)

    # 实际抽样（不放回），其余作为训练池
    train_pool: List[Dict[str,Any]] = []
    val_list:   List[Dict[str,Any]] = []
    for r, lst in all_groups.items():
        tmp = lst[:]
        rng.shuffle(tmp)
        k = min(len(tmp), val_alloc.get(r, 0))
        val_list.extend(tmp[:k])
        train_pool.extend(tmp[k:])

    print(f"[INFO] split: TRAIN={len(train_pool)}  VAL={len(val_list)}  (val_ratio={args.val_ratio})")
    print(f"[INFO] val_alloc (requested shares → actual counts) = { {str(r): val_alloc.get(r,0) for r in sorted(all_groups.keys())} }")

    # ---- TRAIN allocation (optional sizing) ----
    train_groups = group_by_ratio(train_pool)
    share_tr = parse_share(args.per_ratio_share)
    caps_tr  = parse_caps(args.cap_per_ratio)
    train_alloc = allocate(train_groups, total=args.target_size, share=share_tr, caps=caps_tr)

    train_final: List[Dict[str,Any]] = []
    for r, lst in train_groups.items():
        tmp = lst[:]
        rng.shuffle(tmp)
        k = train_alloc.get(r, len(tmp)) if args.target_size is not None or caps_tr else len(tmp)
        train_final.extend(tmp[:k])

    # ---- Final shuffle for output order (ALWAYS) ----
    # Ensure both JSONL and Parquet have randomly ordered samples (deterministic w.r.t --seed)
    rng.shuffle(train_final)
    rng.shuffle(val_list)

    # write jsonl
    tr_path = os.path.join(args.out_dir, "train.jsonl")
    te_path = os.path.join(args.out_dir, "test.jsonl")
    save_jsonl(tr_path, train_final)
    save_jsonl(te_path, val_list)
    print(f"[OK] wrote {len(train_final)} train -> {tr_path}")
    print(f"[OK] wrote {len(val_list)}   val  -> {te_path}")

    # parquet
    if _HAS_DATASETS:
        # lists are already shuffled above; Dataset preserves row order
        ds_tr = datasets.Dataset.from_list(train_final)
        ds_te = datasets.Dataset.from_list(val_list)
        p_tr = os.path.join(args.out_dir, "train.parquet")
        p_te = os.path.join(args.out_dir, "test.parquet")
        ds_tr.to_parquet(p_tr)
        ds_te.to_parquet(p_te)
        print(f"[OK] parquet train: {p_tr} ({len(ds_tr)})")
        print(f"[OK] parquet test : {p_te} ({len(ds_te)})")
    else:
        print("[WARN] 'datasets' not installed; parquet not written (jsonl already saved).")

    # meta
    meta = {
        "input": args.input,
        "total_loaded": base_n,
        "kept_total": len(items),
        "drop_stats": drop_stats,

        "train": len(train_final),
        "val": len(val_list),

        "train_hist_by_ratio": hist_by_ratio(train_final),
        "val_hist_by_ratio": hist_by_ratio(val_list),

        "buckets_used": _sorted_asc(list(set(buckets_desc))),
        "auto_token": AUTO_TOKEN,
        "comp_tolerance": args.comp_tolerance,

        "val_alloc_per_ratio": {str(k): int(v) for k, v in val_alloc.items()},
        "val_share_from": (args.val_per_ratio or "observed_distribution"),

        "balancing_train": {
            "target_size": args.target_size,
            "per_ratio_share": args.per_ratio_share,
            "cap_per_ratio": args.cap_per_ratio,
            "available_train_per_ratio": {str(k): len(v) for k, v in train_groups.items()},
            "alloc_per_ratio": {str(k): train_alloc.get(k,0) for k in train_groups},
        }
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[OK] meta -> {os.path.join(args.out_dir,'meta.json')}")
    print(f"[INFO] Train hist: {meta['train_hist_by_ratio']}")
    print(f"[INFO]  Val  hist: {meta['val_hist_by_ratio']}")

if __name__ == "__main__":
    main()
