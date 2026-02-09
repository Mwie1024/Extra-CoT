#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
eval_one_ratio_unified.py

一次只评一个压缩比 γ，兼容两种数据格式：
1) TokenSkip/GSM8K 格式：
   {"dataset": "...", "id": "...",
    "messages":[{"role":"user","content":"..."},{"role":"assistant","content":"..."}],
    "answer":"..."}
2) MATH_AnsAug 格式（你的数据）：
   {"query":"...", "response":"...", "original_question":"...","type":"..."}
   -> 从 response 的最后一句抽取 GT 答案

功能要点：
- γ 注入方式：
  * tokenskip：对齐 TokenSkip（Qwen: 在 user 段尾加 <|eot_id|>γ<|eot_id|>；Llama3: 在 user 段尾加一行 γ）
  * kv：更稳妥的“键值”提示（compression_ratio: 0.7）
  * special：单token（如 <COMP_70>）
- 可选：按 γ 等比缩放 max_new_tokens（--len_ctrl proportional）以对齐 TokenSkip “一比率一上限”的评测套路
- COT 长度统计：优先 <think>…</think>，其次 \boxed{ 之前，再退化为整段
"""

import os, re, json, time, argparse, random
from typing import List, Dict, Any, Optional

import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel
from tqdm import tqdm

# ====================== 通用小工具 ======================

def set_seed(seed: int):
    random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
    if torch.cuda.is_available(): torch.cuda.manual_seed_all(seed)

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

def write_jsonl(items: List[Dict[str, Any]], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for x in items:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

# ====================== γ 注入 & Prompt ======================

def build_user_text(query: str,
                    ratio: float,
                    model_type: str,
                    ratio_injection: str = "tokenskip",
                    special_prefix: str = "COMP_") -> str:
    """
    ratio_injection:
      - 'tokenskip'：对齐 TokenSkip（Qwen: <|eot_id|>γ<|eot_id|>；Llama3: 在末尾追加一行 γ）
      - 'kv'       ：明文键值（compression_ratio: 0.7），在 Qwen 上更稳
      - 'special'  ：单token，如 <COMP_70>
    """
    head = "Please reason step by step, and put your final answer within \\boxed{}."
    user = f"{head}\n{query}".rstrip()
    r = float(ratio)
    mt = model_type.lower()
    enc = ratio_injection.lower()

    if enc == "none":
        return user

    if enc == "tokenskip" and r < 1.0:
        if mt == "qwen":
            user = f"{user}<|eot_id|>{r:.1f}<|eot_id|>"
        else:  # llama3
            user = f"{user}\n{r:.1f}"
    elif enc == "kv" and r < 1.0:
        user = f"{user}\ncompression_ratio: {r:.1f}"
    elif enc == "special" and r <= 1.0:
        tok = f"<{special_prefix}{int(round(r*100))}>"
        user = f"{user} {tok}"
    elif enc == "special" and r == 2.0:
        tok = "<COMP_AUTO>"
        user = f"{user} {tok}"
    return user

def build_prompt(user_text: str, model_type: str) -> str:
    mt = model_type.lower()
    if mt == "qwen":
        return (
            "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
            f"<|im_start|>user\n{user_text}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
    elif mt == "llama3":
        return (
            "<|start_header_id|>system<|end_header_id|>\n\nYou are a helpful assistant."
            "<|eot_id|>"
            "<|start_header_id|>user<|end_header_id|>\n\n"
            f"{user_text}"
            "<|eot_id|>"
            "<|start_header_id|>assistant<|end_header_id|>\n\n"
        )
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

# ====================== 模型加载 ======================

def maybe_add_specials(tokenizer, tokens: List[str]):
    missing = []
    for t in tokens:
        tid = tokenizer.convert_tokens_to_ids(t)
        if tid is None or (getattr(tokenizer, "unk_token_id", None) is not None and tid == tokenizer.unk_token_id):
            missing.append(t)
    if missing:
        tokenizer.add_special_tokens({"additional_special_tokens": missing})
        print(f"[tokenizer] added {len(missing)} special tokens: {missing}")

def load_model_tokenizer(model_path: str,
                         tokenizer_path: Optional[str],
                         adapter_path: Optional[str],
                         model_type: str,
                         ratio_injection: str,
                         ratio: float,
                         special_prefix: str,
                         dtype: str = "fp16",
                         device_map: str = "auto"):
    tok = AutoTokenizer.from_pretrained(tokenizer_path or model_path, use_fast=True, trust_remote_code=True)
    if ratio_injection == "special" and ratio <= 1.0:
        maybe_add_specials(tok, [f"<{special_prefix}{int(round(ratio*100))}>"])
    
    if ratio_injection == "special" and ratio == 2.0:
        maybe_add_specials(tok, ["<COMP_AUTO>"])

    torch_dtype = {"auto":"auto","bf16":torch.bfloat16,"fp16":torch.float16,"fp32":torch.float32}[dtype.lower()]
    model = AutoModelForCausalLM.from_pretrained(
        model_path, trust_remote_code=True, torch_dtype=torch_dtype,
        device_map=(device_map if device_map != "none" else None),
    )
    if adapter_path and adapter_path.lower() not in ["","none"]:
        model = PeftModel.from_pretrained(model, adapter_path, device_map="auto")
        model = model.merge_and_unload()

    tok.padding_side = "left"
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
        tok.pad_token_id = tok.eos_token_id

    return model.eval(), tok

# ====================== 抽取 COT 段 & 答案对齐 ======================

BOXED_ANY   = re.compile(r"\\boxed\s*\{([^}]*)\}", flags=re.IGNORECASE | re.DOTALL)
BOXED_LAST  = re.compile(r"\\boxed\s*\{([^}]*)\}.*$", flags=re.IGNORECASE | re.DOTALL)
THINK_OPEN  = re.compile(r"<think>", flags=re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", flags=re.IGNORECASE)

def extract_cot_segment(text: str) -> str:
    """优先 <think>…</think>；其次 \boxed{ 之前；再 'The final answer is' 之前；最后整段。"""
    if not text: return ""
    low = text.lower()

    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    if m1 and m2 and m1.start() < m2.start():  # <think>..</think>
        return text[m1.end():m2.start()]

    m = BOXED_ANY.search(text)                 # \boxed{...} 之前
    if m:
        return text[:m.start()]

    k = low.find("the final answer is")        # 'The final answer is' 之前
    if k != -1:
        return text[:k]

    return text

NUM_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
FRAC_RE = re.compile(r"^[+-]?\d+\s*/\s*[+-]?\d+$")
PCT_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")

def _latex_to_plain(s: str) -> str:
    if not s: return ""
    s = s.replace("\u2212","-").replace("−","-").replace("$","")
    s = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\(left|right|,|;|!|:)", "", s)
    s = s.strip().strip("()[]{}")
    # 若含等号，取等号右侧（常见 "x=6"）
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

def answers_equal(gold_raw: str, pred_raw: str, tol: float = 1e-9) -> bool:
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

def extract_pred_from_output(text: str) -> str:
    """预测答案从模型输出抽取：优先最后一个 \\boxed{...}；否则取最后出现的数字/分数/百分数/最后一行。"""
    if not text: return ""
    m = BOXED_LAST.search(text)
    if m: return m.group(1).strip()

    # 没有 boxed：从末尾向前找“看起来像答案”的东西
    # 先按行取最后非空行，再从中抽取数字/分数/百分
    last_line = ""
    for line in reversed(text.splitlines()):
        t = line.strip()
        if t:
            last_line = t
            break
    if not last_line:
        last_line = text.strip()

    # 常见 "The answer is: 6"
    ans_like = re.split(r"[:：]\s*", last_line)[-1].strip()

    # 优先提取分数 / 数字 / 百分
    mfrac = re.findall(r"[+-]?\d+\s*/\s*[+-]?\d+", ans_like)
    if mfrac: return mfrac[-1].strip()
    mnum  = re.findall(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?", ans_like)
    if mnum: return mnum[-1].strip()

    return ans_like

def extract_gold_from_response_last_sentence(resp: str) -> str:
    """
    你的数据：GT 从 response 的最后一句抽取。
    规则：若含 \\boxed{...} → 用最后一个 boxed；
         否则取最后一句/最后一行，抽取分数/数字/百分，若没有就原样返回。
    """
    if not resp: return ""
    # 若任何位置有 boxed，直接用最后一个 boxed 作为 GT（更稳）
    m = BOXED_LAST.search(resp)
    if m:
        return m.group(1).strip()

    # 分句（中英标点+换行）
    sents = re.split(r"(?<=[\.\?!。！？])\s+|\n+", resp.strip())
    tail = ""
    for s in reversed(sents):
        t = s.strip()
        if t:
            tail = t
            break
    if not tail: tail = resp.strip()

    # "The answer is: 6" 场景
    cand = re.split(r"[:：]\s*", tail)[-1].strip()
    # 优先分数
    mfrac = re.findall(r"[+-]?\d+\s*/\s*[+-]?\d+", cand)
    if mfrac: return mfrac[-1].strip()
    # 再数字/百分
    mnum  = re.findall(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?", cand)
    if mnum: return mnum[-1].strip()
    return cand

# ====================== 生成 ======================

@torch.no_grad()
def generate_batch(model, tokenizer, prompts: List[str],
                   max_new_tokens: int,
                   temperature: float = 0.0,
                   top_p: float = 1.0) -> List[str]:
    inputs = tokenizer(prompts, padding=True, truncation=True, return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}
    gen = model.generate(
        **inputs,
        max_new_tokens=max_new_tokens,
        do_sample=(temperature > 0.0),
        temperature=temperature,
        top_p=top_p,
        eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.pad_token_id,
        use_cache=True,
    )
    # outs = tokenizer.batch_decode(gen, skip_special_tokens=True)
    # srcs = tokenizer.batch_decode(inputs["input_ids"], skip_special_tokens=True)
    outs = tokenizer.batch_decode(gen, skip_special_tokens=False)
    srcs = tokenizer.batch_decode(inputs["input_ids"], skip_special_tokens=False)
    res = []
    for p, o in zip(srcs, outs):
        res.append(o[len(p):] if o.startswith(p) else o)
    return res

# ====================== 数据读取（两种格式） ======================

def load_records_unified(path: str, dataset_format: str) -> List[Dict[str, Any]]:
    """
    输出统一结构：每条含
      - messages: [{'role':'user','content':...}, {'role':'assistant','content':...}]  (assistant 内容后续会清空)
      - answer: gold string
      - id / dataset / 其它原字段原样带上
    """
    raw = read_jsonl(path)
    out = []
    fmt = dataset_format.lower()
    for i, ex in enumerate(raw):
        rec = dict(ex)
        if fmt == "tokenskip":
            # 直接复用，确保存在 answer
            assert "messages" in rec and isinstance(rec["messages"], list), "tokenskip 格式缺少 messages"
            assert "answer" in rec, "tokenskip 格式缺少 answer"
            out.append(rec)
        elif fmt == "ansaug":
            # 你的数据：从 response 的最后一句抽 GT
            q = (rec.get("query") or rec.get("original_question") or "").strip()
            resp = rec.get("response","")
            gold = extract_gold_from_response_last_sentence(resp)
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
            # 原样保留原字段，便于追溯
            rec2.update(rec)
            out.append(rec2)
        else:
            raise ValueError(f"Unknown dataset_format: {dataset_format}")
    return out

# ====================== 主流程 ======================

def main():
    ap = argparse.ArgumentParser(description="Evaluate ONE compression ratio; support TokenSkip & MATH_AnsAug inputs.")
    # 数据
    ap.add_argument("--input_path", required=True, help="测试集 jsonl（TokenSkip 格式或你的 AnsAug 格式）")
    ap.add_argument("--dataset_format", choices=["tokenskip","ansaug"], required=True,
                    help="输入数据格式：tokenskip 或 ansaug（你的）")
    ap.add_argument("--output_dir", required=True, help="输出目录")

    # 模型
    ap.add_argument("--model_path", required=True)
    ap.add_argument("--tokenizer_path", default=None)
    ap.add_argument("--adapter_path", default=None)
    ap.add_argument("--model_type", choices=["qwen","llama3"], default="qwen")

    # 压缩比（本脚本一次只评一个）
    ap.add_argument("--compression_ratio", type=float, required=True)
    ap.add_argument("--ratio_injection", choices=["tokenskip","kv","special", "none"], default="tokenskip")
    ap.add_argument("--special_token_prefix", default="COMP_")

    # 解码
    ap.add_argument("--max_new_tokens", type=int, default=512)
    ap.add_argument("--len_ctrl", choices=["none","proportional"], default="none",
                    help="是否按 γ 等比缩放 max_new_tokens（对齐 TokenSkip 的 math 评测）")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--top_p", type=float, default=0.1)
    ap.add_argument("--batch_size", type=int, default=16)

    # 其它
    ap.add_argument("--dtype", choices=["auto","bf16","fp16","fp32"], default="fp16")
    ap.add_argument("--device_map", default="auto")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    set_seed(args.seed)

    # 读数据 & 适配为统一结构
    recs = load_records_unified(args.input_path, args.dataset_format)
    print(f"[data] loaded {len(recs)} records from {args.input_path} ({args.dataset_format})")

    # 构 prompts（按 TokenSkip 习惯，评测时把最后一个 assistant 置空）
    prompts = []
    metas   = []
    for ex in recs:
        messages = []
        for m in ex["messages"]:
            mm = dict(m)
            messages.append(mm)
        # 评测时清空 assistant（即只保留 user）：
        assert messages[-1]["role"] == "assistant"
        messages[-1]["content"] = ""

        # 提取 user 问题文本
        user_q = ""
        for m in messages:
            if m["role"] == "user":
                user_q = m["content"]
                break

        user_text = build_user_text(
            user_q, args.compression_ratio, args.model_type,
            ratio_injection=args.ratio_injection, special_prefix=args.special_token_prefix
        )
        prompt = build_prompt(user_text, args.model_type)
        # breakpoint()

        prompts.append(prompt)
        metas.append({
            "id": ex.get("id"),
            "dataset": ex.get("dataset"),
            "answer": ex.get("answer"),
            "original_record": ex
        })

    # 加载模型与 tokenizer
    model, tok = load_model_tokenizer(
        model_path=args.model_path,
        tokenizer_path=args.tokenizer_path,
        adapter_path=args.adapter_path,
        model_type=args.model_type,
        ratio_injection=args.ratio_injection,
        ratio=args.compression_ratio,
        special_prefix=args.special_token_prefix,
        dtype=args.dtype,
        device_map=args.device_map,
    )

    # 是否按 γ 等比缩放 max_new_tokens（对齐 TokenSkip 风格）
    scaled_max_new = args.max_new_tokens
    if args.len_ctrl == "proportional" and args.compression_ratio < 1.0:
        scaled_max_new = int(round(args.max_new_tokens * args.compression_ratio))
    print(f"[run] ratio={args.compression_ratio:.1f}  len_ctrl={args.len_ctrl}  "
          f"max_new_tokens={scaled_max_new}  batch_size={args.batch_size}")

    # 生成
    t0 = time.time()
    outs = []
    for i in tqdm(range(0, len(prompts), args.batch_size), desc=f"r={args.compression_ratio:.1f}"):
        batch_prompts = prompts[i:i+args.batch_size]
        texts = generate_batch(model, tok, batch_prompts,
                               max_new_tokens=scaled_max_new,
                               temperature=args.temperature, top_p=args.top_p)
        outs.extend(texts)
    total_time = time.time() - t0

    # 汇总
    results = []
    acc = cot_sum = 0
    for meta, out in zip(metas, outs):
        pred = extract_pred_from_output(out)
        gold = meta["answer"] or ""
        ok   = bool(gold) and bool(pred) and answers_equal(gold, pred)

        cot_text = extract_cot_segment(out)
        cot_len  = tok(cot_text, add_special_tokens=False, return_tensors="pt")["input_ids"].shape[1]

        row = {
            "id": meta["id"],
            "dataset": meta["dataset"],
            "prompt": "",  # 如需回溯可保存 prompt；默认不落盘避免体积过大
            "model_output": out,
            "prediction": pred,
            "answer": gold,
            "accuracy": ok,
            "cot_length": cot_len
        }
        # 附上原始记录方便溯源（可按需移除）
        row["__orig__"] = meta["original_record"]
        results.append(row)
        acc += int(ok); cot_sum += cot_len

    # 存盘
    out_dir = os.path.join(args.output_dir, f"{args.compression_ratio:.1f}")
    os.makedirs(out_dir, exist_ok=True)
    write_jsonl(results, os.path.join(out_dir, "predictions.jsonl"))

    mets = {
        "n_samples": len(results),
        "accuracy": (acc / len(results)) if results else 0.0,
        "avg_cot_length": (cot_sum / len(results)) if results else 0.0,
        "sample_latency": (total_time / len(results)) if results else 0.0,
        "total_time_sec": total_time,
        "ratio": args.compression_ratio,
        "len_ctrl": args.len_ctrl,
        "max_new_tokens_base": args.max_new_tokens,
        "max_new_tokens_used": scaled_max_new,
    }
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(mets, f, ensure_ascii=False, indent=2)

    print(f"[done] n={mets['n_samples']}  acc={mets['accuracy']*100:.2f}%  "
          f"avg_cot_len={mets['avg_cot_length']:.1f}  latency={mets['sample_latency']:.3f}s  -> {out_dir}")

if __name__ == "__main__":
    main()
