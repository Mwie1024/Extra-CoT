#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, re, sys, random, math, hashlib
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Optional

# ---------------- tiktoken ----------------
try:
    import tiktoken  # type: ignore
    _ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(s: str) -> int:
        return len(_ENC.encode(s or ""))
    TOKENIZER = "tiktoken:cl100k_base"
except Exception:
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

# ---------- 解析 bins ----------
def parse_bins(spec: str) -> List[Tuple[float,float,float,str]]:
    """
    spec like: "20:0.10-0.30,40:0.30-0.50,60:0.5-0.7,80:0.7-0.9,100:0.90-9.99"
    return list of (L, R, label_ratio, label_str)
    """
    bins = []
    for seg in spec.split(","):
        seg = seg.strip()
        if not seg: continue
        k, r = seg.split(":")
        Ls, Rs = r.split("-")
        label_ratio = 1.0 if k.strip()=="100" else float(int(k)/100.0)
        bins.append((float(Ls), float(Rs), label_ratio, k.strip()))
    bins.sort(key=lambda x: x[0])
    return bins

def bin_of(actual: float, bins: List[Tuple[float,float,float,str]]) -> Optional[Tuple[float,str,Tuple[float,float]]]:
    for L,R,lr,ks in bins:
        if actual >= L and actual < R:
            return (lr, ks, (L,R))
    return None

# ---------- 单调决策（目标优先） ----------
TARGETS = [0.2,0.4,0.6,0.8,1.0]
def choose_target_first_monotonic(pass_map: Dict[float,bool]) -> Tuple[Optional[float], bool]:
    passing = [r for r in TARGETS if pass_map.get(r, False)]
    if not passing:
        return (None, False)
    r0 = passing[0]
    non_mono = any((rr > r0 and not pass_map.get(rr, False)) for rr in TARGETS)
    if non_mono:
        return (None, True)  # 严格模式下不接受
    return (r0, False)

