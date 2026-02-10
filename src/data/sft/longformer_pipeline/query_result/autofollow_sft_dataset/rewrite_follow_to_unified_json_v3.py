#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sft_mixture.py
- 读取 clean_ratios_tiktoken.py 产出的 recat_rxxx.cleaned.jsonl
- 构造 SFT 数据：
  020-080: input 末尾加 <COMP_xx>；output 头部加 <COMP_xx> + 换行
  100: 默认“半加半不加”；可选 --r100_duplicate 做成对复制
- 可选等量平衡（--balance equal）
- 输出 JSONL（每行一个 {"instruction","input","output"}）
"""
import argparse, json, random, sys
from pathlib import Path
from typing import Dict, Any, Iterable, List, Tuple

INSTRUCTION = r"Please reason step by step, and put your final answer within \boxed{}."

def load_jsonl(p: Path) -> Iterable[Dict[str, Any]]:
    with p.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except Exception as e:
                sys.stderr.write(f"[WARN] {p.name}:{ln} JSON parse error: {e}\n")

def write_jsonl(items: Iterable[Dict[str, Any]], p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as w:
        for obj in items:
            w.write(json.dumps(obj, ensure_ascii=False) + "\n")

def write_json_array(items, p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as w:
        json.dump(list(items), w, ensure_ascii=False)

def first_nonempty(d: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""

def comp_token(ratio_str: str, zero_pad: bool) -> str:
    # ratio_str: "020" -> <COMP_20>（默认）；如 --zero_pad 则 <COMP_020>
    if zero_pad:
        return f"<COMP_{ratio_str}>"
    try:
        pct = int(ratio_str)
    except Exception:
        pct = 0
    return f"<COMP_{pct}>"

def rewrite_item(obj: Dict[str, Any], ratio: str, add_token_io: bool, token: str) -> Dict[str, str]:
    """构造一条 SFT 三元组；add_token_io=True 表示 input 末尾追加 token、output 头部前置 token。"""
    q = first_nonempty(obj, ["question","query","question_raw","question_text"])
    o = first_nonempty(obj, ["output","model_output"])
    if not q or not o:
        return {}
    if add_token_io:
        return {
            "instruction": INSTRUCTION,
            "input": f"{q} {token}",
            "output": f"{token}\n{o}",
        }
    else:
        return {
            "instruction": INSTRUCTION,
            "input": q,
            "output": o,
        }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True, help="包含 recat_rxxx.cleaned.jsonl 的目录")
    ap.add_argument("--outfile", type=Path, required=True, help="输出 JSONL 文件路径")
    ap.add_argument("--include_r020", action="store_true", help="是否并入 0.2 档（默认不并入）")
    ap.add_argument("--zero_pad", action="store_true", help="token 用 <COMP_020> 风格（默认 <COMP_20>）")
    ap.add_argument("--r100_tag_fraction", type=float, default=0.5,
                    help="100 档中加 <COMP_100> 的比例（0~1，小数），仅在未开启 --r100_duplicate 时生效")
    ap.add_argument("--r100_duplicate", default=False, action="store_true",
                    help="100 档每条复制一遍：一条加 <COMP_100>，一条不加（会扩容样本数）")
    ap.add_argument("--balance", choices=["none","equal"], default="none",
                    help="是否各档等量下采样（equal：以最小档计数为基准）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    ratios = ["040","060","080","100"]
    if args.include_r020:
        ratios = ["020"] + ratios

    # 读取各档清洗文件
    per_ratio: Dict[str, List[Dict[str, Any]]] = {}
    for r in ratios:
        p = args.dir / f"recat_r{r}.cleaned.jsonl"
        if not p.exists():
            sys.stderr.write(f"[WARN] missing {p}\n")
            per_ratio[r] = []
            continue
        per_ratio[r] = list(load_jsonl(p))
        print(f"[load] r={r} n={len(per_ratio[r])}")

    # 构造输出（不平衡：直接全部写；平衡：对每档做等量下采样）
    # 020/040/060/080：都“加 token”（input 末尾、output 头部）
    # 100：按 r100_tag_fraction 或 duplicate 决定
    out_items: List[Dict[str, str]] = []
    # 先“重写”成目标格式，分别收集
    rewritten_by_ratio: Dict[str, List[Dict[str, str]]] = {}

    for r, data in per_ratio.items():
        tok = comp_token(r, args.zero_pad)
        if r in ("020","040","060","080"):
            items = []
            for ex in data:
                new_obj = rewrite_item(ex, r, add_token_io=True, token=tok)
                if new_obj: items.append(new_obj)
            rewritten_by_ratio[r] = items
        elif r == "100":
            items = []
            # 方案 1：duplicate
            if args.r100_duplicate:
                for ex in data:
                    with_tok = rewrite_item(ex, r, add_token_io=True, token=tok)   # 带 <COMP_100>
                    no_tok   = rewrite_item(ex, r, add_token_io=False, token=tok)  # 不带
                    if with_tok: items.append(with_tok)
                    if no_tok:   items.append(no_tok)
            else:
                # 方案 2：不复制，随机抽取 fraction 比例“带 token”
                idxs = list(range(len(data)))
                random.shuffle(idxs)
                k = int(round(len(idxs) * max(0.0, min(1.0, args.r100_tag_fraction))))
                sel = set(idxs[:k])
                for i, ex in enumerate(data):
                    if i in sel:
                        new_obj = rewrite_item(ex, r, add_token_io=True, token=tok)
                    else:
                        new_obj = rewrite_item(ex, r, add_token_io=False, token=tok)
                    if new_obj: items.append(new_obj)
            rewritten_by_ratio[r] = items

    # 平衡策略
    if args.balance == "equal":
        # 取各档最小样本数
        sizes = {r: len(v) for r, v in rewritten_by_ratio.items() if v}
        if not sizes:
            print("[ERR] nothing to write")
            return
        m = min(sizes.values())
        print(f"[balance] equal -> per-ratio={m}")
        for r, arr in rewritten_by_ratio.items():
            if not arr: continue
            # 随机下采样
            arr2 = arr[:]
            random.shuffle(arr2)
            out_items.extend(arr2[:m])
    else:
        # 不平衡：全量并上
        for arr in rewritten_by_ratio.values():
            out_items.extend(arr)

    random.shuffle(out_items)
    write_json_array(out_items, args.outfile)
    print(f"[DONE] wrote {len(out_items)} items -> {args.outfile}")

if __name__ == "__main__":
    main()
