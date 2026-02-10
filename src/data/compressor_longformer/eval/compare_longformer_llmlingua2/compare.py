#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
demo_dual_compress_long_QA_en.py

Purpose:
- Generate a single long QA sample in ENGLISH (Q: question, A: very long answer/COT).
  The answer targets ~2k–3k tokens (configurable).
- Compress the SAME answer text with two compressors at ratios 0.2/0.4/0.6/0.8:
  * Compressor 1 (LLMLingua-2): strict 512-token chunking -> per-chunk compression -> concatenate.
  * Compressor 2 (Longformer token-skip style): no chunking; question used only to provide global
    attention guidance; the compressed text is built from the answer alone.

Example:
python demo_dual_compress_long_QA_en.py \
  --llmlingua_model microsoft/llmlingua-2-xsmall \
  --longformer_model /path/to/your-trained-longformer-token-skip \
  --ratios 0.2,0.4,0.6,0.8 \
  --target_tokens 2500 \
  --output_dir outputs \
  --amp none
"""

import os
import re
import json
import argparse
from typing import List, Tuple, Dict, Any, Optional

import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForTokenClassification
from llmlingua import PromptCompressor


# ========================= Generic Helpers =========================

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def save_text(path: str, text: str):
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def tokens_len(tok, s: str) -> int:
    if not s:
        return 0
    return len(tok(s, add_special_tokens=False).input_ids)

def _effective_core_block_len(tok, requested_chunk_tokens: int) -> int:
    """
    Compute strict chunk core length so that: len(ids_with_specials) <= model_max_length.
    """
    max_len = getattr(tok, "model_max_length", None)
    try:
        specials = tok.num_special_tokens_to_add(pair=False)
    except Exception:
        specials = 2
    if isinstance(max_len, int) and 0 < max_len < 10**6:
        core = max_len - specials
        core = max(1, core)
        return min(requested_chunk_tokens, core)
    return max(1, requested_chunk_tokens - specials)

def chunk_by_tokens_strict(tok, text: str, requested_chunk_tokens: int = 512) -> List[str]:
    """
    Strict chunking: ensures each chunk, once re-encoded with add_special_tokens=True,
    does not exceed tokenizer.model_max_length.
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


# ========================= Compressor 1: LLMLingua-2 =========================

def create_lingua(llmlingua_path: str):
    try:
        return PromptCompressor(model_name=llmlingua_path, use_llmlingua2=True)
    except TypeError:
        return PromptCompressor(model_name=llmlingua_path)

def safe_compress_prompt(lingua, tok, text: str, rate: float, **kwargs) -> Dict[str, Any]:
    """
    Call llmlingua.compress_prompt with auto-fallback for unsupported kwargs and missing counts.
    """
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

def compress_with_llmlingua_by_chunks(
    text: str,
    llmlingua_model: str,
    ratios: List[float],
    requested_chunk_tokens: int = 512,
    force_reserve_digit: bool = True,
    drop_consecutive: bool = True,
    force_tokens: Optional[List[str]] = None,
) -> Dict[float, Dict[str, Any]]:
    """
    Strict 512-style chunking, per-chunk compression, then concatenate per ratio.
    Returns: ratio -> {"compressed_text", "orig_tok", "kept_tok", "achieved_rate"}.
    """
    tok = AutoTokenizer.from_pretrained(llmlingua_model, use_fast=True)
    lingua = create_lingua(llmlingua_model)

    chunks = chunk_by_tokens_strict(tok, text, requested_chunk_tokens=requested_chunk_tokens)
    results: Dict[float, Dict[str, Any]] = {}
    for r in ratios:
        comp_pieces, o_sum, k_sum = [], 0, 0
        for ch in chunks:
            res = safe_compress_prompt(
                lingua, tok, ch, r,
                force_reserve_digit=force_reserve_digit,
                drop_consecutive=drop_consecutive,
                force_tokens=(force_tokens or []),
            )
            comp_pieces.append(res["compressed_prompt"])
            o_sum += res["origin_tokens"]
            k_sum += res["compressed_tokens"]
        results[r] = {
            "compressed_text": "".join(comp_pieces).strip(),
            "orig_tok": int(o_sum),
            "kept_tok": int(k_sum),
            "achieved_rate": (k_sum / o_sum) if o_sum else 0.0,
        }
    return results


# ========================= Compressor 2: Longformer token-skip =========================

