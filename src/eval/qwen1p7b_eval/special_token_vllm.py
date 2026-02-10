#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
eval_all_ratios_vllm.py

一次性用 vLLM 跑多个 compression ratio（special 注入）：
  ratios: 0.2, 0.4, 0.6, 0.8, 1.0, 2.0
并把每个 ratio 的结果分别写到:
  <output_dir>/<ratio>/prediction.json
  <output_dir>/<ratio>/metrics.json

规则与实现要点：
- Chat 调用：使用 OpenAI 兼容的 /v1/chat/completions（参考第一份代码的调用细节），
  构造 role-based messages（system+user），模型侧处理具体模板（Qwen/llama3 等）。
- user_text = "Please reason step by step, and put your final answer within \\boxed{}.\n" + query [+ <COMP_XX> 或 <COMP_AUTO>]
- gold：从测试数据 response 最后一处 "The answer is: ..." 抽取。
- pred：从 model_output 的最后一个完整 \\boxed{...} 抽取。
- 匹配：math_verify(gold, pred)（分数/小数/百分/等号右侧等鲁棒比对）。
- COT 长度：只统计 <think>...</think> 的 token 数，且不含 special tokens。
- **输出时仅保留第一个 special token（排除 EOS），其它全部移除**。
- 指标分三类统计：
  ① 只统计有 pred 的样本；
  ② 有 pred 且存在闭合 <think>...</think> 的样本；
  ③ 全部样本。
- 并发控制：用 --processes 控制异步并发数量（取代 --concurrency）。

依赖：
  pip install openai>=1.30.0 transformers tqdm
（vLLM 需以 OpenAI 兼容服务形式运行，比如 base_url=http://localhost:8000/v1）

用法示例：
  python eval_all_ratios_vllm.py \
    --input_path data.jsonl --dataset_format ansaug \
    --output_dir out \
    --vllm_base_url http://localhost:8000/v1 --vllm_api_key EMPTY \
    --vllm_model qwen2.5-7b-instruct \
    --tokenizer_path /path/to/qwen2.5-7b-instruct \
    --model_type qwen \
    --processes 32 --max_new_tokens 512 --len_ctrl none
