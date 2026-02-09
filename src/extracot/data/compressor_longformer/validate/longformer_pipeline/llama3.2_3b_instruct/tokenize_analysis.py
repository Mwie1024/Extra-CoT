#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
统计一个 JSON/JSONL 数据集中每条样本的 `compressed_cot` 与 `cot` 字段的 token 数比值（使用 tiktoken）。
默认使用 cl100k_base 编码（适用于 GPT-3.5/4 系列），可通过 --encoding 或 --model 调整。
计算所有样本比值的平均值，用于分析压缩效果。

用法示例：
python tokenize_analysis.py \
  --path /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/longformer_pipeline/qwen2.5-7b-metamath-50k-correct/train_outputs_compressed_ratio_0.2.jsonl \
  --encoding cl100k_base \
  --to-csv compression_ratios.csv
"""

import argparse
import json
from pathlib import Path
from typing import List, Dict, Any, Tuple
import statistics
import csv

import tiktoken


def load_samples(path: Path) -> List[Dict[str, Any]]:
    """尽量鲁棒地读取 JSON / JSONL。返回样本字典列表。"""
    text = path.read_text(encoding="utf-8")

    # 尝试按 JSON 一次性解析
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 常见几种容器字段处理
            for key in ("data", "train", "samples", "items"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            # dict-of-dicts 形式
            if all(isinstance(v, dict) for v in data.values()):
                return list(data.values())
        # 走到这里说明结构不符合预期，回退到 JSONL
    except Exception:
        pass

    # 回退：逐行按 JSONL 解析
    samples: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                samples.append(obj)
        except Exception:
            # 跳过坏行
            continue
    if not samples:
        raise ValueError("无法解析文件为有效的 JSON/JSONL 结构。")
    return samples


def get_encoding(encoding_name: str = None, model: str = None):
    """优先使用 model 映射，否则使用指定的 encoding_name。"""
    if model:
        try:
            return tiktoken.encoding_for_model(model)
        except Exception:
            print(f"[WARN] 无法根据模型 '{model}' 获取编码，改用 encoding 名称。")
    if not encoding_name:
        encoding_name = "cl100k_base"
    return tiktoken.get_encoding(encoding_name)


def as_text(value: Any) -> str:
    """将任意类型安全转为字符串（保证可计数）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # 非字符串时序列化为紧凑 JSON 字符串，避免报错
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def count_compression_ratio_per_sample(
    samples: List[Dict[str, Any]],
    enc,
) -> List[Tuple[int, int, int, float]]:
    """返回 [(index, cot_tokens, compressed_cot_tokens, ratio), ...]"""
    results: List[Tuple[int, int, int, float]] = []
    for i, s in enumerate(samples):
        cot_text = as_text(s.get("cot", ""))
        compressed_cot_text = as_text(s.get("compressed_cot", ""))
        
        # disallowed_special=() 以避免特殊符号报错
        cot_tokens = len(enc.encode(cot_text, disallowed_special=()))
        compressed_cot_tokens = len(enc.encode(compressed_cot_text, disallowed_special=()))
        
        # 计算压缩比值，避免除零错误
        ratio = compressed_cot_tokens / cot_tokens if cot_tokens > 0 else 0.0
        
        results.append((i, cot_tokens, compressed_cot_tokens, ratio))
    return results


def summarize_compression_ratios(ratios: List[float]) -> str:
    """分析压缩比值的统计信息"""
    if not ratios:
        return "无可用样本。"
    
    mean_ratio = sum(ratios) / len(ratios)
    med_ratio = statistics.median(ratios)
    p95_ratio = statistics.quantiles(ratios, n=100)[94] if len(ratios) >= 20 else max(ratios)
    
    return (
        f"样本数: {len(ratios)}\n"
        f"平均压缩比值: {mean_ratio:.4f}\n"
        f"中位数比值: {med_ratio:.4f}，P95比值: {p95_ratio:.4f}\n"
        f"最小比值: {min(ratios):.4f}，最大比值: {max(ratios):.4f}"
    )


def main():
    parser = argparse.ArgumentParser(description="统计 JSON/JSONL 中 compressed_cot 与 cot 的 token 数比值")
    parser.add_argument(
        "--path",
        type=Path,
        required=False,
        default=Path("/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/longformer_pipeline/qwen2.5-7b-metamath-50k-correct/train_outputs_compressed_ratio_0.8.jsonl"),
        help="数据文件路径（.json 或 .jsonl）",
    )
    parser.add_argument(
        "--encoding",
        type=str,
        default="cl100k_base",
        help="tiktoken 编码名（如 cl100k_base / o200k_base 等）",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="（可选）根据模型名自动选择编码，如 gpt-4o-mini；优先于 --encoding",
    )
    parser.add_argument(
        "--to-csv",
        type=Path,
        default=None,
        help="（可选）将结果导出为 CSV（四列：index,cot_tokens,compressed_cot_tokens,compression_ratio）",
    )
    args = parser.parse_args()

    samples = load_samples(args.path)
    enc = get_encoding(encoding_name=args.encoding, model=args.model)

    results = count_compression_ratio_per_sample(samples, enc)
    ratios = [ratio for _, _, _, ratio in results]

    print("\n==== 压缩比值统计 ====")
    print(summarize_compression_ratios(ratios))

    # 可选导出
    if args.to_csv:
        with args.to_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["index", "cot_tokens", "compressed_cot_tokens", "compression_ratio"])
            writer.writerows(results)
        print(f"\n已写入 CSV：{args.to_csv}")


if __name__ == "__main__":
    main()
