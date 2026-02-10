#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import sys
from typing import Optional, List

# 尝试使用 tiktoken；没有则回退
try:
    import tiktoken  # type: ignore
except Exception:
    tiktoken = None  # noqa

THINK_RE = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)

# 回退分词：CJK 按单字计；英文按 \w+ 或单个非空白符号计
FALLBACK_TOKEN_RE = re.compile(
    r"[\u4e00-\u9fff]"           # 中日韩统一表意文字
    r"|[\u3040-\u30ff]"          # 日文平/片假名
    r"|[\uac00-\ud7af]"          # 韩文
    r"|\w+"                      # 单词/数字/下划线
    r"|[^\s\w]",                 # 其它非空白符号
    re.UNICODE
)

def get_by_path(obj, path: str):
    """支持点号路径取值：a.b.c 或 a.0.b"""
    cur = obj
    for part in path.split('.'):
        if isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        elif isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None
        else:
            return None
    return cur

def load_tokenizer(pref: Optional[str]) -> Optional[object]:
    """返回 tiktoken 分词器实例；不可用则返回 None"""
    if not tiktoken:
        return None
    # 优先按 encoding 名称加载；失败则按模型名；都失败用 cl100k_base
    if pref:
        try:
            return tiktoken.get_encoding(pref)
        except Exception:
            try:
                return tiktoken.encoding_for_model(pref)
            except Exception:
                pass
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None

def count_tokens(text: str, enc: Optional[object]) -> int:
    if enc:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # 回退：近似计数
    return len(FALLBACK_TOKEN_RE.findall(text))

def compute_avg_tokens(jsonl_path: str, field_path: str, tokenizer_pref: Optional[str], print_each: bool):
    enc = load_tokenizer(tokenizer_pref)
    counts: List[int] = []
    total_lines = 0
    used_lines = 0

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                # 跳过非法 JSON 行
                continue

            content = get_by_path(obj, field_path)
            if not isinstance(content, str):
                continue

            # 找出所有 <think>...</think> 片段，合并计数
            segments = THINK_RE.findall(content)
            if not segments:
                continue

            used_lines += 1
            tokens_this_sample = sum(count_tokens(seg, enc) for seg in segments)
            counts.append(tokens_this_sample)

            if print_each:
                print(f"line={lineno}\ttokens_in_think={tokens_this_sample}")

    if not counts:
        print("未找到任何包含 <think>...</think> 的样本。")
        return 1

    avg_tokens = sum(counts) / len(counts)
    print("—— 统计结果 ——")
    print(f"总行数: {total_lines}")
    print(f"包含 <think> 的样本数: {used_lines}")
    print(f"总 token（仅统计 <think> 块）: {sum(counts)}")
    print(f"平均 token/样本（<think> 内）: {avg_tokens:.2f}")
    return 0

def main():
    parser = argparse.ArgumentParser(
        description="统计 JSONL 文件中 model_output 的 <think>…</think> 区间 token 平均值"
    )
    parser.add_argument("--jsonl", default="/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/outputs/qwen2.5_7b_infer_50k_correct.jsonl", help="输入 JSONL 文件路径")
    parser.add_argument(
        "--field", default="model_output",
        help="要读取的字段路径（支持点号路径），默认 model_output"
    )
    parser.add_argument(
        "--tokenizer", default="/data/tyt/workspace/tyt/Models/Qwen3-8B",
        help="tiktoken 的编码名或模型名（默认 cl100k_base）。若未安装 tiktoken 将自动回退到近似计数。"
    )
    parser.add_argument(
        "--print-each", action="store_true",
        help="逐样本输出 <think> token 计数"
    )
    args = parser.parse_args()

    try:
        exit(compute_avg_tokens(args.jsonl, args.field, args.tokenizer, args.print_each))
    except KeyboardInterrupt:
        print("\n中断。")
        sys.exit(130)

if __name__ == "__main__":
    main()