"""

import os, re, json, time, argparse, random, asyncio
from typing import List, Dict, Any, Optional, Tuple

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

# ====================== Prompt & 注入（Qwen 专用） ======================

def build_user_text_special(query: str, ratio: float, special_prefix: str = "COMP_") -> str:
    head = "Please reason step by step, and put your final answer within \\boxed{}."
    user = f"{head}\n{query}".rstrip()
    r = float(ratio)
    if r <= 1.0:
        tok = f"<{special_prefix}{int(round(r*100))}>"
        user = f"{user} {tok}"
    elif r == 2.0:
        user = f"{user} <COMP_AUTO>"
    return user

def build_messages_qwen(user_text: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user_text}
    ]

# ====================== Gold & Pred & Verify ======================

BOXED_LAST  = re.compile(r"\\boxed\\s*\{([^}]*)\}.*$", flags=re.IGNORECASE | re.DOTALL)
THINK_OPEN  = re.compile(r"<think>", flags=re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", flags=re.IGNORECASE)
SHORT_OPEN  = re.compile(r"<short>", flags=re.IGNORECASE)
SHORT_CLOSE = re.compile(r"</short>", flags=re.IGNORECASE)

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
    m3 = SHORT_OPEN.search(low); m4 = SHORT_CLOSE.search(low)
    if m3 and m4 and m3.start() < m4.start():
        return text[m3.end():m4.start()]
    return ""

def has_closed_think_span(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    m3 = SHORT_OPEN.search(low); m4 = SHORT_CLOSE.search(low)
    return bool((m1 and m2 and m1.start() < m2.start()) or ((m3 and m4 and m3.start() < m4.start())))

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

# ---- 原有：移除所有 special ----

def strip_special_tokens_from_text(text: str, tokenizer) -> str:
    if not text: return text
    out = text
    for tok in _collect_special_strings(tokenizer):
        out = out.replace(tok, "")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

# ---- 新增：仅保留首个（非 EOS）special token ----

def _collect_eos_strings(tokenizer) -> set:
    eos = set()
    tok = getattr(tokenizer, "eos_token", None)
    if isinstance(tok, str) and tok:
        eos.add(tok)
    # 常见 EOS 变体补充
    eos.update({"<|im_end|>", "<|eot_id|>", "</s>", "<|endoftext|>", "[EOS]"})
    # special_tokens_map 里若直接存了字符串也纳入
    mp = getattr(tokenizer, "special_tokens_map", {}) or {}
    for k in ["eos_token"]:
        v = mp.get(k)
        if isinstance(v, str): eos.add(v)
    return eos

def strip_special_tokens_keep_first(text: str, tokenizer, keep_eos: bool = False) -> Tuple[str, Optional[str]]:
    """在文本中仅保留**首个** special token；EOS 默认不保留；其它 special 全删。
    返回：(清洗后的文本, 首个 special 的字符串或 None)
    """
    if not text:
        return text, None
    specials = _collect_special_strings(tokenizer)
    if not specials:
        return text.strip(), None

    eos_set = _collect_eos_strings(tokenizer)
    pattern = re.compile("|".join(re.escape(s) for s in specials))

    out_parts: List[str] = []
    i = 0
    kept: Optional[str] = None
    past_eos = False

    for m in pattern.finditer(text):
        start, end = m.span()
        tok = m.group(0)
        # 先拼接普通片段
        out_parts.append(text[i:start])

        if tok in eos_set:
            past_eos = True
            if keep_eos and kept is None:
                out_parts.append(tok)
                kept = tok
            # 默认不保留 EOS
        else:
            # 仅在 EOS 之前、且尚未保留任何 special 时保留
            if (not past_eos) and kept is None:
                out_parts.append(tok)
                kept = tok
            # 否则丢弃
        i = end

    # 尾部普通文本
    out_parts.append(text[i:])

    out = "".join(out_parts)
    # 二次兜底：删掉任何剩余的 special（但保留 kept）
    for s in specials:
        if kept is not None and s == kept:
            continue
        out = out.replace(s, "")

    # 简单空白规范化
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), kept

# ====================== 数据读取 ======================

def load_records_unified(path: str, dataset_format: str) -> List[Dict[str, Any]]:
    raw = read_jsonl(path)
    out = []
    fmt = dataset_format.lower()
    for i, ex in enumerate(raw):
        rec = dict(ex)
        if fmt == "tokenskip":
            assert "messages" in rec and isinstance(rec["messages"], list), "tokenskip 缺少 messages"
            assert "answer" in rec, "tokenskip 缺少 answer"
            out.append(rec)
        elif fmt == "ansaug":
            q = (rec.get("query") or rec.get("question") or rec.get("problem") or "").strip()
            resp = rec.get("response","")
            if rec.get("answer") is None:
                gold = extract_gold_from_response_last_answer_line(resp)
            else:
                gold = rec.get("answer")
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

# ====================== vLLM 并发推理（/chat/completions） ======================

async def generate_vllm_async(client: AsyncOpenAI,
                              model: str,
                              messages_list: List[List[Dict[str, str]]],
                              max_tokens: int,
                              temperature: float,
                              top_p: float,
                              processes: int,
                              timeout: float) -> List[str]:
    """
    使用 /v1/chat/completions（与第一份代码一致：role-based messages）。
    并发由 processes 控制（async 信号量）。
    - 关键改动：通过 extra_body 传递 skip_special_tokens=False，确保服务端不丢弃 special。
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
                            # vLLM OpenAI 兼容：允许通过 extra_body 传自定义参数
                            extra_body={"skip_special_tokens": False},
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

# ====================== 主流程 ======================

