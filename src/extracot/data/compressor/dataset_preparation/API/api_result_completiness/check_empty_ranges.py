#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, argparse, re
from typing import List, Dict, Any

def _read_jsonl(path:str)->List[dict]:
    out=[]; bad=0
    with open(path,"r",encoding="utf-8") as f:
        for ln, line in enumerate(f,1):
            s=line.strip()
            if not s: continue
            if s.endswith(","): s=s[:-1].strip()
            try:
                obj=json.loads(s)
                if isinstance(obj,dict): out.append(obj)
            except Exception:
                bad+=1
    if bad: print(f"[read_jsonl] skipped {bad} malformed line(s)")
    return out

def _load_any(path:str)->List[dict]:
    # 尝试 JSON；否则 JSONL
    with open(path,"r",encoding="utf-8") as f:
        txt=f.read()
    try:
        data=json.loads(txt)
        if isinstance(data, list): return data
        if isinstance(data, dict): return [data]
    except Exception:
        pass
    return _read_jsonl(path)

def _is_empty_ranges(r):
    if r is None: return True
    if isinstance(r, list): return len(r)==0
    if isinstance(r, str): return len(r.strip())==0
    return True

def main():
    ap=argparse.ArgumentParser(description="扫描结果文件，导出空 ranges 的 (id, chunk_id) 清单")
    ap.add_argument("-i","--input",required=True,help="结果 JSONL/JSON（含 chunks[].ranges ）")
    ap.add_argument("-o","--output",required=True,help="导出清单 JSONL（每行：{id,chunk_id}）")
    args=ap.parse_args()

    data=_load_any(args.input)

    total_samples=len(data)
    total_chunks=0
    empty_list=[]

    for rec in data:
        rid=rec.get("id")
        try:
            # 统一 id 类型为整数（可回退为原值）
            rid_int=int(rid)
            rid=rid_int
        except Exception:
            pass
        chunks=rec.get("chunks") or []
        for ch in chunks:
            total_chunks+=1
            ranges=ch.get("ranges")
            if _is_empty_ranges(ranges):
                empty_list.append({"id": rid, "chunk_id": ch.get("chunk_id")})

    with open(args.output,"w",encoding="utf-8") as f:
        for it in empty_list:
            f.write(json.dumps(it, ensure_ascii=False)+"\n")

    print("\n=== 扫描完成 ===")
    print(f"样本数: {total_samples}")
    print(f"总 chunks: {total_chunks}")
    print(f"空 ranges 的 chunk 数: {len(empty_list)}")
    print("示例前 10 条：", empty_list[:10])

if __name__=="__main__":
    main()
