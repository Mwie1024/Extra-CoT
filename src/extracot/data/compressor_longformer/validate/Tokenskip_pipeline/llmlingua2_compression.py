#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
LLMLingua-2 compression for full `model_output` (no <think> matching), with STRICT 512 blocking:
- Each chunk length (after adding special tokens) <= tokenizer.model_max_length (typically 512).
- We compute core block length = model_max_length - num_special_tokens_to_add(pair=False).

New:
- Fixed sampling before compression: default sample_k=8000, seed=42 (override via CLI).
"""

import os
import re
import json
import argparse
import random
from typing import List, Dict, Any, Optional
from tqdm import tqdm

# ========================= Basic utils =========================

def load_jsonl(path: str, encoding: str = "utf-8") -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding=encoding) as f:
        for ln, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception as e:
                raise RuntimeError(f"[JSONL] line {ln} parse error: {e}")
    return out

def save_jsonl(items: List[Dict[str, Any]], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    with open(path, "w", encoding="utf-8") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def sample_records(data: List[Dict[str, Any]], k: int, seed: int) -> List[Dict[str, Any]]:
    """Deterministically sample k records; keep original order among sampled."""
    n = len(data)
    if k <= 0 or k >= n:
        return data
    rnd = random.Random(seed)
    idxs = rnd.sample(range(n), k)
    idxs.sort()
    return [data[i] for i in idxs]

# ========================= 输入字段兼容 =========================

def _first_non_empty_str(d: Dict[str, Any], keys: List[str]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _extract_from_messages(d: Dict[str, Any]) -> str:
    msgs = d.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return ""
    user_first = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
    for m in (user_first + msgs):
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""

def extract_question_generic(rec: Dict[str, Any], model_type: str = "qwen") -> str:
    q = None
    if isinstance(rec.get("messages"), list) and rec["messages"]:
        q = rec["messages"][0].get("content")
    if not q:
        q = rec.get("query") or rec.get("prompt") or ""
    return (q or "").strip()

def extract_model_output_generic(rec: Dict[str, Any]) -> str:
    return _first_non_empty_str(rec, ["model_output", "output", "response", "text"])

def coerce_boollike(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"true", "yes", "y", "1", "correct", "right"}:
            return True
        if s in {"false", "no", "n", "0", "incorrect", "wrong"}:
            return False
    return None

def extract_common_fields(rec: Dict[str, Any], model_type: str = "qwen") -> Dict[str, Any]:
    question = extract_question_generic(rec, model_type=model_type)
    final_output = extract_model_output_generic(rec)
    mapped = {
        "id": rec.get("id") or rec.get("sample_id"),
        "type": rec.get("type") or rec.get("dataset"),
        "question": question,
        "input": _first_non_empty_str(rec, ["prompt", "input", "context"]) or None,
        "output": final_output,
        "answer": rec.get("answer") or rec.get("gold") or rec.get("label"),
        "model_answer": _first_non_empty_str(rec, ["prediction", "model_answer", "response"]) or None,
        "is_correct": (
            rec.get("is_correct")
            if isinstance(rec.get("is_correct"), bool)
            else coerce_boollike(rec.get("is_correct") or rec.get("accuracy") or rec.get("correct"))
        ),
    }
    return mapped

# ========================= LLMLingua & Tokenizer =========================

def create_lingua(llmlingua_path: str):
    from llmlingua import PromptCompressor
    try:
        return PromptCompressor(model_name=llmlingua_path, use_llmlingua2=True)
    except TypeError:
        return PromptCompressor(model_name=llmlingua_path)

def build_hf_tokenizer(llmlingua_path: str):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(llmlingua_path, use_fast=True)

def tokens_len(tok, s: str) -> int:
    if not s:
        return 0
    return len(tok(s, add_special_tokens=False).input_ids)

def _effective_core_block_len(tok, requested_chunk_tokens: int) -> int:
    """
    Compute STRICT core length so that len(ids)+specials <= model_max_length (e.g., 512).
    """
    max_len = getattr(tok, "model_max_length", None)
    try:
        specials = tok.num_special_tokens_to_add(pair=False)
    except Exception:
        specials = 2  # RoBERTa-like default
    if isinstance(max_len, int) and 0 < max_len < 10**6:
        core = max_len - specials
        core = max(1, core)
        return min(requested_chunk_tokens, core)
    return max(1, requested_chunk_tokens - specials)

def chunk_by_tokens_strict(tok, text: str, requested_chunk_tokens: int = 512) -> List[str]:
    """
    STRICT chunking: ensure each chunk, when re-encoded with add_special_tokens=True,
    will NOT exceed tokenizer.model_max_length.
    """
    if not text:
        return []
    core_len = _effective_core_block_len(tok, requested_chunk_tokens)
    enc = tok(text, add_special_tokens=False)
    ids = enc["input_ids"]
    chunks = []
    for i in range(0, len(ids), core_len):
        sub_ids = ids[i:i+core_len]
        chunk_text = tok.decode(sub_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        try:
            with_special = tok(chunk_text, add_special_tokens=True).input_ids
            if isinstance(tok.model_max_length, int) and tok.model_max_length < 10**6:
                if len(with_special) > tok.model_max_length:
                    over = len(with_special) - tok.model_max_length
                    if over > 0 and len(sub_ids) > over:
                        sub_ids = sub_ids[:-over]
                        chunk_text = tok.decode(sub_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        except Exception:
            pass
        chunks.append(chunk_text)
    return chunks

# ========================= Safe compression wrapper =========================

def _drop_unsupported_and_retry(fn, text, rate, kwargs):
    import re as _re
    try:
        return fn(text, rate=rate, **kwargs)
    except TypeError as e:
        m = _re.search(r"unexpected keyword argument '(\w+)'", str(e))
        if m:
            bad = m.group(1)
            if bad in kwargs:
                new_kwargs = dict(kwargs)
                new_kwargs.pop(bad, None)
                return _drop_unsupported_and_retry(fn, text, rate, new_kwargs)
        raise

def safe_compress_prompt(lingua, tok, text: str, rate: float, **kwargs) -> Dict[str, Any]:
    """
    - Constructor already handles use_llmlingua2; don't pass it here.
    - Drop unsupported kwargs automatically.
    - If token counts missing, fall back to tokenizer counting.
    """
    res = _drop_unsupported_and_retry(lingua.compress_prompt, text, rate, kwargs)

    comp_txt = res.get("compressed_prompt") or res.get("compressed_text") or ""
    orig_tok = res.get("origin_tokens")
    kept_tok = res.get("compressed_tokens")
    rate_out = res.get("rate")

    if orig_tok is None:
        orig_tok = tokens_len(tok, text)
    if kept_tok is None:
        kept_tok = tokens_len(tok, comp_txt)
    if rate_out is None:
        rate_out = (kept_tok / orig_tok) if orig_tok else 0.0

    return {
        "compressed_prompt": comp_txt,
        "origin_tokens": int(orig_tok),
        "compressed_tokens": int(kept_tok),
        "rate": rate_out,
    }

# ========================= Core compression (FULL model_output) =========================

def compress_output_llmlingua2(
    data: List[Dict[str, Any]],
    llmlingua_path: str,
    ratios: List[float],
    requested_chunk_tokens: int = 512,
    force_reserve_digit: bool = True,
    force_tokens: List[str] = None,
    drop_consecutive: bool = True,
    model_type: str = "qwen",   # only affects input-field extraction
) -> Dict[float, List[Dict[str, Any]]]:
    """
    For each record:
      - Take FULL model_output (no <think> filtering)
      - Strictly chunk by tokenizer budget (including specials)
      - Compress each chunk with LLMLingua-2 at each ratio, then concat
    """
    lingua = create_lingua(llmlingua_path)
    tok = build_hf_tokenizer(llmlingua_path)

    outs: Dict[float, List[Dict[str, Any]]] = {r: [] for r in ratios}
    skipped_no_output = 0
    processed = 0

    for rec in tqdm(data, desc="LLMLingua2 STRICT-512 compress full model_output"):
        processed += 1

        fields = extract_common_fields(rec, model_type=model_type)
        mo = (fields["output"] or "").strip()
        if not mo:
            skipped_no_output += 1
            continue

        chunks = chunk_by_tokens_strict(tok, mo, requested_chunk_tokens=requested_chunk_tokens)
        if not chunks:
            skipped_no_output += 1
            continue

        for r in ratios:
            comp_pieces: List[str] = []
            orig_tok_sum = 0
            kept_tok_sum = 0
            for ch in chunks:
                res = safe_compress_prompt(
                    lingua, tok, ch, r,
                    force_reserve_digit=force_reserve_digit,
                    drop_consecutive=drop_consecutive,
                    force_tokens=(force_tokens or []),
                )
                comp_pieces.append(res["compressed_prompt"])
                orig_tok_sum += res["origin_tokens"]
                kept_tok_sum += res["compressed_tokens"]

            comp_text = "".join(comp_pieces).strip()
            rate_val = (kept_tok_sum / orig_tok_sum) if orig_tok_sum else 0.0

            item = {
                "id": fields["id"],
                "type": fields["type"],
                "question": fields["question"],
                "input": fields["input"],
                "output": mo,                 # full original model output
                "answer": fields["answer"],
                "model_answer": fields["model_answer"],
                "is_correct": fields["is_correct"],
                "cot": mo,                    # treat whole model_output as COT source
                "compressed_cot": comp_text,
                "original_cot_tokens": int(orig_tok_sum),
                "compressed_cot_tokens": int(kept_tok_sum),
                "compression_rate": float(rate_val),
            }
            outs[r].append(item)

    print(f"[INFO] processed={processed}, skipped(no model_output)={skipped_no_output}")
    return outs

# ========================= CLI & Main =========================

def main():
    ap = argparse.ArgumentParser(
        description="Compress FULL `model_output` using LLMLingua-2 with STRICT 512 blocking (respecting special tokens)."
    )
    ap.add_argument("--input_path", required=True, help="Input JSONL path")
    ap.add_argument("--output_dir", required=True, help="Output directory")
    ap.add_argument("--llmlingua_path", required=True, help="LLMLingua-2 model path/name")
    ap.add_argument("--ratios", default="0.9,0.8,0.7,0.6,0.5", help="Comma-separated keep ratios")
    ap.add_argument("--chunk_tokens", type=int, default=512, help="Requested per-chunk token budget (including specials)")
    ap.add_argument("--force_reserve_digit", action="store_true", help="Force reserve tokens with digits")
    ap.add_argument("--drop_consecutive", action="store_true", help="Drop consecutive duplicates")
    ap.add_argument("--force_tokens", default="", help="Comma-separated tokens to force (optional)")
    ap.add_argument("--model_type", default="qwen", choices=["qwen", "llama3"],
                    help="Only affects input-field extraction; compression logic unchanged")
    ap.add_argument("--sample_k", type=int, default=8000,
                    help="Number of records to sample for compression (<=0 means use all; default 8000)")
    ap.add_argument("--seed", type=int, default=42, help="Random seed for sampling (default 42)")
    args = ap.parse_args()

    data = load_jsonl(args.input_path)

    # Fixed-size sampling (default 8k with seed=42)
    orig_N = len(data)
    data = sample_records(data, args.sample_k, args.seed)
    print(f"[INFO] Sampling: using {len(data)} of {orig_N} records (sample_k={args.sample_k}, seed={args.seed})")

    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    force_tokens = [t for t in args.force_tokens.split(",") if t.strip()]

    outs = compress_output_llmlingua2(
        data=data,
        llmlingua_path=args.llmlingua_path,
        ratios=ratios,
        requested_chunk_tokens=args.chunk_tokens,
        force_reserve_digit=args.force_reserve_digit,
        force_tokens=force_tokens,
        drop_consecutive=args.drop_consecutive,
        model_type=args.model_type,
    )

    os.makedirs(args.output_dir, exist_ok=True)
    for r, items in outs.items():
        out_path = os.path.join(args.output_dir, f"train_outputs_compressed_ratio_{r:.1f}.jsonl")
        save_jsonl(items, out_path)
        avg_rate = (sum((x["compressed_cot_tokens"] / max(1, x["original_cot_tokens"])) for x in items) / len(items)) if items else 0.0
        print(f"[ratio={r:.1f}] samples={len(items)} avg_token_rate={avg_rate:.3f} -> {out_path}")

if __name__ == "__main__":
    main()