def main():
    ap = argparse.ArgumentParser(description="Evaluate multiple compression ratios with vLLM (special injection).")
    # 数据
    ap.add_argument("--input_path", required=True)
    ap.add_argument("--dataset_format", choices=["tokenskip","ansaug"], required=True)
    ap.add_argument("--output_dir", required=True)

    # vLLM OpenAI 接口
    ap.add_argument("--vllm_base_url", required=True, help="e.g., http://localhost:8000/v1")
    ap.add_argument("--vllm_api_key", default="EMPTY")
    ap.add_argument("--vllm_model", required=True)

    # 模型/分词器（本地只用来做 token 统计与清理）
    ap.add_argument("--tokenizer_path", required=True, help="与 vLLM 服务同款 tokenizer")

    # prompt 形态
    ap.add_argument("--model_type", default="qwen")

    # ratios
    ap.add_argument("--ratios", default="0.2,0.4,0.6,0.8,1.0,2.0")
    ap.add_argument("--special_token_prefix", default="COMP_")

    # 解码/并发
    ap.add_argument("--max_new_tokens", type=int, default=4096)
    ap.add_argument("--len_ctrl", choices=["none","proportional"], default="none",
                    help="是否按 γ 等比缩放 max_new_tokens")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top_p", type=float, default=0.9)
    ap.add_argument("--processes", type=int, default=32, help="并发请求数（async 信号量）")
    ap.add_argument("--timeout", type=float, default=360.0)

    # 其它
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    set_seed(args.seed)

    # 读数据
    recs = load_records_unified(args.input_path, args.dataset_format)
    print(f"[data] loaded {len(recs)} records from {args.input_path} ({args.dataset_format})")

    # 本地 tokenizer（计数/清理用）
    tok = AutoTokenizer.from_pretrained(args.tokenizer_path, use_fast=True, trust_remote_code=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
        tok.pad_token_id = tok.eos_token_id

    # vLLM 客户端
    client = AsyncOpenAI(api_key=args.vllm_api_key, base_url=args.vllm_base_url.rstrip("/"))

    # 解析 ratios
    ratios = []
    for s in args.ratios.split(","):
        s = s.strip()
        if not s: continue
        ratios.append(float(s))
    print(f"[run] ratios = {ratios}")

    # 准备基础 user 问题（评测时清空最后一条 assistant）
    base_users = []
    metas = []
    for ex in recs:
        messages = [dict(m) for m in ex["messages"]]
        assert messages[-1]["role"] == "assistant"
        messages[-1]["content"] = ""  # 只保留 user

        # 提取 user 问题文本
        user_q = ""
        for m in messages:
            if m["role"] == "user":
                user_q = m["content"]
                break

        base_users.append(user_q)
        metas.append({
            "id": ex.get("id"),
            "dataset": ex.get("dataset"),
            "answer": ex.get("answer"),
        })

    # 逐 ratio 推理并落盘
    for r in ratios:
        t0 = time.time()

        # 构 messages（保留 special 注入）
        user_texts = [build_user_text_special(q, r, args.special_token_prefix) for q in base_users]
        messages_list = [build_messages_qwen(u) for u in user_texts]

        scaled_max_new = args.max_new_tokens
        if args.len_ctrl == "proportional" and r < 1.0:
            scaled_max_new = int(round(args.max_new_tokens * r))

        print(f"[vllm] ratio={r:.1f} len_ctrl={args.len_ctrl} max_new_tokens={scaled_max_new} "
              f"processes={args.processes} N={len(messages_list)}")

        # 调 vLLM（/chat/completions）
        texts = asyncio.run(
            generate_vllm_async(
                client=client,
                model=args.vllm_model,
                messages_list=messages_list,
                max_tokens=scaled_max_new,
                temperature=args.temperature,
                top_p=args.top_p,
                processes=args.processes,
                timeout=args.timeout,
            )
        )

        # 汇总与评测
        results = []
        for meta, out in zip(metas, texts):
            pred = extract_pred_from_output_boxed(out)
            gold = meta["answer"] or ""
            ok = bool(gold) and bool(pred) and math_verify(gold, pred)

            think_text = extract_think_inner(out)
            cot_len = count_non_special_tokens(tok, think_text)

            # ✨ 只保留第一个（非 EOS）special token
            cleaned_output, first_special = strip_special_tokens_keep_first(out, tok, keep_eos=False)
            closed_think = has_closed_think_span(out)

            row = {
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
                # 记录本次保留的首个 special token
                "first_special_token": first_special,
            }
            results.append(row)

        # 三种口径统计
        def _agg(sub: List[Dict[str, Any]]) -> Dict[str, Any]:
            if not sub:
                return {"n": 0, "accuracy": 0.0, "avg_cot_length": 0.0}
            acc = sum(1 for r0 in sub if r0["accuracy"]) / len(sub)
            avg_cot = sum(r0["cot_length"] for r0 in sub) / len(sub)
            return {"n": len(sub), "accuracy": acc, "avg_cot_length": avg_cot}

        all_set = results
        pred_set = [r0 for r0 in results if r0["has_pred"]]
        pred_think_set = [r0 for r0 in results if r0["has_pred"] and r0["has_closed_think"]]

        mets_all = _agg(all_set)
        mets_pred = _agg(pred_set)
        mets_pred_think = _agg(pred_think_set)

        total_time = time.time() - t0

        # 写盘
        out_dir = os.path.join(args.output_dir, f"{r:.1f}")
        os.makedirs(out_dir, exist_ok=True)
        write_json(results, os.path.join(out_dir, "prediction.json"))  # 改为 JSON 数组文件
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
            "len_ctrl": args.len_ctrl,
            "max_new_tokens_base": args.max_new_tokens,
            "max_new_tokens_used": scaled_max_new,
            "injection": "special"
        }
        with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(mets, f, ensure_ascii=False, indent=2)

        def _fmt(m): 
            return f"n={m['n']} acc={m['accuracy']*100:.2f}% cot={m['avg_cot_length']:.1f}"
        print(
            f"[done:r={r:.1f}] "
            f"ALL[{_fmt(mets_all)}] | "
            f"PRED[{_fmt(mets_pred)}] | "
            f"PRED+THINK[{_fmt(mets_pred_think)}] | "
            f"latency={(total_time/len(results)) if results else 0.0:.3f}s -> {out_dir}"
        )

if __name__ == "__main__":
    main()
