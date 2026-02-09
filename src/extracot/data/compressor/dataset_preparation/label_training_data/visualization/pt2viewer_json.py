#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, argparse
import torch
from typing import List, Dict, Any, Tuple
from transformers import LongformerTokenizerFast
from tqdm import tqdm

SPECIAL_TOKENS = {"<s>", "</s>", "<pad>", "<mask>"}

def tokens_to_text_and_offsets(tokens: List[str]) -> Tuple[str, List[Tuple[int,int]]]:
    """
    依据 RoBERTa/Longformer 的 BPE“可视化惯例”重建文本，并给出每个 token 的字符区间 [L,R)。
    规则：
      - 以 'Ġ' 表示前置空格：去掉 'Ġ'，并在输出前补一个空格
      - 以 '▁' 表示前置空格（兼容 SentencePiece）：同理
      - 'Ċ' 作为换行 '\n'
      - 其它子词直接拼接
      - special tokens 记 offset=(0,0)
    """
    out = []
    offsets = []
    cur = 0

    for tok in tokens:
        if tok in SPECIAL_TOKENS:
            offsets.append((0,0))
            continue

        piece = tok
        add = ""

        if piece == "Ċ":
            add = "\n"
        elif piece.startswith("Ġ"):
            add = " " + piece[1:]
        elif piece.startswith("▁"):
            add = " " + piece[1:]
        else:
            add = piece

        L = cur
        out.append(add)
        cur += len(add)
        R = cur
        offsets.append((L,R))

    return "".join(out), offsets

def load_pt(path: str) -> List[Dict[str, Any]]:
    obj = torch.load(path, map_location="cpu")
    if isinstance(obj, list):
        return obj
    raise RuntimeError("期望 torch.save(list[dict]) 的格式；当前对象不是 list。")

def tolist(x):
    return x.tolist() if hasattr(x, "tolist") else list(x)

def main():
    ap = argparse.ArgumentParser(description="把 Longformer .pt（input_ids+labels）转为可视化 JSONL")
    ap.add_argument("-i","--input", required=True, help=".pt 路径（torch.save 的 list[dict]）")
    ap.add_argument("-o","--output", required=True, help="输出 JSONL（每行一个样本）")
    ap.add_argument("--tokenizer", default="/Users/mwie/Downloads/longformer", help="分词器路径或名称")
    ap.add_argument("--max-samples", type=int, default=0, help="只导出前 N 条（0=全部）")
    args = ap.parse_args()

    data = load_pt(args.input)
    tok = LongformerTokenizerFast.from_pretrained(args.tokenizer)

    n_take = len(data) if args.max_samples <= 0 else min(args.max_samples, len(data))
    cnt = 0
    with open(args.output, "w", encoding="utf-8") as f:
        for rec in tqdm(data[:n_take], desc="Export"):
            sid = rec.get("id")
            input_ids = tolist(rec.get("input_ids"))
            labels = tolist(rec.get("labels"))
            global_mask = tolist(rec.get("global_attention_mask", []))

            # 还原 token 字符串
            tokens = tok.convert_ids_to_tokens(input_ids)

            # 可视化文本 + 近似 offsets
            text, offsets = tokens_to_text_and_offsets(tokens)

            # JSONL 行
            out = {
                "id": sid,
                "tokens": tokens,
                "labels": labels,
                "global_mask": global_mask,
                "text": text,
                "offsets": offsets
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            cnt += 1

    print(f"\n导出完成：{cnt} 条 -> {args.output}")

if __name__ == "__main__":
    main()