FINAL_PAT = re.compile(r"(Final\s+Answer|\\boxed\s*\{)", re.IGNORECASE)

def build_global_attention_mask(offsets, prefix_len: int, ids_shape: torch.Size, device) -> torch.Tensor:
    """
    Build global_attention_mask for Longformer: tokens whose char offsets are within the question prefix get 1.
    """
    gmask_list = []
    for (L0, R0) in offsets:
        if (L0, R0) == (0, 0):
            gmask_list.append(0)
        else:
            gmask_list.append(1 if L0 < prefix_len else 0)
    global_mask = torch.zeros(ids_shape, dtype=torch.long, device=device)
    global_mask[0, :len(gmask_list)] = torch.tensor(gmask_list, dtype=torch.long, device=device)
    return global_mask

def forward_longformer_once(
    question: str,
    cot: str,
    tokenizer,
    model,
    max_len: int = 4096,
    use_global: bool = True,
    amp: str = "none",
) -> Tuple[np.ndarray, List[Tuple[int,int]], int, str, int]:
    """
    Returns: keep_scores[L], offsets, prefix_len, full_text, orig_chars
    """
    q_prefix = f"<Q> {question.strip()} </Q> <SEP> "
    full_text = q_prefix + cot
    prefix_len = len(q_prefix)
    orig_chars = len(cot)

    enc = tokenizer(full_text, return_offsets_mapping=True, padding=False, truncation=True, max_length=max_len)
    offsets = enc["offset_mapping"]
    device  = model.device
    ids     = torch.tensor([enc["input_ids"]], dtype=torch.long, device=device)
    mask    = torch.tensor([enc["attention_mask"]], dtype=torch.long, device=device)

    if use_global:
        gmask = build_global_attention_mask(offsets, prefix_len, ids.shape, device)
    else:
        gmask = None

    if amp == "fp16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.float16) if torch.cuda.is_available() else torch.no_grad()
    elif amp == "bf16":
        autocast_ctx = torch.autocast(device_type="cuda", dtype=torch.bfloat16) if torch.cuda.is_available() else torch.no_grad()
    else:
        autocast_ctx = torch.no_grad()

    with torch.inference_mode(), autocast_ctx:
        out = model(
            input_ids=ids,
            attention_mask=mask,
            global_attention_mask=gmask if gmask is not None else None
        )
        logits = out.logits[0]  # [L,2]
        keep_scores = logits[:, 1].float().cpu().numpy()
    return keep_scores, offsets, prefix_len, full_text, orig_chars

def select_and_rebuild_from(
    keep_scores: np.ndarray,
    offsets: List[Tuple[int,int]],
    prefix_len: int,
    full_text: str,
    keep_ratio: float,
    merge_gap_chars: int = 1,
    force_keep_final: bool = True,
) -> Tuple[str, int, int, int, int, float, float]:
    """
    Builds the compressed text from top-k kept tokens (by score), merges adjacent spans, trims whitespace.
    Returns:
      compressed_text, orig_tok, kept_tok, orig_chars, kept_chars, ratio_tok, ratio_char
    """
    valid_idx = [i for i,(L0,R0) in enumerate(offsets) if (L0,R0)!=(0,0) and R0>prefix_len]
    orig_tok = len(valid_idx)
    orig_chars = len(full_text) - prefix_len
    if orig_tok == 0:
        return "", 0, 0, orig_chars, 0, 0.0, 0.0

    k = max(1, int(round(orig_tok * float(keep_ratio))))
    k = min(k, orig_tok)
    vals = np.asarray(keep_scores, dtype=np.float32)[valid_idx]
    topk_local = np.argpartition(vals, -k)[-k:]
    chosen = set(valid_idx[i] for i in topk_local)

    if force_keep_final:
        frag_cot = full_text[prefix_len:]
        for m in FINAL_PAT.finditer(frag_cot):
            gL = prefix_len + m.start()
            gR = prefix_len + m.end()
            for i,(L0,R0) in enumerate(offsets):
                if (L0,R0)==(0,0) or R0<=prefix_len:
                    continue
                if not (R0<=gL or L0>=gR):
                    chosen.add(i)

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

