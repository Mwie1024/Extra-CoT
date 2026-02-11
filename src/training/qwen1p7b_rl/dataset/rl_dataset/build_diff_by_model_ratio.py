#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import argparse, json, os, re, sys, random, hashlib
from collections import Counter
from typing import Dict, List, Tuple, Optional

# ---------------- tiktoken ----------------
try:
    import tiktoken  # type: ignore
    _ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(s: str) -> int:
        return len(_ENC.encode(s or ""))
    TOKENIZER = "tiktoken:cl100k_base"
except Exception:  # pragma: no cover
    def count_tokens(s: str) -> int:
        return max(1, len(s or "") // 4)
    TOKENIZER = "fallback:len//4"

# -------------- Regex --------------
THINK_SPAN = re.compile(r"<think>(.*?)</think>", flags=re.I | re.S)
GT_LAST    = re.compile(r"The\s+answer\s+is\s*:\s*(.+?)(?=$|\n)", flags=re.I)
BOXED_LAST = re.compile(r"\\boxed\s*\{([^}]*)\}.*$", flags=re.I | re.S)

def extract_think_inner(text: str) -> str:
    m = THINK_SPAN.search(text or "")
    return m.group(1) if m else ""

def extract_gold_from_response(s: str) -> Optional[str]:
    m_all = list(GT_LAST.finditer((s or "").strip()))
    return m_all[-1].group(1).strip() if m_all else None

def extract_last_boxed(s: str) -> Optional[str]:
    m = None
    for mm in BOXED_LAST.finditer(s or ""):
        m = mm
    return m.group(1).strip() if m else None

# ---------- 宽松等价 ----------
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

def _normalize_eq(s: str) -> str:
    s = _strip_latex(s)
    s = (s.replace(r"\cdot","*").replace(r"\times","*")
           .replace(r"\div","/").replace(r"\pi","pi"))
    s = re.sub(r"\s+","",s)
    return s.strip(" .;,:，。")

def _to_num(s: str) -> Optional[float]:
    try:
        if "/" in s and not re.search(r"[a-zA-Z]", s):
            a,b = s.split("/",1); return float(a)/float(b)
        return float(s)
    except Exception:
        return None

def answers_equal(pred: str, gold: str) -> bool:
    if pred is None or gold is None: return False
    p = _normalize_eq(pred); g = _normalize_eq(gold)
    if not p or not g: return False
    pn, gn = _to_num(p), _to_num(g)
    if pn is not None and gn is not None:
        return abs(pn - gn) <= 1e-9
    return p == g

# -------------- I/O --------------
def load_jsonl(path: str) -> List[dict]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s: continue
            try:
                out.append(json.loads(s))
            except Exception as e:
                sys.stderr.write(f"[WARN] JSON parse fail {path}:{ln}: {e}\n")
    return out

def get_id(obj: dict) -> str:
    sid = obj.get("id") or obj.get("example_id") or obj.get("uid")
    if sid: return str(sid)
    q = obj.get("query") or obj.get("original_question") or ""
    return "h_" + hashlib.md5(q.encode("utf-8")).hexdigest()[:16]

# ---------- parse actual windows ----------
def parse_windows(spec: str) -> Dict[str, Tuple[float,float]]:
    out: Dict[str, Tuple[float,float]] = {}
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg: continue
        k, r = seg.split(":")
        Ls, Rs = r.split("-")
        out[k.strip()] = (float(Ls), float(Rs))
    return out

# ---------- Strict target-first monotonic ----------
TARGETS = [0.2,0.4,0.6,0.8,1.0]
LABELS  = ["20","40","60","80","100"]
R2LBL   = {0.2:"20",0.4:"40",0.6:"60",0.8:"80",1.0:"100"}
LBL2R   = {"20":0.2,"40":0.4,"60":0.6,"80":0.8,"100":1.0}
NEXT    = {"20":"40","40":"60","60":"80","80":"100"}  # upward for adjacency

def choose_target_first_monotonic(pass_map: Dict[float,bool]) -> Tuple[Optional[float], bool]:
    passing = [r for r in TARGETS if pass_map.get(r, False)]
    if not passing:
        return (None, False)
    r0 = passing[0]
    non_mono = any((rr > r0 and not pass_map.get(rr, False)) for rr in TARGETS)
    if non_mono:
        return (None, True)
    return (r0, False)

def in_window(x: float, rng: Tuple[float,float]) -> bool:
    L, R = rng
    return (x >= L) and (x < R)

def mid_of(L: float, R: float) -> float:
    return 0.5*(L+R)

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="+", required=True,
                    help="1..N directories each containing rxxx.best.jsonl")
    ap.add_argument("--out_dir", required=True)
    # 目标±0.1（100 档上限 1.10）
    ap.add_argument("--actual_windows",
                    default="20:0.10-0.30,40:0.30-0.50,60:0.50-0.70,80:0.70-0.90,100:0.90-1.10",
                    help="per-target acceptable actual ratio windows")
    ap.add_argument("--quotas",
                    help="per-target sample counts (sum to total)",
                    default="20:800,40:800,60:800,80:800,100:800")
    ap.add_argument("--all_fail_k", type=int, default=500,
                    help="how many all-fail samples to export")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--shuffle_within_level", action="store_true",
                    help="randomize candidates within each level before closeness sort")
    args = ap.parse_args()

    random.seed(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)

    windows = parse_windows(args.actual_windows)  # label -> (L,R)
    for lbl in LABELS:
        if lbl not in windows:
            raise ValueError(f"missing window for level {lbl}")

    # quotas
    def parse_quotas(spec: str) -> Dict[str,int]:
        out: Dict[str,int] = {}
        for seg in spec.split(","):
            seg=seg.strip()
            if not seg: continue
            k,v = seg.split(":")
            out[k.strip()] = int(v)
        return out
    quotas = parse_quotas(args.quotas)
    for lbl in LABELS:
        quotas.setdefault(lbl, 0)
    total_target = sum(quotas.values())

    # ---- Read & merge; keep longest <think> per (id, ratio) ----
    pattern = {"020":0.2,"040":0.4,"060":0.6,"080":0.8,"100":1.0}
    data_by_ratio: Dict[float, Dict[str, dict]] = {r:{} for r in TARGETS}

    def maybe_update(store: Dict[str,dict], obj: dict):
        sid = get_id(obj)
        cur = store.get(sid)
        if not cur:
            store[sid] = obj
            return
        t_new = count_tokens(extract_think_inner(obj.get("model_output","")))
        t_old = count_tokens(extract_think_inner(cur.get("model_output","")))
        if t_new > t_old:
            store[sid] = obj

    for d in args.dirs:
        for tag, r in pattern.items():
            p = os.path.join(d, f"r{tag}.best.jsonl")
            if not os.path.isfile(p):
                sys.stderr.write(f"[WARN] missing {p}\n"); continue
            arr = load_jsonl(p)
            for obj in arr:
                maybe_update(data_by_ratio[r], obj)
            sys.stderr.write(f"[INFO] loaded {len(arr)} from {p}\n")

    base = data_by_ratio[1.0]
    if not base:
        print("[FATAL] r100 missing, cannot compute actual ratios", file=sys.stderr)
        sys.exit(2)

    # ---- Per-id evaluation ----
    stats_seen = 0
    raw_pass_counts           = Counter({lbl:0 for lbl in LABELS})
    mono_chosen_counts        = Counter({lbl:0 for lbl in LABELS})
    eligible_primary_counts   = Counter({lbl:0 for lbl in LABELS})
    eligible_adj1_counts      = Counter({lbl:0 for lbl in LABELS})
    eligible_adj2_counts      = Counter({lbl:0 for lbl in LABELS})

    candidates_by_level: Dict[str, List[dict]] = {lbl: [] for lbl in LABELS}  # own window
    adj1_by_level: Dict[str, List[dict]]       = {lbl: [] for lbl in LABELS}  # from prev level
    adj2_by_level: Dict[str, List[dict]]       = {lbl: [] for lbl in LABELS}  # from prev-prev

    all_fail_pool: List[dict] = []  # items that fail all ratios

    for sid, bobj in base.items():
        stats_seen += 1
        base_think = extract_think_inner(bobj.get("model_output",""))
        ref_full_len = count_tokens(base_think)      # ← 唯一分母：r=1.0 的 think 长度
        if ref_full_len <= 0:
            continue

        pass_map: Dict[float,bool] = {}
        len_map: Dict[float,int] = {}
        actual_map: Dict[float,float] = {}

        q_text = None
        gold_text = None
        any_pass = False

        for r in TARGETS:
            e = data_by_ratio.get(r, {}).get(sid)
            if not e:
                pass_map[r] = False
                continue
            if q_text is None:
                q_text = e.get("query") or e.get("original_question") or ""
            gold = e.get("answer") or extract_gold_from_response(e.get("response",""))
            pred = extract_last_boxed(e.get("model_output",""))
            if gold_text is None and gold:
                gold_text = gold
            ok = bool(gold) and bool(pred) and answers_equal(pred, gold)
            pass_map[r] = ok
            if ok:
                any_pass = True
                cur_len = max(1, count_tokens(extract_think_inner(e.get("model_output",""))))
                len_map[r] = cur_len
                actual_map[r] = cur_len / float(ref_full_len)    # 以 r100 为分母
                raw_pass_counts[R2LBL[r]] += 1

        if not any_pass:
            all_fail_pool.append({
                "id": sid,
                "question": q_text or "",
                "gold": gold_text or "",
                "gt_ratio": 1.0,
                "ref_full_len": int(ref_full_len)
            })
            continue  # not eligible for main dataset

        chosen_r, non_mono = choose_target_first_monotonic(pass_map)
        if chosen_r is None or non_mono:
            continue

        lbl = R2LBL[chosen_r]
        mono_chosen_counts[lbl] += 1

        act = actual_map.get(chosen_r)
        if act is None:
            if not actual_map:
                continue
            chosen_r, act = min(actual_map.items(), key=lambda kv: kv[1])
            lbl = R2LBL[chosen_r]

        def make_row(dataset_level: str, source: str) -> dict:
            return {
                "id": sid,
                "question": q_text or "",
                "gold": gold_text or "",
                "gt_ratio": float(LBL2R[dataset_level]),    # 五档之一
                "target_ratio": chosen_r,
                "target_label": lbl,                        # donor
                "dataset_level": dataset_level,             # receiver
                "actual_ratio": round(act, 6),
                "ref_full_len": int(ref_full_len),
                "comp100_think_tokens": int(ref_full_len),  # 同步给出 r100 的 think
                "current_length": len_map.get(chosen_r, max(1, int(round(act * ref_full_len)))),
                "monotonic": True,
                "actual_window": list(windows[dataset_level]),
                "source": source
            }

        # --- primary (own window) ---
        if in_window(act, windows[lbl]):
            eligible_primary_counts[lbl] += 1
            candidates_by_level[lbl].append(make_row(lbl, "primary"))

        # --- adjacent up-fill (one hop) ---
        nxt1 = NEXT.get(lbl)
        if nxt1 and in_window(act, windows[nxt1]):
            eligible_adj1_counts[nxt1] += 1
            adj1_by_level[nxt1].append(make_row(nxt1, f"adjacent1_from_{lbl}"))

        # --- adjacent up-fill (two hops) ---
        nxt2 = NEXT.get(nxt1) if nxt1 else None
        if nxt2 and in_window(act, windows[nxt2]):
            eligible_adj2_counts[nxt2] += 1
            adj2_by_level[nxt2].append(make_row(nxt2, f"adjacent2_from_{lbl}"))

    # ---- sorting by closeness to receiver window midpoint ----
    for lbl, items in candidates_by_level.items():
        if not items: continue
        if args.shuffle_within_level: random.shuffle(items)
        L, R = windows[lbl]; mid = mid_of(L,R)
        items.sort(key=lambda x: abs(x["actual_ratio"] - mid))

    for lbl, items in adj1_by_level.items():
        if not items: continue
        if args.shuffle_within_level: random.shuffle(items)
        L, R = windows[lbl]; mid = mid_of(L,R)
        items.sort(key=lambda x: abs(x["actual_ratio"] - mid))

    for lbl, items in adj2_by_level.items():
        if not items: continue
        if args.shuffle_within_level: random.shuffle(items)
        L, R = windows[lbl]; mid = mid_of(L,R)
        items.sort(key=lambda x: abs(x["actual_ratio"] - mid))

    # ---- sampling: primary -> adj1 -> adj2 (donors only from lower targets) ----
    used = set()
    sampled: List[dict] = []
    per_level_used = {lbl: {"primary":0, "adjacent1":0, "adjacent2":0} for lbl in LABELS}

    # primary first
    for lbl in LABELS:
        need = quotas[lbl]
        if need <= 0: continue
        items = candidates_by_level.get(lbl, [])
        for row in items:
            if per_level_used[lbl]["primary"] >= need: break
            if row["id"] in used: continue
            sampled.append(row); used.add(row["id"])
            per_level_used[lbl]["primary"] += 1

    # adjacent 1-hop
    for lbl in ["40","60","80","100"]:
        need = quotas[lbl] - (per_level_used[lbl]["primary"] + per_level_used[lbl]["adjacent1"] + per_level_used[lbl]["adjacent2"])
        if need <= 0: continue
        donors = adj1_by_level.get(lbl, [])
        for row in donors:
            if (per_level_used[lbl]["primary"] + per_level_used[lbl]["adjacent1"] + per_level_used[lbl]["adjacent2"]) >= quotas[lbl]: break
            if row["id"] in used: continue
            sampled.append(row); used.add(row["id"])
            per_level_used[lbl]["adjacent1"] += 1

    # adjacent 2-hop
    for lbl in ["60","80","100"]:
        need = quotas[lbl] - (per_level_used[lbl]["primary"] + per_level_used[lbl]["adjacent1"] + per_level_used[lbl]["adjacent2"])
        if need <= 0: continue
        donors = adj2_by_level.get(lbl, [])
        for row in donors:
            if (per_level_used[lbl]["primary"] + per_level_used[lbl]["adjacent1"] + per_level_used[lbl]["adjacent2"]) >= quotas[lbl]: break
            if row["id"] in used: continue
            sampled.append(row); used.add(row["id"])
            per_level_used[lbl]["adjacent2"] += 1

    # ---- write outputs ----
    out_path = os.path.join(args.out_dir, "rl25k.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for r in sampled:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- all-fail ----
    random.shuffle(all_fail_pool)
    allfail_take = min(args.all_fail_k, len(all_fail_pool))
    allfail_sampled = all_fail_pool[:allfail_take]
    out_allfail = os.path.join(args.out_dir, "allfail500.jsonl")
    with open(out_allfail, "w", encoding="utf-8") as f:
        for r in allfail_sampled:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- meta ----
    supply_primary  = {lbl: len(candidates_by_level[lbl]) for lbl in LABELS}
    supply_adj1     = {lbl: len(adj1_by_level[lbl])       for lbl in LABELS}
    supply_adj2     = {lbl: len(adj2_by_level[lbl])       for lbl in LABELS}

    meta = {
        "tokenizer": TOKENIZER,
        "dirs": args.dirs,
        "actual_windows": {lbl: list(windows[lbl]) for lbl in LABELS},
        "quotas_request": quotas,
        "per_level_used": per_level_used,
        "counts": {
            "raw_pass": dict(raw_pass_counts),
            "mono_chosen": dict(mono_chosen_counts),
            "eligible_primary": dict(eligible_primary_counts),
            "eligible_adjacent1": dict(eligible_adj1_counts),
            "eligible_adjacent2": dict(eligible_adj2_counts),
            "supply_primary": supply_primary,
            "supply_adjacent1": supply_adj1,
            "supply_adjacent2": supply_adj2,
            "all_fail_total": len(all_fail_pool),
            "all_fail_sampled": allfail_take
        },
        "sizes": {
            "total_target": total_target,
            "sampled": len(sampled),
            "unique_ids": len(used),
            "seen_problems": stats_seen
        },
        "notes": {
            "policy": "target-first strict monotonic; primary by own window; allow downward up-fill up to 2 levels (only if actual in receiver window); all-fail gt_ratio=1.0; actual_ratio uses r100 think length (ref_full_len) as denominator."
        }
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # ---- console summary ----
    print(f"[OK] sampled {len(sampled)} (target={total_target}) -> {out_path}")
    print(f"[OK] all-fail {allfail_take}/{len(all_fail_pool)} -> {out_allfail}")
    print("[STATS] per target level (20/40/60/80/100):")
    for lbl in LABELS:
        rp   = raw_pass_counts[lbl]
        mc   = mono_chosen_counts[lbl]
        epr  = eligible_primary_counts[lbl]
        eaj1 = eligible_adj1_counts[lbl]
        eaj2 = eligible_adj2_counts[lbl]
        supP = supply_primary[lbl]
        sup1 = supply_adj1[lbl]
        sup2 = supply_adj2[lbl]
        useP = per_level_used[lbl]["primary"]
        use1 = per_level_used[lbl]["adjacent1"]
        use2 = per_level_used[lbl]["adjacent2"]
        tgt  = quotas[lbl]
        print(f"  [{lbl}] raw_pass={rp} mono_chosen={mc} eligible_primary={epr} "
              f"eligible_adj1={eaj1} eligible_adj2={eaj2} | "
              f"supply primary={supP} adj1={sup1} adj2={sup2} | "
              f"target={tgt} used_primary={useP} used_adj1={use1} used_adj2={use2}")

    unmet = sum(max(0, quotas[lbl] - (per_level_used[lbl]['primary'] + per_level_used[lbl]['adjacent1'] + per_level_used[lbl]['adjacent2'])) for lbl in LABELS)
    if unmet > 0:
        print(f"[WARN] unmet quotas remain: {unmet} (insufficient primary+adjacent supply)")

if __name__ == "__main__":
    main()
