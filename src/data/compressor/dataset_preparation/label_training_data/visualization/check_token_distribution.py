#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse, torch, math
from collections import Counter

def quantiles(vals, ps=(0,0.25,0.5,0.75,1.0)):
    if not vals: return {}
    xs = sorted(vals)
    out={}
    for p in ps:
        k = (len(xs)-1)*p
        f = math.floor(k); c = math.ceil(k)
        if f==c: out[p]=xs[int(k)]
        else:    out[p]=xs[f]*(c-k)+xs[c]*(k-f)
    return out

def analyze_pt(path):
    data = torch.load(path, map_location="cpu")
    n = len(data)
    total_tokens = 0
    eff_tokens   = 0
    kept_tokens  = 0
    per_sample_keep = []

    for item in data:
        labels = item["labels"].tolist()
        eff = sum(1 for x in labels if x != -100)
        keep= sum(1 for x in labels if x == 1)
        total_tokens += len(labels)
        eff_tokens   += eff
        kept_tokens  += keep
        per_sample_keep.append((keep/eff) if eff>0 else 0.0)

    q = quantiles(per_sample_keep)
    print("\n=== PT 压缩统计 ===")
    print(f"样本数: {n}")
    print(f"总 token 数: {total_tokens}")
    print(f"有效 token 数(参与损失): {eff_tokens}")
    print(f"保留 token 数(=1): {kept_tokens}")
    print(f"总体保留比例: {kept_tokens/eff_tokens:.4f}" if eff_tokens else "总体保留比例: n/a")
    print("\n按样本保留比例分布（分位数）:")
    for p in [0,0.25,0.5,0.75,1.0]:
        v = q.get(p, float('nan'))
        print(f"  p{int(p*100):>2}: {v:.4f}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-i","--input", required=True, help=".pt 文件路径")
    args = ap.parse_args()
    analyze_pt(args.input)