def compress_with_longformer(
    question: str,
    cot: str,
    longformer_model_dir: str,
    ratios: List[float],
    max_len: int = 4096,
    amp: str = "none",
) -> Dict[float, Dict[str, Any]]:
    """
    Compress the same COT text at multiple ratios (no chunking).
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(longformer_model_dir, use_fast=True)
    model = AutoModelForTokenClassification.from_pretrained(longformer_model_dir, num_labels=2).to(device).eval()

    keep_scores, offsets, prefix_len, full_text, orig_chars = forward_longformer_once(
        question=question, cot=cot, tokenizer=tok, model=model, max_len=max_len, use_global=True, amp=amp
    )

    results = {}
    for r in ratios:
        text, orig_tok, kept_tok, _, kept_chars, ratio_tok, ratio_char = select_and_rebuild_from(
            keep_scores, offsets, prefix_len, full_text, keep_ratio=r, merge_gap_chars=1, force_keep_final=True
        )
        results[r] = {
            "compressed_text": text,
            "orig_tok": int(orig_tok),
            "kept_tok": int(kept_tok),
            "achieved_rate_tok": float(ratio_tok),
            "achieved_rate_char": float(ratio_char),
            "kept_chars": int(kept_chars),
        }
    return results


# ========================= English QA Synthesis =========================

QUESTION = "Consider the language L with the only binary function symbol $f$. Let $\\varphi$ be the sentence that says \"there exists an element $x$ such that for all elements $y$, $f(x,y)=x$\". \n\nDetermine whether each of the following statements is true or false in the model $M$ given by the structure $(\\mathbb{Z},+)$:\n1. $M \\models \\varphi$ \n2. Every model of $Th(\\mathbb{Z},+)$ satisfies $\\varphi$"

# 8 reusable segments that will be repeated and varied until we reach ~target tokens
ANSWER_SEGMENTS = "Okay, let's try to figure out this problem. So, we have a language L with a binary function symbol f. The sentence φ says that there exists an element x such that for all elements y, f(x, y) = x. We need to check two things in the model M which is (ℤ, +). \n\nFirst, let me parse what φ means in this context. Since the model uses addition as the function f, the sentence φ translates to: There exists an integer x such that for all integers y, x + y = x. Hmm, right? Because f(x, y) is addition here.\n\nSo for part 1: Does M satisfy φ? Let's think. If there's an x in ℤ where adding any y to it gives back x, then that would mean x + y = x for all y. If I subtract x from both sides (assuming we can do that in integers, which we can), we get y = 0. But this has to hold for all y, which is only possible if y is 0. However, the statement requires that for all y, so this would only be true if every y is 0, which isn't the case in ℤ. Therefore, there's no such x. Wait, but maybe I made a mistake here. Let me check again.\n\nSuppose there exists some x such that for every y, x + y = x. Let's pick a specific y. Let's take y = 1. Then x + 1 = x implies 1 = 0, which is false. Similarly, any non-zero y would lead to a contradiction. Therefore, there is no such x in ℤ. Therefore, M does not satisfy φ. So part 1 is false. \n\nBut wait, what if x is something special? Like, maybe zero? Let's check. If x is 0, then for any y, 0 + y = y, which is not equal to 0 unless y is 0. So that doesn't work. So no, there's no x in integers where adding any y gives back x. Therefore, part 1 is false. \n\nNow part 2: Every model of Th(ℤ, +) satisfies φ. Th(ℤ, +) is the theory of the structure (ℤ, +), which is the set of all sentences true in (ℤ, +). The question is whether every model of this theory must satisfy φ. \n\nBut first, what is Th(ℤ, +)? Well, the theory of the integers under addition. I know that (ℤ, +) is a model of certain axioms. For example, it's a commutative group under addition. The axioms would include things like associativity, commutativity, existence of an additive identity (which is 0), and existence of additive inverses. Wait, but does Th(ℤ, +) include all sentences that are true in ℤ with addition? So, for example, the sentence that says there's an identity element (0) is in Th(ℤ, +). \n\nBut φ is saying there exists an x such that for all y, x + y = x. In other words, there's an element that acts as a \"left zero\" for addition. But in the integers, the only element that satisfies x + y = x for all y is... well, there isn't one. But maybe in some other models of Th(ℤ, +)? Wait, Th(ℤ, +) is the set of all sentences true in (ℤ, +). So models of Th(ℤ, +) are structures that satisfy all sentences that are true in (ℤ, +). Therefore, any model of Th(ℤ, +) must be elementarily equivalent to (ℤ, +). \n\nBut does elementary equivalence imply that such a model must also satisfy φ? Well, since φ is false in (ℤ, +), then if φ were logically implied by Th(ℤ, +), then it would have to be true in all models of Th(ℤ, +). But since φ is false in (ℤ, +), it's not part of Th(ℤ, +). Therefore, there might be models of Th(ℤ, +) where φ is true and others where it's false? Wait, but all models of Th(ℤ, +) are elementarily equivalent to (ℤ, +), meaning they satisfy exactly the same sentences. Since φ is false in (ℤ, +), it must be false in all models of Th(ℤ, +). Wait, but that would mean that the second statement is true? But that contradicts my previous thought. Let me think again.\n\nWait, if Th(ℤ, +) is the set of all sentences true in (ℤ, +), then any model of Th(ℤ, +) must satisfy exactly those sentences. Therefore, since φ is not true in (ℤ, +), it's not in Th(ℤ, +), so there exist models of Th(ℤ, +) where φ is false. But the question is asking if every model of Th(ℤ, +) satisfies φ. Since φ is false in (ℤ, +), which is a model of its own theory, then there exists at least one model (namely ℤ itself) where φ is false. Therefore, the statement \"every model of Th(ℤ, +) satisfies φ\" is false. \n\nWait, but maybe I'm missing something here. Let me check again. Suppose that φ were true in all models of Th(ℤ, +). Then φ would be a logical consequence of Th(ℤ, +), which would mean that φ is true in (ℤ, +), because Th(ℤ, +) is exactly the set of sentences true in ℤ. But since φ is false in ℤ, that can't be. Therefore, there must be some model of Th(ℤ, +) where φ is false (like ℤ itself) and possibly others where it might be true? Or maybe not? Wait, but maybe all models of Th(ℤ, +) also don't satisfy φ. \n\nBut how can we know? Let me think about what models of Th(ℤ, +) look like. The theory Th(ℤ, +) is the complete theory of the additive group of integers. This theory is known to be decidable and admits quantifier elimination when expanded with the divisibility predicates, but perhaps that's more than needed here. \n\nIn any case, models of Th(ℤ, +) are called elementary extensions of (ℤ, +). Now, does such a model necessarily have an element x such that for all y, x + y = x? That would be an element that acts like \"infinity\" in some sense, where adding anything to it leaves it unchanged. But in the integers, there's no such element. However, in non-standard models, perhaps?\n\nWait, but (ℤ, +) is a model of Th(ℤ, +). Since φ is false in (ℤ, +), then there exists a model of Th(ℤ, +) where φ is false. Therefore, the statement that every model of Th(ℤ, +) satisfies φ is false. Therefore, part 2 is false. \n\nBut maybe there are other models where φ is true? Suppose we have a structure that's elementarily equivalent to ℤ but has some extra elements. For example, consider the direct sum ℤ ⊕ G, where G is some other group. Wait, but would that be elementarily equivalent to ℤ? Probably not, because ℤ is a torsion-free group of rank 1, and adding another component would change the theory. \n\nAlternatively, maybe a non-standard model of Th(ℤ, +). But in such models, would there be an element x with x + y = x for all y? I don't think so. Because the theory Th(ℤ, +) includes the axioms that for any element, adding 1 repeatedly can get you further away. But maybe in a saturated model? Hmm, perhaps not. Let me think of a specific example. \n\nSuppose we have a model M of Th(ℤ, +). Then M is an abelian group elementarily equivalent to ℤ. So it must be torsion-free (since ℤ has no elements of finite order), and it must satisfy certain other properties. But does it have an element x such that x + y = x for all y? That would mean that y = 0 for all y, which would imply that the model has only one element, the zero element. But ℤ is infinite, so any model elementarily equivalent to ℤ must also be infinite. Therefore, in any model of Th(ℤ, +), there are multiple elements, so there can't be such an x. Therefore, φ is false in all models of Th(ℤ, +). \n\nWait, if that's the case, then part 2 would be true? Because every model of Th(ℤ, +) would not satisfy φ? Wait, the original question for part 2 is: Every model of Th(ℤ, +) satisfies φ. If all models of Th(ℤ, +) do not satisfy φ, then the statement is false. \n\nBut earlier reasoning suggests that φ is false in all models of Th(ℤ, +), so the statement \"every model satisfies φ\" is false. Therefore, part 2 is false. \n\nBut let me check again. Suppose that φ were true in some model of Th(ℤ, +). Then that model would have an element x such that for all y, x + y = x. Which implies that y = 0 for all y. But that would mean the model has only one element, 0. However, Th(ℤ, +) includes the statement that there exists at least two elements (for example, ∃x∃y (x ≠ y)), since ℤ has infinitely many elements. Therefore, any model of Th(ℤ, +) must be infinite. Therefore, such a model cannot have only one element, so φ cannot be satisfied. Therefore, in every model of Th(ℤ, +), φ is false. Therefore, the statement \"every model of Th(ℤ, +) satisfies φ\" is false. \n\nSo both parts 1 and 2 are false? Wait, but part 1 is asking if M (which is (ℤ, +)) satisfies φ. Since in ℤ, there's no such x, part 1 is false. Part 2 is asking if every model of Th(ℤ, +) satisfies φ. Since φ is false in all models of Th(ℤ, +), then the statement that every model satisfies φ is false. Therefore, both 1 and 2 are false. \n\nBut let me check once more. For part 2, suppose someone claims that maybe there's a model of Th(ℤ, +) where φ is true. But since Th(ℤ, +) includes the statement that the group is infinite (as ℤ is infinite and elementary equivalence preserves cardinality for infinite models?), Wait, actually, elementary equivalence doesn't necessarily preserve cardinality. Wait, no, Th(ℤ, +) is a theory that has models of all infinite cardinalities. For example, by the Löwenheim-Skolem theorem, since Th(ℤ, +) is countable, it has models of every infinite cardinality. However, regardless of the cardinality, can such a model have an element x with x + y = x for all y? \n\nIf such an element existed, then for all y in the model, y must equal x - x = 0. Wait, no. If x + y = x, then y must be 0? Wait, no. Let me think. If x + y = x, then adding -x to both sides gives y = 0. Therefore, for this x, every y must equal 0. Therefore, the entire model must consist of only the zero element. But that's a contradiction because models of Th(ℤ, +) are infinite. Therefore, there can't be such an x in any model of Th(ℤ, +). Therefore, φ is false in all models of Th(ℤ, +). Therefore, the statement that every model satisfies φ is false. \n\nTherefore, both statements 1 and 2 are false. \n\nWait, but maybe I'm missing something. Let me think of another approach. Suppose we have a model M of Th(ℤ, +). Then M is an abelian group, torsion-free, and admits division by integers? No, not necessarily. The integers themselves don't admit division. The theory Th(ℤ, +) is that of a torsion-free abelian group, but not necessarily divisible. \n\nBut regardless, in any abelian group, if there exists an element x such that for all y, x + y = x, then y must be 0 for all y. Therefore, the group must be trivial. Since models of Th(ℤ, +) are non-trivial (in fact, infinite), such an element cannot exist. Therefore, φ is false in all models of Th(ℤ, +). \n\nTherefore, both 1 and 2 are false. \n\nSo the answers are:\n\n1. False\n\n2. False\n\nBut let me check once again for part 1. The model M is (ℤ, +). The sentence φ is ∃x ∀y (x + y = x). Is there such an x in ℤ? Let's suppose x is 0. Then 0 + y = y ≠ 0 unless y is 0. If x is 5, then 5 + y = 5 implies y = 0. So no, there's no x such that for all y, x + y = x. Therefore, part 1 is false. \n\nYes, that's correct. So both statements are false. Therefore, the answers are both false."


def build_long_answer(tok, target_tokens: int = 2500) -> str:
    """
    Repeats ANSWER_SEGMENTS (with {n} increasing) until token count approaches target_tokens (~±10%).
    """
    parts = []
    n = 0
    # brief overview
    parts.append(
        "Overview: We combine multi-objective, risk-sensitive routing with pass selection and cross-trip budget "
        "allocation to strike a reliable yet affordable plan. Below we detail the modeling choices and estimates."
    )
    cur = "\n\n".join(parts)
    while tokens_len(tok, cur) < int(target_tokens * 0.9):
        seg = ANSWER_SEGMENTS[n % len(ANSWER_SEGMENTS)].format(n=n+1)
        parts.append(seg)
        cur = "\n\n".join(parts)
        n += 1
        if n > 200:
            break
    # closing retrospective
    parts.append(
        "Retrospective: The key is to bring uncertainty into the objective—quantiles and CVaR—to control tails, while "
        "allocating resources across trips at the household budget level. Caveats: holiday shifts, sudden storms, and "
        "temporary station closures can bias estimates; we therefore maintain two reliable fallbacks for quick switching."
    )
    return "\n\n".join(parts)

def synthesize_long_QA_for_both(tokenizer_name: str, target_tokens: int = 2500) -> Tuple[str, str, str]:
    """
    Use the LLMLingua tokenizer to gauge token length and build (question, answer_text, qa_full_preview).
    Both compressors will compress the SAME answer_text; the question is only used to guide Longformer attention.
    """
    tok = AutoTokenizer.from_pretrained(tokenizer_name, use_fast=True)
    answer = ANSWER_SEGMENTS
    qa_full = f"Question:\n{QUESTION}\n\nAnswer:\n{answer}\n"
    return QUESTION, answer, qa_full


# ========================= Orchestration =========================

def main():
    ap = argparse.ArgumentParser(description="Compress the same long EN QA answer with two compressors (LLMLingua-2 with strict 512 chunking & Longformer token-skip).")
    ap.add_argument("--llmlingua_model", required=True, help="LLMLingua-2 model (e.g., microsoft/llmlingua-2-xsmall)")
    ap.add_argument("--longformer_model", required=True, help="Your trained Longformer token-skip model dir")
    ap.add_argument("--ratios", default="0.2,0.4,0.6,0.8", help="Comma-separated keep ratios")
    ap.add_argument("--chunk_tokens", type=int, default=400, help="LLMLingua per-chunk token budget (including specials)")
    ap.add_argument("--target_tokens", type=int, default=2500, help="Target token count for the synthesized answer")
    ap.add_argument("--output_dir", default="outputs", help="Output directory")
    ap.add_argument("--amp", choices=["none", "fp16", "bf16"], default="none", help="Longformer AMP precision")
    args = ap.parse_args()

    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]
    ensure_dir(args.output_dir)

    # 1) Build a single long QA (English). Only the answer_text is compressed by both compressors.
    print("[INFO] Building long English QA text...")
    q, answer_text, qa_full_preview = synthesize_long_QA_for_both(args.llmlingua_model, target_tokens=args.target_tokens)
    save_text(os.path.join(args.output_dir, "original_QA_preview.txt"), qa_full_preview)

    # 2) Compressor #1: LLMLingua-2 with strict 512-token chunking
    print("[INFO] Compressing with LLMLingua-2 (strict 512 chunking)...")
    ll_outs = compress_with_llmlingua_by_chunks(
        text=answer_text,
        llmlingua_model=args.llmlingua_model,
        ratios=ratios,
        requested_chunk_tokens=args.chunk_tokens,
        force_reserve_digit=True,
        drop_consecutive=True,
        force_tokens=[],
    )
    for r, info in ll_outs.items():
        out_path = os.path.join(args.output_dir, f"llmlingua_ratio_{r:.1f}.txt")
        save_text(out_path, info["compressed_text"])
        print(f"[LLMLingua r={r:.1f}] orig_tok={info['orig_tok']} kept_tok={info['kept_tok']} achieved={info['achieved_rate']:.3f} -> {out_path}")

    # 3) Compressor #2: Longformer token-skip (no chunking); question only guides global attention
    print("[INFO] Compressing with Longformer token-skip (global attention on question)...")
    lf_outs = compress_with_longformer(
        question=q,
        cot=answer_text,
        longformer_model_dir=args.longformer_model,
        ratios=ratios,
        max_len=4096,
        amp=args.amp
    )
    for r, info in lf_outs.items():
        out_path = os.path.join(args.output_dir, f"longformer_ratio_{r:.1f}.txt")
        save_text(out_path, info["compressed_text"])
        print(f"[Longformer r={r:.1f}] orig_tok≈{info['orig_tok']} kept_tok≈{info['kept_tok']} "
              f"(token_rate={info['achieved_rate_tok']:.3f}, char_rate={info['achieved_rate_char']:.3f}) -> {out_path}")

    # 4) Summary JSON
    summary = {
        "ratios": ratios,
        "llmlingua": ll_outs,
        "longformer": lf_outs,
        "notes": "Both compressors operate on the same answer text; Longformer uses the question only for attention guidance.",
    }
    save_text(os.path.join(args.output_dir, "summary.json"), json.dumps(summary, ensure_ascii=False, indent=2))
    print("[DONE] All outputs saved to:", os.path.abspath(args.output_dir))


if __name__ == "__main__":
    main()
