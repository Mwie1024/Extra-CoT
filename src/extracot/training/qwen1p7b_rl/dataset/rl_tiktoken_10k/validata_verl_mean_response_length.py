# -*- coding: utf-8 -*-
"""
vllm_chat_eval_jsonl.py

- 从 parquet 读入样本，生成 chat messages（按 <COMP_xx>/<COMP_AUTO> 规范）
- 调用 vLLM 的 /v1/chat/completions（开启 logprobs）
- 统计并导出 JSONL：每行一条生成记录（含首 token、长度、片段等）
- 打印长度分布与 <COMP> 首 token 命中率

用法示例：
python vllm_chat_eval_jsonl.py \
  --data /path/to/train.parquet \
  --tokenizer /path/to/tokenizer \
  --model qwen3-1.7b \
  --num_prompts 128 --n 4 \
  --temperature 0.7 --top_p 0.9 \
  --stop "<|im_end|>" "</s>" \
  --seed 43 \
  --out_jsonl vllm_eval_outputs.jsonl
"""
import os
import re
import json
import time
import argparse
from typing import List, Dict, Any, Tuple

import pandas as pd
import requests
from transformers import AutoTokenizer

COMP_RE = re.compile(r"^<COMP_(\d{1,3})>$")


def build_user_text_special(query: str, ratio: float, special_prefix: str = "COMP_") -> str:
    """
    你的控制头拼接函数：在 user 文本末尾加 <COMP_xx> 或 <COMP_AUTO>
    ratio <= 1.0 -> <COMP_xx>；ratio == 2.0 -> <COMP_AUTO>
    """
    head = "Please reason step by step, and put your final answer within \\boxed{}."
    user = f"{head}\n{query}".rstrip()
    r = float(ratio)
    if r <= 1.0:
        tok = f"<{special_prefix}{int(round(r * 100))}>"
        user = f"{user} {tok}"
    elif r == 2.0:
        user = f"{user} <COMP_AUTO>"
    return user


def build_messages_qwen(user_text: str) -> List[Dict[str, str]]:
    """Qwen 风格的 system + user 消息"""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user_text},
    ]


def left_truncate_by_tokens(text: str, tokenizer, max_tokens: int) -> str:
    if not max_tokens or max_tokens <= 0:
        return text
    ids = tokenizer.encode(text, add_special_tokens=False)
    if len(ids) > max_tokens:
        ids = ids[-max_tokens:]
    return tokenizer.decode(ids, skip_special_tokens=False)


def parse_ratio_from_extra(extra: dict) -> float:
    """
    从 extra_info 尽量还原 ratio：
    1) chosen_comp_token（<COMP_40>）
    2) gt_ratio / ratio_bucket
    找不到则默认 2.0（表示 AUTO）
    """
    if not isinstance(extra, dict):
        return 2.0
    tok = extra.get("chosen_comp_token")
    if isinstance(tok, str) and tok.startswith("<COMP_") and tok.endswith(">"):
        try:
            v = int(tok[len("<COMP_") : -1])
            return max(0.0, min(1.0, v / 100.0))
        except Exception:
            pass
    for key in ("gt_ratio", "ratio_bucket"):
        if key in extra:
            try:
                return float(extra[key])
            except Exception:
                pass
    return 2.0


def call_vllm_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: List[Dict[str, str]],
    n: int,
    temperature: float,
    top_p: float,
    max_tokens: int,
    stop: List[str],
    seed: int,
) -> Dict[str, Any]:
    """
    走 /v1/chat/completions；开启 logprobs 以便读取首 token
    """
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "n": n,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stop": stop,
        "stream": False,
        # vLLM chat 模式下：choices[i].logprobs.content = [{token, logprob, ...}, ...]
        "logprobs": 1,
    }
    if seed is not None and seed >= 0:
        payload["seed"] = seed

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=600)
    r.raise_for_status()
    return r.json()