# ---------- 主逻辑 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", nargs="+", required=True, help="1..N directories each containing rxxx.best.jsonl")
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--bins", default="20:0.10-0.30,40:0.30-0.50,60:0.5-0.7,80:0.7-0.9,100:0.90-1.10")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--quotas", default="20:800,40:800,60:800,80:800,100:800",
                    help="per-bin target counts summing to total (default 25k)")
    ap.add_argument("--allow_non_mono_bins", default="80,100",
                    help="comma-list of bin labels that may use non-monotonic fillers if shortage (e.g., '100' or '80,100')")
    ap.add_argument("--shuffle_within_bin", action="store_true", help="randomize candidates in a bin before closeness sort")
    args = ap.parse_args()

    random.seed(args.seed)
    os.makedirs(args.out_dir, exist_ok=True)
    bins = parse_bins(args.bins)
    bin_labels = [b[3] for b in bins]  # ["20","40",...,"100"]

    # quotas
    def parse_quotas(spec: str) -> Dict[str,int]:
        out = {}
        for seg in spec.split(","):
            seg=seg.strip()
            if not seg: continue
            k,v = seg.split(":")
            out[k.strip()] = int(v)
        return out
    quotas = parse_quotas(args.quotas)
    total_target = sum(quotas.get(k,0) for k in bin_labels)
    allow_non_mono = set([s.strip() for s in args.allow_non_mono_bins.split(",") if s.strip()])

    # ---- 读入与并集索引（允许多目录；对每个 ratio 以 id 去重，默认保留 think 更长的）----
    pattern = {"020":0.2,"040":0.4,"060":0.6,"080":0.8,"100":1.0}
    data_by_ratio: Dict[float, Dict[str, dict]] = {r:{} for r in TARGETS}
    def maybe_update(store: Dict[str,dict], obj: dict):
        sid = get_id(obj)
        cur = store.get(sid)
        if not cur:
            store[sid] = obj
            return
        # 选择 <think> token 更长者
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
        print("[FATAL] r100 缺失，无法计算实际压缩比", file=sys.stderr); sys.exit(2)

    # ---- 为每个 id 计算 pass_map / actual_by_target / 选择难度 / 分桶 ----
    kept = []
    stats_seen = 0
    per_bin_supply = {k: {"mono":0, "non_mono":0} for k in bin_labels}

    for sid, bobj in base.items():
        stats_seen += 1

        base_think = extract_think_inner(bobj.get("model_output",""))
        base_len = count_tokens(base_think)
        if base_len <= 0:
            continue

        comp100_think_tokens = base_len

        pass_map: Dict[float,bool] = {}
        actual_map: Dict[float,float] = {}

        q_text = None
        gold_text = None
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
                cur_len = max(1, count_tokens(extract_think_inner(e.get("model_output",""))))
                actual_map[r] = cur_len / max(1, base_len)

        # 没有任何通过就跳过
        if not any(pass_map.get(r, False) for r in TARGETS):
            continue

        # 目标优先 + 严格单调
        chosen_r, non_mono = choose_target_first_monotonic(pass_map)
        if chosen_r is None:
            # 标记为 non-mono 候选；是否后面用来填补由 allow_non_mono 控制
            # 仍然需要给它找一个“支持它的 r”以及 actual/bin（取 actual 最小者）
            if not actual_map:
                continue
            rr_min, act_min = min(actual_map.items(), key=lambda kv: kv[1])
            binfo = bin_of(act_min, bins)
            if not binfo:
                continue
            lr, lbl, (L,R) = binfo
            per_bin_supply[lbl]["non_mono"] += 1
            kept.append({
                "id": sid,
                "question": q_text or "",
                "gold": gold_text or "",
                "chosen_ratio": rr_min,                   # 产生最小 actual 的目标档
                "comp100_think_tokens": comp100_think_tokens,
                "current_length": cur_len,
                "monotonic": False,
                "actual_ratio": round(act_min, 6),
                "bin_label": lbl, "bin_range": [L,R],
                "source": "non_mono"
            })
            continue

        # 有严格单调解：用 chosen_r 的 actual 做分桶
        act = actual_map.get(chosen_r, None)
        if act is None:
            # 理论上不太会发生，但保守处理：取最小 actual 的那个 r
            if not actual_map: continue
            chosen_r, act = min(actual_map.items(), key=lambda kv: kv[1])

        binfo = bin_of(act, bins)
        if not binfo:
            continue
        lr, lbl, (L,R) = binfo
        per_bin_supply[lbl]["mono"] += 1
        kept.append({
            "id": sid,
            "question": q_text or "",
            "gold": gold_text or "",
            "chosen_ratio": chosen_r,
            "comp100_think_tokens": comp100_think_tokens,
            "current_length": cur_len,
            "monotonic": True,
            "actual_ratio": act,
            "bin_label": lbl, "bin_range": [L,R],
            "source": "mono"
        })

    # ---- 按 bin 聚合，区内排序：默认按“距离区间中点的接近度”从近到远（越靠近越稳）----
    by_bin = defaultdict(list)
    for row in kept:
        by_bin[row["bin_label"]].append(row)

    def mid_of(L,R): return 0.5*(L+R)
    for lbl, items in by_bin.items():
        if args.shuffle_within_bin:
            random.shuffle(items)
        L,R = items[0]["bin_range"]
        mid = mid_of(L,R)
        items.sort(key=lambda x: (not x["monotonic"], abs(x["actual_ratio"]-mid)))

    # ---- 计算可用供给与目标配额，做一次“缺口回填”的再平衡（不跨桶，只调整每桶目标数）----
    supply_mono = {lbl: sum(1 for x in by_bin[lbl] if x["monotonic"]) for lbl in bin_labels}
    supply_non  = {lbl: sum(1 for x in by_bin[lbl] if not x["monotonic"]) for lbl in bin_labels}
    supply_all  = {lbl: supply_mono[lbl] + supply_non[lbl] for lbl in bin_labels}

    # 初始目标
    target = {lbl: quotas.get(lbl, 0) for lbl in bin_labels}

    # 第一轮：用 mono 填到 min(target, mono_supply)
    take_mono = {lbl: min(target[lbl], supply_mono[lbl]) for lbl in bin_labels}
    # mono 不足的缺口
    deficits = {lbl: target[lbl] - take_mono[lbl] for lbl in bin_labels}

    # 第二轮：允许的 bin 用 non-mono 补（不跨桶）
    take_non = {lbl: 0 for lbl in bin_labels}
    for lbl in bin_labels:
        if deficits[lbl] <= 0: continue
        if lbl in allow_non_mono:
            use = min(deficits[lbl], supply_non[lbl])
            take_non[lbl] = use
            deficits[lbl] -= use

    # 仍有缺口：第三轮全局重平衡（不跨桶抽，只调目标），把剩余缺口按其他桶的“可用剩余”分摊
    # 可用剩余 = supply_all - (take_mono+take_non) 当前已占
    rem_need = sum(max(0, deficits[lbl]) for lbl in bin_labels)
    if rem_need > 0:
        leftover = {lbl: max(0, supply_all[lbl] - (take_mono[lbl] + take_non[lbl])) for lbl in bin_labels}
        pool = sum(leftover.values())
        if pool > 0:
            for lbl in bin_labels:
                add = int(round(rem_need * (leftover[lbl] / pool))) if pool>0 else 0
                target[lbl] = take_mono[lbl] + take_non[lbl] + add
            # 细调凑整到总数
            diff = total_target - sum(target.values())
            # 若 diff>0 还欠：按 leftover 再按序加；若 diff<0：从目标多的桶减
            seq = sorted(bin_labels, key=lambda x: leftover[x], reverse=(diff>0))
            i = 0
            while diff != 0 and seq:
                k = seq[i % len(seq)]
                if diff>0 and target[k] < supply_all[k]:
                    target[k] += 1; diff -= 1
                elif diff<0 and target[k] > 0:
                    target[k] -= 1; diff += 1
                i += 1
        else:
            # 没有任何剩余，只能接受少于 total_target 的总量
            pass

    # 最终各桶取数 = min(target, supply_all)
    final_take = {lbl: min(target[lbl], supply_all[lbl]) for lbl in bin_labels}

    # ---- 逐桶抽样（先 mono 再 non-mono；区内已按接近度排序；不重复 id）----
    used = set()
    sampled = []
    per_bin_used = {lbl: {"mono":0, "non_mono":0} for lbl in bin_labels}

    for lbl in bin_labels:
        need = final_take[lbl]
        if need <= 0: continue
        items = by_bin.get(lbl, [])
        # 先拿 mono
        for row in items:
            if len(sampled) >= total_target: break
            if row["id"] in used: continue
            if not row["monotonic"]: continue
            sampled.append(row); used.add(row["id"])
            per_bin_used[lbl]["mono"] += 1
            if per_bin_used[lbl]["mono"] + per_bin_used[lbl]["non_mono"] >= need:
                break
        # 再拿 non-mono（仅当允许）
        if per_bin_used[lbl]["mono"] < need and lbl in allow_non_mono:
            for row in items:
                if len(sampled) >= total_target: break
                if row["id"] in used: continue
                if row["monotonic"]: continue
                sampled.append(row); used.add(row["id"])
                per_bin_used[lbl]["non_mono"] += 1
                if per_bin_used[lbl]["mono"] + per_bin_used[lbl]["non_mono"] >= need:
                    break

    # ---- 落盘 ----
    out_path = os.path.join(args.out_dir, "rl25k.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for r in sampled:
            # RL 侧常用到的字段：question, gold, bin_label/bin_range, chosen_ratio(目标难度), actual_ratio
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    meta = {
        "tokenizer": TOKENIZER,
        "dirs": args.dirs,
        "bins": [{"bin": b[3], "range": [b[0], b[1]]} for b in bins],
        "supply": {
            "mono": supply_mono,
            "non_mono": supply_non,
            "all": supply_all
        },
        "quotas_request": quotas,
        "allow_non_mono_bins": sorted(list(allow_non_mono)),
        "final_take": final_take,
        "per_bin_used": per_bin_used,
        "sizes": {
            "total_target": total_target,
            "sampled": len(sampled),
            "unique_ids": len(used)
        },
        "notes": {
            "policy": "bucket=actual_ratio_of_chosen_r; difficulty=strict target-first; per-bin no-dup per-id",
            "rebalance": "if mono shortage, allow non-mono only in allow_non_mono_bins; else proportional rebalance of targets w.r.t. remaining supplies"
        }
    }
    with open(os.path.join(args.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    print(f"[OK] sampled {len(sampled)} (target={total_target}) -> {out_path}")
    for lbl in bin_labels:
        mono_u = per_bin_used[lbl]["mono"]
        non_u  = per_bin_used[lbl]["non_mono"]
        sup_m  = supply_mono[lbl]; sup_n = supply_non[lbl]
        print(f"  [{lbl}] used: mono={mono_u} non={non_u}  | supply: mono={sup_m} non={sup_n} all={sup_m+sup_n} | target={final_take[lbl]}")
    if len(sampled) < total_target:
        print(f"[WARN] supply bottleneck -> sampled={len(sampled)} < target={total_target}")

if __name__ == "__main__":
    main()
