#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, argparse, re
from typing import List, Dict, Any, Tuple

def read_jsonl(path:str)->List[dict]:
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        for line in f:
            s=line.strip()
            if not s: continue
            if s.endswith(","): s=s[:-1].strip()
            try:
                obj=json.loads(s)
                if isinstance(obj,dict): out.append(obj)
            except Exception:
                pass
    return out

def is_nonempty_ranges(r)->bool:
    return isinstance(r, list) and len(r)>0

def to_int_or_raw(x):
    try: return int(x)
    except Exception: return x

def coerce_bool(x)->bool:
    if isinstance(x,bool): return x
    if isinstance(x,str): return x.strip().lower() in {"1","true","yes"}
    if isinstance(x,int): return x!=0
    return bool(x)

def extract_retry_mapping(retry_list:List[dict])->Dict[Tuple[Any,int], List[str]]:
    """兼容两种格式：按 chunk / 按样本"""
    repl={}
    for it in retry_list:
        rid = to_int_or_raw(it.get("id"))
        if isinstance(it.get("chunks"), list):
            # 样本级
            for ch in it["chunks"]:
                cid = ch.get("chunk_id")
                try: cid=int(cid)
                except Exception: pass
                ranges = ch.get("ranges")
                ok     = ch.get("ok", True)
                if is_nonempty_ranges(ranges) and coerce_bool(ok):
                    repl[(rid, cid)] = [str(x) for x in ranges]
        else:
            # chunk级
            cid = it.get("chunk_id")
            try: cid=int(cid)
            except Exception: pass
            ranges = it.get("ranges")
            ok     = it.get("ok", True)
            if is_nonempty_ranges(ranges) and coerce_bool(ok):
                repl[(rid, cid)] = [str(x) for x in ranges]
    return repl

def main():
    ap=argparse.ArgumentParser(description="把重试结果合并回大结果文件（仅用非空 ranges 覆盖）")
    ap.add_argument("-b","--base", required=True, help="基准结果 JSONL（含 chunks[].ranges ...）")
    ap.add_argument("-r","--retry", required=True, help="重试输出 JSONL（每行一个 chunk 或 每行一个样本）")
    ap.add_argument("-o","--output", required=True, help="合并后的输出 JSONL")
    ap.add_argument("--replace-only-empty", action="store_true", default=True,
                    help="仅在 base 原 ranges 为空时才覆盖（默认开启）")
    args=ap.parse_args()

    base  = read_jsonl(args.base)
    retry = read_jsonl(args.retry)

    repl = extract_retry_mapping(retry)
    if not repl:
        print("WARNING: 重试文件里没有可用的非空 ranges（ok=True）。不做任何修改。")

    replaced=0
    total_chunks=0
    total_nonempty=0
    skipped_due_to_nonempty=0
    missed_keys=0

    # 为了统计：构建所有 (id,cid) -> True 的集合，看看有多少 key 在 base 中找不到
    keys_in_base=set()
    for rec in base:
        rid=to_int_or_raw(rec.get("id"))
        for ch in (rec.get("chunks") or []):
            keys_in_base.add((rid, ch.get("chunk_id")))

    for key in repl.keys():
        if key not in keys_in_base:
            missed_keys += 1

    out_lines=[]
    for rec in base:
        rid=to_int_or_raw(rec.get("id"))
        chunks=rec.get("chunks") or []
        # 替换
        for ch in chunks:
            total_chunks+=1
            cid=ch.get("chunk_id")
            key=(rid, cid)
            if key in repl:
                if is_nonempty_ranges(ch.get("ranges")) and args.replace_only_empty:
                    # base 已非空且只允许替换空 -> 跳过
                    skipped_due_to_nonempty += 1
                else:
                    ch["ranges"]=repl[key]
                    replaced+=1
            if is_nonempty_ranges(ch.get("ranges")): total_nonempty+=1
        # 更新 labeled_count
        rec["labeled_count"]=sum(1 for ch in chunks if is_nonempty_ranges(ch.get("ranges")))
        out_lines.append(rec)

    with open(args.output,"w",encoding="utf-8") as f:
        for obj in out_lines:
            f.write(json.dumps(obj, ensure_ascii=False)+"\n")

    rate=(total_nonempty/total_chunks*100.0) if total_chunks else 0.0
    print("\n=== 合并完成 ===")
    print(f"可用于替换的 retry 键数：{len(repl)}（有 {missed_keys} 个键在 base 中找不到）")
    print(f"替换的 chunk 数（写回）：{replaced}")
    if args.replace_only_empty:
        print(f"因 base 原 ranges 非空而跳过的数：{skipped_due_to_nonempty}")
    print(f"合并后总 chunks: {total_chunks}，其中有 ranges 的: {total_nonempty}，占比 {rate:.1f}%")
    print(f"输出：{args.output}")

if __name__=="__main__":
    main()
