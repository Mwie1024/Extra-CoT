#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
eval_all_ratios_vllm_paperctrl.py

一次性评测 TokenSkip 论文中的五类长度控制基线：
  ctrls ∈ {beconcise, onlynumbers, abbrewords, lc-prompt, truncation} 或 --ctrls all_paper

- beconcise   : 在 user 末尾追加 `Be concise.`（表 B.3）
- onlynumbers : 在 user 末尾追加 `Only use numbers or equations.`（表 B.3）
- abbrewords  : 在 user 末尾追加 `Abbreviate words as much as possible.`（表 B.3）
- lc-prompt   : 在 user 末尾追加 `Please reduce {reduce_pct}% of the words in your Chain-of-Thought process.`
                其中 reduce_pct = round((1-γ)*100)。来自表 1/正文“Length-control Prompts”
- truncation  : 把 max_new_tokens 硬性改为 round(base * γ) 作为生成上限（论文表 1 的 Truncation）

vLLM 侧调用形式与 eval_all_ratios_vllm.py 保持一致：
- 使用 OpenAI 兼容的 /v1/chat/completions
- role-based messages（system+user）
- AsyncOpenAI + asyncio.Semaphore 用 --processes 控制并发
- 其它逻辑（prompt 变体、truncation、评测与落盘）保持不变
"""

import os, re, json, time, argparse, random, asyncio
from typing import List, Dict, Any, Optional

import numpy as np
import torch
from transformers import AutoTokenizer
from tqdm import tqdm
from openai import AsyncOpenAI

# ====================== 通用与 IO ======================

def set_seed(seed: int):
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)

def read_jsonl(path: str) -> List[Dict[str, Any]]:
    data = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s: continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict): data.append(obj)
            except Exception:
                pass
    return data

def write_json(items: List[Dict[str, Any]], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ====================== Gold & Pred & Verify ======================

BOXED_LAST  = re.compile(r"\\boxed\\s*\{([^}]*)\}.*$", flags=re.IGNORECASE | re.DOTALL)
THINK_OPEN  = re.compile(r"<think>", flags=re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", flags=re.IGNORECASE)

def extract_gold_from_response_last_answer_line(resp: str) -> str:
    if not resp: return ""
    lines = [ln.strip() for ln in resp.strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        m = re.search(r"the\s+answer\s+is\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if m: return m.group(1).strip()
    matches = list(re.finditer(r"the\s+answer\s+is\s*[:：]\s*(.+)", resp, flags=re.IGNORECASE))
    return matches[-1].group(1).strip() if matches else ""

def extract_pred_from_output_boxed(text: str) -> str:
    if not text: return ""
    m = BOXED_LAST.search(text)
    return m.group(1).strip() if m else ""

# ---- math_verify ----
NUM_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
FRAC_RE = re.compile(r"^[+-]?\d+\s*/\s*[+-]?\d+$")
PCT_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")

def _latex_to_plain(s: str) -> str:
    if not s: return ""
    s = s.replace("\u2212","-").replace("−","-").replace("$","")
    s = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\(left|right|,|;|!|:)", "", s)
    s = s.strip().strip("()[]{}")
    if "=" in s: s = s.split("=")[-1].strip()
    return s

def _to_numeric(s: str):
    t = s.strip()
    if not t: return ("string","")
    if PCT_RE.match(t): return ("percent", float(t[:-1]))
    if FRAC_RE.match(t.replace(" ", "")):
        a,b = t.replace(" ","").split("/")
        try: return ("fraction", (float(a), float(b)))
        except: return ("string", t.lower())
    if NUM_RE.match(t):
        try: return ("number", float(t))
        except: return ("string", t.lower())
    return ("string", t.lower())

def math_verify(gold_raw: str, pred_raw: str, tol: float = 1e-9) -> bool:
    g = _latex_to_plain(gold_raw)
    p = _latex_to_plain(pred_raw)
    kg, vg = _to_numeric(g); kp, vp = _to_numeric(p)
    try:
        if kg=="fraction" and kp=="fraction": return abs(vg[0]/vg[1] - vp[0]/vp[1]) <= tol
        if kg=="fraction" and kp=="number":   return abs(vg[0]/vg[1] - vp) <= tol
        if kg=="number" and kp=="fraction":   return abs(vp[0]/vp[1] - vg) <= tol
        if kg=="number" and kp=="number":     return abs(vg - vp) <= tol
        if kg=="percent" and kp=="percent":   return abs(vg - vp) <= 100*tol
        if kg=="percent" and kp=="number":    return abs(vg/100.0 - vp) <= tol
        if kg=="number" and kp=="percent":    return abs(vg - vp/100.0) <= tol
    except Exception:
        pass
    sg = re.sub(r"\s+|,", "", g).rstrip(".").lower()
    sp = re.sub(r"\s+|,", "", p).rstrip(".").lower()
    return sg == sp

# ====================== special tokens & COT ======================

def _collect_special_ids(tokenizer) -> set:
    ids = set([i for i in getattr(tokenizer, "all_special_ids", []) if isinstance(i, int)])
    for attr in ["pad_token_id","eos_token_id","bos_token_id","unk_token_id"]:
        v = getattr(tokenizer, attr, None)
        if isinstance(v, int): ids.add(v)
    return ids

def count_non_special_tokens(tokenizer, text: str) -> int:
    if not text: return 0
    enc = tokenizer(text, add_special_tokens=False, return_tensors="pt")
    ids = enc["input_ids"][0].tolist()
    sp_ids = _collect_special_ids(tokenizer)
    return sum(1 for t in ids if t not in sp_ids)

def extract_think_inner(text: str) -> str:
    if not text:
        return ""
    low = text.lower()
    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    if m1 and m2 and m1.start() < m2.start():
        return text[m1.end():m2.start()]
    return ""

def has_closed_think_span(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    return bool(m1 and m2 and m1.start() < m2.start())

def _collect_special_strings(tokenizer) -> List[str]:
    special = set()
    for s in getattr(tokenizer, "all_special_tokens", []) or []:
        if isinstance(s, str): special.add(s)
    for s in getattr(tokenizer, "additional_special_tokens", []) or []:
        if isinstance(s, str): special.add(s)
    mp = getattr(tokenizer, "special_tokens_map", {}) or {}
    for v in mp.values():
        if isinstance(v, str): special.add(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str): special.add(x)
    special.update({
        "<|im_end|>", "<|im_start|>", "<|eot_id|>",
        "<|start_header_id|>", "<|end_header_id|>",
        "<|endoftext|>", "<s>", "</s>", "[PAD]", "[UNK]", "[BOS]", "[EOS]"
    })
    return sorted(special, key=len, reverse=True)

def strip_special_tokens_from_text(text: str, tokenizer) -> str:
    if not text: return text
    out = text
    for tok in _collect_special_strings(tokenizer):
        out = out.replace(tok, "")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

# ====================== 数据读取 ======================

def load_records_unified(path: str, dataset_format: str) -> List[Dict[str, Any]]:
    data_raw = read_jsonl(path)
    out = []
    fmt = dataset_format.lower()
    for i, ex in enumerate(data_raw):
        rec = dict(ex)
        if fmt == "tokenskip":
            assert "messages" in rec and isinstance(rec["messages"], list), "tokenskip 缺少 messages"
            assert "answer" in rec, "tokenskip 缺少 answer"
            out.append(rec)
        elif fmt == "ansaug":
            q = (rec.get("query") or rec.get("question") or rec.get("problem") or "").strip()
            resp = rec.get("response", "")
            gold = rec.get("answer")
            if gold is None:
                gold = extract_gold_from_response_last_answer_line(resp)
            msgs = [
                {"role":"user","content": q},
                {"role":"assistant","content": resp}
            ]
            rec2 = {
                "dataset": rec.get("dataset") or rec.get("type") or "MATH_AnsAug",
                "id": rec.get("id") or f"ansaug-{i}",
                "messages": msgs,
                "answer": gold
            }
            rec2.update(rec)
            out.append(rec2)
        else:
            raise ValueError(f"Unknown dataset_format: {dataset_format}")
    return out

# ====================== vLLM 异步请求（/v1/chat/completions） ======================

async def generate_vllm_async(client: AsyncOpenAI,
                              model: str,
                              messages_list: List[List[Dict[str, str]]],
                              max_tokens: int,
                              temperature: float,
                              top_p: float,
                              processes: int,
                              timeout: float) -> List[str]:
    """
    使用 OpenAI 兼容的 /v1/chat/completions，role-based messages；
    并发由 --processes 控制（async 信号量），调用形式与 eval_all_ratios_vllm.py 相同。
    """
    sem = asyncio.Semaphore(processes)
    results: List[Optional[str]] = [None] * len(messages_list)

    async def _one(i: int, messages: List[Dict[str, str]]):
        backoff = 1.0
        for _ in range(5):
            try:
                async with sem:
                    resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=model,
                            messages=messages,
                            temperature=temperature,
                            top_p=top_p,
                            max_tokens=max_tokens,
                        ),
                        timeout=timeout
                    )
                txt = resp.choices[0].message.content if resp.choices else ""
                results[i] = txt or ""
                return
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 8.0)
        results[i] = ""

    tasks = [asyncio.create_task(_one(i, m)) for i, m in enumerate(messages_list)]
    for f in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="vLLM"):
        await f
    return [r or "" for r in results]

# ====================== 5 个基线 prompt & 预算 ======================

BASE_HEAD = "Please reason step by step, and put your final answer within \\boxed{}."

def build_messages_beconcise(query: str) -> List[Dict[str,str]]:
    u = f"{BASE_HEAD}\n{query}\n\nBe concise."
    return [{"role":"system","content":"You are a helpful assistant."},
            {"role":"user","content":u}]

def build_messages_onlynumbers(query: str) -> List[Dict[str,str]]:
    u = f"{BASE_HEAD}\n{query}\n\nOnly use numbers or equations."
    return [{"role":"system","content":"You are a helpful assistant."},
            {"role":"user","content":u}]

def build_messages_abbrewords(query: str) -> List[Dict[str,str]]:
    u = f"{BASE_HEAD}\n{query}\n\nAbbreviate words as much as possible."
    return [{"role":"system","content":"You are a helpful assistant."},
            {"role":"user","content":u}]

def build_messages_lc_prompt(query: str, ratio: float) -> List[Dict[str,str]]:
    reduce_pct = max(0, int(round((1.0 - ratio) * 100)))
    line = f"Please reduce {reduce_pct}% of the words in your Chain-of-Thought process."
    u = f"{BASE_HEAD}\n{query}\n\n{line}"
    return [{"role":"system","content":"You are a helpful assistant."},
            {"role":"user","content":u}]

def build_messages_truncation(query: str) -> List[Dict[str,str]]:
    u = f"{BASE_HEAD}\n{query}"
    return [{"role":"system","content":"You are a helpful assistant."},
            {"role":"user","content":u}]

def decide_max_for_ctrl(base: int, ratio: float, ctrl: str) -> int:
    if ctrl == "truncation":
        return max(1, int(round(base * ratio)))
    return base

CTRL_BUILDERS = {
    "beconcise": lambda q, r: build_messages_beconcise(q),
    "onlynumbers": lambda q, r: build_messages_onlynumbers(q),
    "abbrewords": lambda q, r: build_messages_abbrewords(q),
    "lc-prompt": lambda q, r: build_messages_lc_prompt(q, r),
    "truncation": lambda q, r: build_messages_truncation(q),
}

# ====================== 主流程 ======================

def main():
    ap = argparse.ArgumentParser(description="Run five TokenSkip-paper control baselines in one pass.")
    # 数据
    ap.add_argument("--input_path", required=True)
    ap.add_argument("--dataset_format", choices=["tokenskip","ansaug"], required=True)
    ap.add_argument("--output_dir", required=True)

    # vLLM 接口（OpenAI 兼容）
    ap.add_argument("--vllm_base_url", required=True, help="e.g., http://localhost:8000/v1")
    ap.add_argument("--vllm_api_key", default="EMPTY")
    ap.add_argument("--vllm_model", required=True)

    # 本地 tokenizer（统计与清理）
    ap.add_argument("--tokenizer_path", required=True, help="与 vLLM 服务同款 tokenizer")
    ap.add_argument("--model_type", default="qwen")

    # 控制方法
    ap.add_argument("--ctrls", default="all_paper",
                    help="逗号分隔 {beconcise, onlynumbers, abbrewords, lc-prompt, truncation}；或 all_paper")

    # ratios
    ap.add_argument("--ratios", default="0.6")

    # 解码/并发
    ap.add_argument("--max_new_tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top_p", type=float, default=0.9)
    ap.add_argument("--processes", type=int, default=32, help="并发请求数（async 信号量）")
    ap.add_argument("--timeout", type=float, default=360.0)

    # 其它
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    set_seed(args.seed)

    # ctrls
    if args.ctrls.strip().lower() == "all_paper":
        ctrls = ["beconcise", "onlynumbers", "abbrewords", "lc-prompt", "truncation"]
    else:
        ctrls = [c.strip().lower() for c in args.ctrls.split(",") if c.strip()]
    for c in ctrls:
        if c not in CTRL_BUILDERS:
            raise ValueError(f"Unknown ctrl: {c}")
    print(f"[run] ctrls = {ctrls}")

    # ratios
    ratios = []
    for s in args.ratios.split(","):
        s = s.strip()
        if s: ratios.append(float(s))
    print(f"[run] ratios = {ratios}")

    # 数据
    recs = load_records_unified(args.input_path, args.dataset_format)
    print(f"[data] loaded {len(recs)} records")

    # tokenizer（只用来做 token 统计与清理，不参与推理）
    tok = AutoTokenizer.from_pretrained(args.tokenizer_path, use_fast=True, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
        tok.pad_token_id = tok.eos_token_id

    # vLLM 客户端（OpenAI 兼容）
    client = AsyncOpenAI(api_key=args.vllm_api_key, base_url=args.vllm_base_url.rstrip("/"))

    # 基础 user
    base_users, metas = [], []
    for ex in recs:
        messages = [dict(m) for m in ex["messages"]]
        assert messages[-1]["role"] == "assistant"
        messages[-1]["content"] = ""  # 只保留 user
        user_q = ""
        for m in messages:
            if m["role"] == "user":
                user_q = m["content"]; break
        base_users.append(user_q)
        metas.append({"id": ex.get("id"), "dataset": ex.get("dataset"), "answer": ex.get("answer")})

    # 逐 ctrl & ratio
    for ctrl in ctrls:
        for r in ratios:
            t0 = time.time()

            # 构 messages（保留各自 prompt 变体）
            builder = CTRL_BUILDERS[ctrl]
            messages_list = [builder(q, r) for q in base_users]

            # 决定 max_new_tokens（仅 truncation 生效）
            scaled_max = decide_max_for_ctrl(args.max_new_tokens, r, ctrl)
            print(f"[vllm] ctrl={ctrl} ratio={r:.2f} max_new_tokens={scaled_max} "
                  f"processes={args.processes} N={len(messages_list)}")

            # 推理（/v1/chat/completions）
            texts = asyncio.run(
                generate_vllm_async(
                    client=client, model=args.vllm_model,
                    messages_list=messages_list,
                    max_tokens=scaled_max,
                    temperature=args.temperature, top_p=args.top_p,
                    processes=args.processes, timeout=args.timeout,
                )
            )

            # 评测与清理
            results = []
            for meta, out in zip(metas, texts):
                pred = extract_pred_from_output_boxed(out)
                gold = meta["answer"] or ""
                ok = bool(gold) and bool(pred) and math_verify(gold, pred)

                think_text = extract_think_inner(out)
                cot_len = count_non_special_tokens(tok, think_text)

                cleaned_output = strip_special_tokens_from_text(out, tok)
                closed_think = has_closed_think_span(out)

                results.append({
                    "id": meta["id"],
                    "dataset": meta["dataset"],
                    "prompt": "",
                    "model_output": cleaned_output,
                    "prediction": pred,
                    "answer": gold,
                    "accuracy": ok,
                    "cot_length": cot_len,
                    "has_closed_think": closed_think,
                    "has_pred": bool(pred),
                })

            # 三口径统计
            def _agg(sub: List[Dict[str, Any]]) -> Dict[str, Any]:
                if not sub:
                    return {"n": 0, "accuracy": 0.0, "avg_cot_length": 0.0}
                acc = sum(1 for r0 in sub if r0["accuracy"]) / len(sub)
                avg_cot = sum(r0["cot_length"] for r0 in sub) / len(sub)
                return {"n": len(sub), "accuracy": acc, "avg_cot_length": avg_cot}

            all_set = results
            pred_set = [r0 for r0 in results if r0["has_pred"]]
            pred_think_set = [r0 for r0 in results if r0["has_pred"] and r0["has_closed_think"]]
            mets_all = _agg(all_set); mets_pred = _agg(pred_set); mets_pred_think = _agg(pred_think_set)

            total_time = time.time() - t0

            # 写盘
            out_dir = os.path.join(args.output_dir, ctrl, f"{r:.1f}")
            os.makedirs(out_dir, exist_ok=True)
            write_json(results, os.path.join(out_dir, "prediction.json"))
            mets = {
                "n_samples": len(results),
                "accuracy": mets_all["accuracy"],
                "avg_cot_length": mets_all["avg_cot_length"],
                "breakdown": {
                    "all": mets_all,
                    "with_pred": mets_pred,
                    "with_pred_and_closed_think": mets_pred_think,
                },
                "sample_latency": (total_time / len(results)) if results else 0.0,
                "total_time_sec": total_time,
                "ratio": r,
                "ctrl": ctrl,
                "max_new_tokens_base": args.max_new_tokens,
                "max_new_tokens_used": scaled_max,
            }
            with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
                json.dump(mets, f, ensure_ascii=False, indent=2)

            def _fmt(m): return f"n={m['n']} acc={m['accuracy']*100:.2f}% cot={m['avg_cot_length']:.1f}"
            print(f"[done:ctrl={ctrl} r={r:.2f}] ALL[{_fmt(mets_all)}] | "
                  f"PRED[{_fmt(mets_pred)}] | PRED+THINK[{_fmt(mets_pred_think)}] | "
                  f"latency={(total_time/len(results)) if results else 0.0:.3f}s -> {out_dir}")

if __name__ == "__main__":
    main()
