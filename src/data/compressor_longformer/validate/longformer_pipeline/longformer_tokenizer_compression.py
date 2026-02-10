#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
compress_with_longformer_tokenskip_style.py

将已有推理结果（如 TokenSkip 里的 predictions.jsonl / formatted.jsonl）
用【你训练好的 Longformer 压缩器】在多种压缩比下生成训练数据，
输出字段与 LLMLingua2 管线保持一致（便于无缝替换）。

本版本改进：
- 为 Longformer 设置 global attention（question 段开启全局注意）。
- 支持批量推理（--batch_size），显著提升吞吐。
- 同一条样本一次前向，复用 logits 到多种 ratio，避免重复前向。
- 后处理使用“区间并集”替代逐字符掩膜，减少 Python 端热点。
- 可选 AMP 半精度（--amp {none,fp16,bf16}）。

输出条目字段（每条样本）：
{
  "question": str,
  "input": str or null,
  "output": str,
  "answer": str or null,
  "model_answer": str or null,
  "is_correct": bool or null,
  "cot": str,
  "compressed_cot": str,
  "original_cot_tokens": int,      # COT区间内参与排序的token数（Longformer分词）
  "compressed_cot_tokens": int,    # 被选择的token数（Top-k）
  "compression_rate": float,       # = compressed_cot_tokens / original_cot_tokens
  "original_cot_chars": int,       # COT原文字符数
  "compressed_cot_chars": int,     # 压缩后字符数
  "achieved_keep_ratio_token": float,
  "achieved_keep_ratio_char": float
}
"""

import os, re, json, argparse, contextlib
from typing import List, Dict, Any, Tuple, Optional
from tqdm import tqdm

import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForTokenClassification

# ---------- 读取/保存 ----------

def load_jsonl(path: str, encoding="utf-8") -> List[dict]:
    out=[]
    with open(path, "r", encoding=encoding, errors="strict") as f:
        for ln, line in enumerate(f, 1):
            s=line.strip()
            if not s: continue
            if s.endswith(","): s=s[:-1].strip()
            try:
                obj=json.loads(s)
                if isinstance(obj, dict): out.append(obj)
            except Exception as e:
                raise RuntimeError(f"[JSONL] line {ln} 解析失败: {e}")
    return out

def save_jsonl(items: List[dict], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path): os.remove(path)
    with open(path, "w", encoding="utf-8", errors="strict") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# ---------- COT 抽取 ----------

RE_THINK = re.compile(r"<think>([\s\S]*?)</think>", flags=re.IGNORECASE)

def extract_question(rec: dict, model_type: str) -> str:
    # 兼容 TokenSkip/GSM8K 样例：messages[0].content 或 rec['question']
    # breakpoint()
    q = None
    if isinstance(rec.get("messages"), list) and rec["messages"]:
        q = rec["messages"][0].get("content")
    if not q:
        q = rec.get("query") or rec.get("prompt") or ""
    return (q or "").strip()

def extract_cot(rec: dict, model_type: str) -> str:
    """
    - llama3：可能要从 output 中切 'The final answer is:' 之前内容；
    - qwen（默认）：直接用 rec['model_output']；若含<think>…</think>则仅取其中内容。
    """
    if model_type == "llama3":
        out = rec.get("output") or rec.get("model_output") or ""
        parts = out.split("\n\nThe final answer is:")
        cot = parts[0] if len(parts)==2 else out
        m = RE_THINK.search(cot)
        return (m.group(1).strip() if m else cot.strip())
    elif model_type == "qwen":
        cot = rec.get("model_output") or rec.get("output") or ""
        m = RE_THINK.search(cot)
        return (m.group(1).strip() if m else cot.strip())
    else:
        raw = rec.get("model_output") or rec.get("output") or rec.get("cot") or ""
        m = RE_THINK.search(raw)
        return (m.group(1).strip() if m else raw.strip())

# ---------- 前向（一次），复用到多个压缩比 ----------

def build_global_attention_mask(offsets, prefix_len: int, ids_shape: torch.Size, device) -> torch.Tensor:
    """
    为 Longformer 构建 global_attention_mask：question 段（L0 < prefix_len）置 1
    offsets: List[(L0,R0)]
    """
    gmask_list = []
    for (L0, R0) in offsets:
        if (L0, R0) == (0, 0):
            gmask_list.append(0)
        else:
            gmask_list.append(1 if L0 < prefix_len else 0)
    global_mask = torch.zeros(ids_shape, dtype=torch.long, device=device)
    # ids_shape = (1, seq_len)
    global_mask[0, :len(gmask_list)] = torch.tensor(gmask_list, dtype=torch.long, device=device)
    return global_mask

def forward_longformer_once(
    question: str,
    cot: str,
    tokenizer,
    model,
    max_len: int = 4096,
    use_global: bool = True,
    amp: str = "none",  # "none" | "fp16" | "bf16"
) -> Tuple[np.ndarray, List[Tuple[int,int]], int, str, int]:
    """
    返回：
      keep_scores(np.float32)[L], offsets(list[(L,R)]), prefix_len, full_text, orig_chars
    """
    q_prefix = f"<Q> {question.strip()} </Q> <SEP> "
    full_text = q_prefix + cot
    prefix_len = len(q_prefix)
    orig_chars = len(cot)

    enc = tokenizer(
        full_text, return_offsets_mapping=True, padding=False, truncation=True, max_length=max_len
    )
    offsets = enc["offset_mapping"]
    device  = model.device
    ids     = torch.tensor([enc["input_ids"]], dtype=torch.long, device=device)
    mask    = torch.tensor([enc["attention_mask"]], dtype=torch.long, device=device)

    # global attention
    global_mask = None
    if use_global:
        global_mask = build_global_attention_mask(offsets, prefix_len, ids.shape, device)

    # AMP 上下文
    if amp == "fp16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if torch.cuda.is_available() else contextlib.nullcontext()
    elif amp == "bf16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.bfloat16) if torch.cuda.is_available() else contextlib.nullcontext()
    else:
        autocast_ctx = contextlib.nullcontext()

    with torch.inference_mode(), autocast_ctx:
        out = model(
            input_ids=ids,
            attention_mask=mask,
            global_attention_mask=global_mask if global_mask is not None else None
        )
        logits = out.logits[0]  # [L,2]
        keep_scores = logits[:, 1].float().cpu().numpy()

    return keep_scores, offsets, prefix_len, full_text, orig_chars

# ---------- 选择与重建（高效版） ----------

FINAL_PAT = re.compile(r"(Final\s+Answer|\\boxed\s*\{)", re.IGNORECASE)

def select_and_rebuild_from(
    keep_scores: np.ndarray,
    offsets: List[Tuple[int,int]],
    prefix_len: int,
    full_text: str,
    keep_ratio: float,
    merge_gap_chars: int = 1,
    force_keep_final: bool = False,
) -> Tuple[str, int, int, int, int, float, float]:
    """
    返回：
      compressed_text, orig_tok, kept_tok, orig_chars, kept_chars, ratio_tok, ratio_char
    """
    # 选 COT 段 token
    valid_idx = [i for i,(L0,R0) in enumerate(offsets) if (L0,R0)!=(0,0) and R0>prefix_len]
    orig_tok = len(valid_idx)
    orig_chars = len(full_text) - prefix_len
    if orig_tok == 0:
        return "", 0, 0, orig_chars, 0, 0.0, 0.0

    # Top-k（局部 argpartition）
    k = max(1, int(round(orig_tok * float(keep_ratio))))
    k = min(k, orig_tok)
    vals = np.asarray(keep_scores, dtype=np.float32)[valid_idx]
    topk_local = np.argpartition(vals, -k)[-k:]
    chosen = set(valid_idx[i] for i in topk_local)

    # （可选）强制保留 Final Answer / \boxed{...}：在 COT 子串上搜一次，映射回 token
    if force_keep_final:
        frag_cot = full_text[prefix_len:]
        for m in FINAL_PAT.finditer(frag_cot):
            gL = prefix_len + m.start()
            gR = prefix_len + m.end()
            # 线性扫描（可按需优化为二分）
            for i,(L0,R0) in enumerate(offsets):
                if (L0,R0)==(0,0) or R0<=prefix_len:
                    continue
                if not (R0<=gL or L0>=gR):  # 有交集
                    chosen.add(i)

    # 由被选 token 直接构造字符区间，并做“区间并集”
    spans = []
    for i in chosen:
        L0, R0 = offsets[i]
        s, e = max(L0, prefix_len), R0
        if e > s:
            spans.append((s, e))
    spans.sort()

    merged = []
    for s, e in spans:
        if not merged or s > merged[-1][1] + merge_gap_chars:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)

    # 字符级重建（保持可读性）
    parts = [full_text[s:e] for s, e in merged]
    compressed = " ".join(p.strip() for p in parts if p.strip())
    kept_chars = len(compressed)
    kept_tok = len(chosen)

    return (
        compressed,
        orig_tok,
        kept_tok,
        orig_chars,
        kept_chars,
        (kept_tok / orig_tok) if orig_tok else 0.0,
        (kept_chars / orig_chars) if orig_chars else 0.0,
    )

# ---------- 批量推理 ----------

def batched_forward_longformer(
    batch_records: List[Tuple[str, str]],
    tokenizer,
    model,
    max_len: int,
    use_global: bool,
    amp: str,
) -> List[Tuple[np.ndarray, List[Tuple[int,int]], int, str, int]]:
    """
    对一批 (question, cot) 做一次 encode+前向，返回 per-sample 元组列表：
    (keep_scores, offsets, prefix_len, full_text, orig_chars)
    """
    qs = [f"<Q> {q.strip()} </Q> <SEP> " for q, _ in batch_records]
    cots = [c for _, c in batch_records]
    full_texts = [qp + c for qp, c in zip(qs, cots)]
    prefix_lens = [len(qp) for qp in qs]
    orig_chars_list = [len(c) for c in cots]

    # batched encode（注意：offset_mapping 返回为 list[list[(L,R)]]）
    enc = tokenizer(
        full_texts, return_offsets_mapping=True, padding=True, truncation=True, max_length=max_len
    )
    input_ids = torch.tensor(enc["input_ids"], dtype=torch.long, device=model.device)
    attn_mask = torch.tensor(enc["attention_mask"], dtype=torch.long, device=model.device)
    offsets_batched: List[List[Tuple[int,int]]] = enc["offset_mapping"]

    # batched global attention
    if use_global:
        g_list = []
        for offsets, pref_len in zip(offsets_batched, prefix_lens):
            gmask = [0 if (L0,R0)==(0,0) else (1 if L0 < pref_len else 0) for (L0,R0) in offsets]
            g_list.append(gmask)
        # pad 到相同长度
        max_len_pad = input_ids.size(1)
        g_tensor = torch.zeros((len(batch_records), max_len_pad), dtype=torch.long, device=model.device)
        for i, gmask in enumerate(g_list):
            n = min(len(gmask), max_len_pad)
            g_tensor[i, :n] = torch.tensor(gmask[:n], dtype=torch.long, device=model.device)
    else:
        g_tensor = None

    # AMP
    if amp == "fp16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if torch.cuda.is_available() else contextlib.nullcontext()
    elif amp == "bf16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.bfloat16) if torch.cuda.is_available() else contextlib.nullcontext()
    else:
        autocast_ctx = contextlib.nullcontext()

    results: List[Tuple[np.ndarray, List[Tuple[int,int]], int, str, int]] = []
    with torch.inference_mode(), autocast_ctx:
        out = model(
            input_ids=input_ids,
            attention_mask=attn_mask,
            global_attention_mask=g_tensor if g_tensor is not None else None
        )
        logits = out.logits  # [B, L, 2]
        keep = logits[..., 1].float().cpu().numpy()  # [B, L]

    # 按样本拆回
    for i in range(len(batch_records)):
        keep_scores = keep[i]
        offsets = offsets_batched[i]
        pref = prefix_lens[i]
        ft = full_texts[i]
        oc = orig_chars_list[i]
        results.append((keep_scores, offsets, pref, ft, oc))
    return results

# ---------- 主流程（与 LLMLingua2 风格对齐） ----------

def compress_with_longformer(
    data: List[dict],
    model_dir: str,
    ratios: List[float],
    model_type: str = "qwen",
    limit: Optional[int] = None,
    force_keep_final: bool = False,
    max_len: int = 4096,
    batch_size: int = 8,
    amp: str = "none",   # "none" | "fp16" | "bf16"
) -> Dict[float, List[dict]]:
    """
    返回： ratio -> list[compressed_items]，每个 items 的字段与 LLMLingua2 输出一致
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(model_dir, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(model_dir, num_labels=2).to(device).eval()

    if limit is not None and limit > 0:
        data = data[:limit]

    out: Dict[float, List[dict]] = {r: [] for r in ratios}

    # 分批处理
    idx = 0
    N = len(data)
    pbar = tqdm(total=N, desc="Compressing (Longformer batched + global)")
    while idx < N:
        j = min(idx + max(1, batch_size), N)
        batch = data[idx:j]

        # 组装当前 batch 的 (question, cot)
        q_cot_pairs = []
        metas = []  # 附加元信息以便回填
        for rec in batch:
            q = extract_question(rec, model_type)
            cot = extract_cot(rec, model_type)
            if not cot.strip():
                metas.append((rec, None))  # 标记为空
                q_cot_pairs.append(("<EMPTY>", ""))  # 占位，保持对齐
                continue
            metas.append((rec, cot))
            q_cot_pairs.append((q, cot))

        # 前向（对有效条目也会有结果，但空 COT 的条目我们后面会跳过）
        fw_results = batched_forward_longformer(
            q_cot_pairs, tok, model, max_len=max_len, use_global=True, amp=amp
        )

        # 对每条样本：一次前向，多比率复用
        for (rec, cot), (keep_scores, offsets, prefix_len, full_text, orig_chars) in zip(metas, fw_results):
            if cot is None or not cot.strip():
                # 跳过无 COT 的样本
                pbar.update(1)
                continue

            # 兼容输入数据常见字段（与 LLMLingua2 脚本一致）
            q = extract_question(rec, model_type)
            # orig_prompt = rec.get("prompt")
            final_output = rec.get("output") or rec.get("model_output") or ""
            answer = rec.get("answer")
            model_answer = rec.get("prediction")
            is_correct = rec.get("accuracy")
            ids = rec.get("id")

            for r in ratios:
                text, orig_tok, kept_tok, _, kept_chars, ratio_tok, ratio_char = select_and_rebuild_from(
                    keep_scores, offsets, prefix_len, full_text,
                    keep_ratio=r, merge_gap_chars=1, force_keep_final=force_keep_final
                )
                item = {
                    "id": ids,
                    "question": q,
                    # "input": orig_prompt,
                    "output": final_output,
                    "answer": answer,
                    "model_answer": model_answer,
                    "is_correct": is_correct,
                    "cot": cot,
                    "compressed_cot": text,
                    "original_cot_tokens": int(orig_tok),
                    "compressed_cot_tokens": int(kept_tok),
                    "compression_rate": (kept_tok / orig_tok) if orig_tok else 0.0,
                    "original_cot_chars": int(orig_chars),
                    "compressed_cot_chars": int(kept_chars),
                    "achieved_keep_ratio_token": float(ratio_tok),
                    "achieved_keep_ratio_char": float(ratio_char),
                }
                out[r].append(item)

            pbar.update(1)

        idx = j

    pbar.close()
    return out

# ---------- 命令行 ----------

def main():
    ap = argparse.ArgumentParser(description="Use trained Longformer compressor to produce TokenSkip-style compressed data (global attention + batched + AMP)")
    ap.add_argument("--input_path", required=True, help="predictions_formatted.jsonl（或任意jsonl，需含 question/messages + model_output/output）")
    ap.add_argument("--model_dir", required=True, help="你训练好的 Longformer 压缩器目录（含 tokenizer/model）")
    ap.add_argument("--output_dir", required=True, help="输出目录，按 ratio 写 jsonl")
    ap.add_argument("--ratios", default="0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1")
    ap.add_argument("--model_type", default="qwen", choices=["qwen", "llama3"], help="决定如何从样本中抽取 COT")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=不限）")
    ap.add_argument("--force_keep_final", action="store_true", default=False, help="是否强制保留 Final Answer/\\boxed{...} 附近token")
    ap.add_argument("--max_len", type=int, default=4096, help="Longformer最大长度（默认4096）")
    # 新增提速相关参数
    ap.add_argument("--batch_size", type=int, default=8, help="批量大小（默认8）")
    ap.add_argument("--amp", choices=["none", "fp16", "bf16"], default="none", help="启用半精度推理（默认none）")

    args = ap.parse_args()

    data = load_jsonl(args.input_path)
    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    outs = compress_with_longformer(
        data=data,
        model_dir=args.model_dir,
        ratios=ratios,
        model_type=args.model_type,
        limit=(args.limit if args.limit and args.limit>0 else None),
        force_keep_final=args.force_keep_final,
        max_len=args.max_len,
        batch_size=max(1, args.batch_size),
        amp=args.amp
    )

    os.makedirs(args.output_dir, exist_ok=True)
    # 与 LLMLingua2 管线一致：每个 ratio 单独写一个文件
    for r, items in outs.items():
        out_path = os.path.join(args.output_dir, f"train_outputs_compressed_ratio_{r:.1f}.jsonl")
        save_jsonl(items, out_path)
        # 计算平均压缩率（token维）
        if items:
            avg_rate = sum(x["compression_rate"] for x in items) / len(items)
        else:
            avg_rate = 0.0
        print(f"[ratio={r:.1f}] samples={len(items)}  avg_token_rate={avg_rate:.3f}  -> {out_path}")

if __name__ == "__main__":
    main()
