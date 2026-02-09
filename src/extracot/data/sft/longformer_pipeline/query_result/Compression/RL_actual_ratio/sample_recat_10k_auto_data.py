#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sample 10k data from recat_rXXX.jsonl files according to desired ratio proportions,
matching entries from A.jsonl by question text and adding the special_token field.

Usage example:
  python sample_recat_10k.py \
    --a_path /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/longformer_pipeline/Compression/A.jsonl \
    --recat_dir /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/longformer_pipeline/Compression/RL_actual_ratio \
    --output /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/longformer_pipeline/Compression/RL_actual_ratio/recat_10k_sampled.jsonl \
    --seed 42

Notes:
- Exact matching is done between A.jsonl["question_raw"] and recat_rXXX.jsonl["question"],
  after simple whitespace normalization. If no match, the item is skipped.
- If a ratio doesn't have enough matched items to meet its quota, all available items are used.
  The final total may be less than 10k in that case; a stats JSON is written alongside the output.
"""

import argparse
import json
import random
import re
from pathlib import Path
from collections import defaultdict

RATIO_PROPS = {
    # ratio -> proportion of 10k
    "020": 0.18,
    "040": 0.24,
    "060": 0.26,
    "080": 0.22,
    "100": 0.10,
}

RATIO_FILES = ["020", "040", "060", "080", "100"]

def normalize_text(s: str) -> str:
    """Simple normalization: trim, collapse whitespace, remove full-width spaces."""
    if s is None:
        return ""
    s = s.replace("\u3000", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def parse_ratio_key_from_token(special_token: str) -> str | None:
    """
    Extract '020'/'040'/.../'100' from tokens like '<COMP_80>', '<COMP_080>', '<COMP_0.8>' etc.
    Returns a zero-padded 3-digit string or None if not recognized.
    """
    if not special_token:
        return None
    m = re.search(r'COMP[_-]?(\d+(?:\.\d+)?)', special_token)
    if not m:
        return None
    num_str = m.group(1)
    try:
        if "." in num_str:
            # e.g. '0.8' -> 80
            val = float(num_str)
            pct = int(round(val * 100))
        else:
            # e.g. '80' or '080' -> 80
            pct = int(num_str)
            if pct <= 1:  # handle odd cases like '0' or '1'
                pct = int(round(float(num_str) * 100))
    except Exception:
        return None

    # Snap to the supported set {20, 40, 60, 80, 100} by nearest value
    candidates = [20, 40, 60, 80, 100]
    pct = min(candidates, key=lambda x: abs(x - pct))
    return f"{pct:03d}"

def load_jsonl(path: Path):
    """Yield JSON objects, skipping malformed lines."""
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                print(f"[WARN] {path.name}:{ln} JSON parse error: {e}")
                continue
            yield obj

def load_recat_mapping(path: Path) -> dict:
    """
    Load recat_rXXX.jsonl into a map of normalized question -> full object.
    Prefer 'question', fallback to 'question_raw' / 'question_text' if needed.
    """
    mapping = {}
    if not path.exists():
        print(f"[WARN] recat file not found: {path}")
        return mapping

    for obj in load_jsonl(path):
        q = obj.get("question") or obj.get("question_raw") or obj.get("question_text") or obj.get("query")
        if not q:
            continue
        mapping[q] = obj
    return mapping

def compute_quotas(total: int = 10_000) -> dict:
    """Compute integer quotas by rounding and fix rounding drift to match total exactly."""
    quotas = {k: int(round(total * p)) for k, p in RATIO_PROPS.items()}
    drift = total - sum(quotas.values())
    if drift != 0:
        # add/subtract the drift to the largest bucket (060) by default
        quotas["060"] = quotas.get("060", 0) + drift
    return quotas

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--a_path", required=True, type=Path, help="Path to A.jsonl")
    parser.add_argument("--recat_dir", required=True, type=Path, help="Dir of recat_rXXX.jsonl files")
    parser.add_argument("--output", required=True, type=Path, help="Output JSONL path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling")
    args = parser.parse_args()

    random.seed(args.seed)

    # Compute quotas for 10k
    quotas = compute_quotas(10_000)

    # Load recat maps
    recat_maps = {}
    for key in RATIO_FILES:
        recat_path = args.recat_dir / f"recat_r{key}.jsonl"
        recat_maps[key] = load_recat_mapping(recat_path)

    # Build candidates by matching A.jsonl against corresponding recat map
    per_ratio_candidates: dict[str, dict[str, dict]] = defaultdict(dict)  # norm_q -> obj
    total_a = 0
    not_found = 0
    no_token = 0

    for aobj in load_jsonl(args.a_path):
        total_a += 1
        qraw = aobj.get("question") or aobj.get("question_text") or aobj.get("query")
        # breakpoint()
        token = aobj.get("extra_info").get("special_token") or aobj.get("specialToken") or aobj.get("comp_token")
        if not qraw:
            continue

        ratio_key = parse_ratio_key_from_token(token)
        if not ratio_key or ratio_key not in recat_maps:
            no_token += 1
            continue

        norm_q = qraw
        rec_map = recat_maps[ratio_key]
        rec_obj = rec_map.get(norm_q)
        if not rec_obj:
            not_found += 1
            continue

        out_obj = dict(rec_obj)
        # Ensure the token is in standardized format if missing
        if token:
            out_obj["special_token"] = token
        else:
            pct = int(ratio_key)  # '080' -> 80
            out_obj["special_token"] = f"<COMP_{pct}>"

        # Deduplicate by normalized question within the same ratio
        per_ratio_candidates[ratio_key][norm_q] = out_obj

    # Sample by quota
    selected = []
    per_ratio_stats = {}
    for rk in RATIO_FILES:
        cand = list(per_ratio_candidates.get(rk, {}).values())
        quota = quotas.get(rk, 0)
        if len(cand) >= quota:
            picked = random.sample(cand, quota)
        else:
            picked = cand  # shortfall
        selected.extend(picked)
        per_ratio_stats[rk] = {
            "quota": quota,
            "available": len(cand),
            "selected": len(picked),
            "shortfall": max(0, quota - len(cand)),
            "recat_file": str(args.recat_dir / f"recat_r{rk}.jsonl"),
        }

    # Write output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as w:
        for obj in selected:
            w.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # Write stats
    stats = {
        "total_requested": 10_000,
        "total_written": len(selected),
        "total_in_A_seen": total_a,
        "no_token_or_unrecognized_ratio": no_token,
        "not_found_in_recat": not_found,
        "quotas": compute_quotas(10_000),
        "per_ratio": per_ratio_stats,
        "recat_dir": str(args.recat_dir),
        "a_path": str(args.a_path),
        "output": str(args.output),
        "seed": args.seed,
    }
    stats_path = args.output.with_suffix(args.output.suffix + ".stats.json")
    with stats_path.open("w", encoding="utf-8") as w:
        json.dump(stats, w, ensure_ascii=False, indent=2)

    print(f"[DONE] Wrote {len(selected)} lines to: {args.output}")
    print(f"[INFO] Stats written to: {stats_path}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