def first_comp_token_from_choice(choice: Dict[str, Any]) -> Tuple[str, bool]:
    """
    从 chat-completions 的 choice 中，读取**首个生成 token**。
    优先用 logprobs.content；若缺失，回退到 message.text 的首词粗判。
    """
    lp = choice.get("logprobs", {})
    # vLLM: logprobs.content = list of tokens
    content = lp.get("content") if isinstance(lp, dict) else None
    if isinstance(content, list) and content:
        for t in content:
            tok = t.get("token", "")
            if tok.strip() == "":  # 跳过纯空白
                continue
            return tok, bool(COMP_RE.fullmatch(tok))

    # 回退：从 message.content 的开头粗看
    text = choice.get("message", {}).get("content", "") or choice.get("text", "")
    head = text.strip().split()[:1]
    tok = head[0] if head else ""
    return tok, bool(COMP_RE.fullmatch(tok))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="parquet 文件路径")
    ap.add_argument("--prompt_key", default="prompt", help="列名：聊天消息（list[dict]）")
    ap.add_argument("--question_key", default="question", help="列名：纯文本问题")
    ap.add_argument("--extra_key", default="extra_info", help="列名：包含 gt_ratio/ratio_bucket/chosen_comp_token")
    ap.add_argument("--num_prompts", type=int, default=128)
    ap.add_argument("--n", type=int, default=4, help="每个 prompt 生成条数")
    ap.add_argument("--base_url", default="http://localhost:8000")
    ap.add_argument("--api_key", default=os.environ.get("VLLM_API_KEY", "EMPTY"))
    ap.add_argument("--model", default="default")
    ap.add_argument("--tokenizer", required=True, help="HF tokenizer 路径（需与服务端模型一致）")
    ap.add_argument("--max_user_tokens", type=int, default=0, help="对 user 文本进行左截断的 token 上限；0 表示不截断")
    ap.add_argument("--max_tokens", type=int, default=4096)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top_p", type=float, default=0.9)
    ap.add_argument("--stop", type=str, nargs="*", default=["<|im_end|>", "</s>"])
    ap.add_argument("--seed", type=int, default=43, help="-1 表示随机；>=0 固定种子")
    ap.add_argument("--out_jsonl", default="vllm_eval_outputs.jsonl")
    args = ap.parse_args()

    print(f"[INFO] loading data: {args.data}")
    df = pd.read_parquet(args.data).head(args.num_prompts).copy()

    print(f"[INFO] loading tokenizer: {args.tokenizer}")
    tok = AutoTokenizer.from_pretrained(args.tokenizer, use_fast=True)

    out_lines: List[str] = []
    t0 = time.time()

    for idx, row in df.iterrows():
        # ----------------- 构造 messages -----------------
        messages = None
        raw = row.get(args.prompt_key)

        if isinstance(raw, list) and all(isinstance(m, dict) and "role" in m and "content" in m for m in raw):
            # 已是聊天消息；如果最后 user 未携带 <COMP_..>/<COMP_AUTO>，用 question+ratio 重新构造
            last_content = raw[-1].get("content", "") if raw else ""
            if ("<COMP_" in last_content) or ("<COMP_AUTO>" in last_content):
                messages = raw
            else:
                extra = row.get(args.extra_key) or {}
                ratio = parse_ratio_from_extra(extra)
                query = row.get(args.question_key, "")
                user_text = build_user_text_special(query, ratio)
                if args.max_user_tokens > 0:
                    user_text = left_truncate_by_tokens(user_text, tok, args.max_user_tokens)
                messages = build_messages_qwen(user_text)
        else:
            # 不是消息列表：从 question + ratio 生成
            extra = row.get(args.extra_key) or {}
            ratio = parse_ratio_from_extra(extra)
            query = row.get(args.question_key)
            if not isinstance(query, str) or not query.strip():
                query = str(raw)  # 退而求其次
            user_text = build_user_text_special(query, ratio)
            if args.max_user_tokens > 0:
                user_text = left_truncate_by_tokens(user_text, tok, args.max_user_tokens)
            messages = build_messages_qwen(user_text)

        # ----------------- 调 vLLM Chat -----------------
        resp = call_vllm_chat(
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
            messages=messages,
            n=args.n,
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
            stop=args.stop,
            seed=args.seed,
        )

        choices = resp.get("choices", [])
        for k, ch in enumerate(choices):
            content = ch.get("message", {}).get("content", "") or ""
            tok0, is_comp = first_comp_token_from_choice(ch)

            # 统计长度（按生成文本）
            gen_len_tok = len(tok.encode(content, add_special_tokens=False))
            gen_len_char = len(content)

            out = {
                "prompt_idx": int(idx),
                "choice_idx": int(k),
                "messages": messages,  # 记录实际喂入（便于回溯）
                "resp_head": content[:120].replace("\n", "\\n"),
                "resp_tail": content[-200:].replace("\n", "\\n"),
                "len_tokens": int(gen_len_tok),
                "len_chars": int(gen_len_char),
                "finish_reason": ch.get("finish_reason", "NA"),
                "first_token_raw": tok0,
                "first_is_comp": int(is_comp),
            }
            out_lines.append(json.dumps(out, ensure_ascii=False))

        if ((len(out_lines) // max(args.n, 1)) % 16) == 0:
            done_prompts = (len(out_lines) // max(args.n, 1))
            print(f"[INFO] done {done_prompts}/{len(df)} prompts...")

    # ----------------- 写 JSONL -----------------
    with open(args.out_jsonl, "w", encoding="utf-8") as f:
        for line in out_lines:
            f.write(line + "\n")

    dt = time.time() - t0
    print(f"[INFO] saved {len(out_lines)} lines to {args.out_jsonl}; time={dt:.1f}s")

    # ----------------- 打印统计 -----------------
    try:
        recs = [json.loads(x) for x in out_lines]
        stat = pd.DataFrame(recs)

        mean_over_all = stat["len_tokens"].mean()
        p50 = stat["len_tokens"].quantile(0.50)
        p90 = stat["len_tokens"].quantile(0.90)
        p95 = stat["len_tokens"].quantile(0.95)
        print("\n=== Metric A: 平均长度（按所有生成条目）===")
        print(f"mean_tokens={mean_over_all:.1f}  p50={p50:.1f}  p90={p90:.1f}  p95={p95:.1f}")

        if "finish_reason" in stat:
            print(stat["finish_reason"].value_counts(dropna=False))

        # 按 prompt 先平均再整体平均
        per_prompt = stat.groupby("prompt_idx")["len_tokens"].mean()
        mean_grouped = per_prompt.mean()
        print("\n=== Metric B: 先组内平均（n），再对 N 组求均值 ===")
        print(f"grouped_mean_tokens={mean_grouped:.1f}")

        # 首 token 命中率
        ratio_comp = stat["first_is_comp"].mean()
        print("\n=== 首 token 命中率（是否 <COMP_xx>）===")
        print(f"first token is <COMP_xx>: {ratio_comp * 100:.2f}%")

        # 最常见的首 token
        if "first_token_raw" in stat:
            print("\n=== 首 token Top-10 ===")
            print(stat["first_token_raw"].value_counts().head(10))

    except Exception as e:
        print(f"[WARN] 统计阶段出错：{e}")


if __name__ == "__main__":
    main()
