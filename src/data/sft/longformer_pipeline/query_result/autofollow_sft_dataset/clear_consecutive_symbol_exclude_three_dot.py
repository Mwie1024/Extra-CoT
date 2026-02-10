#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_think_punct.py
清理 JSON 文件中 output 的 <think>...</think> 区域：
- 统计 由 '.' 与 ',' 组成、长度≥2 的“连续串”（允许符号之间夹任意空白），按类别计数：dot_only / comma_only / mixed
- 将这些连续串替换为单个 '.'
- **但若恰好为 '...'（连续无空格的三个英文句点），则不清除、原样保留**

兼容输入：JSON 数组、单对象、JSON Lines (jsonl)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

# 仅在 <think>...</think> 内处理（标签大小写不敏感）
THINK_BLOCK_RE = re.compile(r"(<think>)(.*?)(</think>)", re.DOTALL | re.IGNORECASE)

# 允许两个及以上的 '.' / ',' 组成“连续串”，符号之间可有任意空白（空格/制表符/换行等）
# 示例能匹配：'..'、',,'、'.,'、'. ,'、', . ,,'、'.  ,  .'、'...' 等
RUN_RE = re.compile(r"[.,](?:\s*[.,]){1,}")

def categorize(seq: str) -> str:
    """按类别统计：去掉空白后只含 '.' 记为 dot_only，只含 ',' 记为 comma_only，二者混合记为 mixed"""
    seq = re.sub(r"\s+", "", seq)
    s = set(seq)
    if s == {"."}:
        return "dot_only"
    if s == {","}:
        return "comma_only"
    return "mixed"

def clean_think_regions(text: str, counters: Dict[str, int]) -> Tuple[str, bool, bool]:
    """
    仅在 <think>...</think> 内：
    - 先统计 RUN_RE 匹配到的每个连续串
    - 再把这些连续串替换为单个 '.'，**但恰好为 '...'（连续无空格的三个点）不替换**
    返回 (清洗后的文本, 是否含<think>, 是否发生修改)
    """
    if not isinstance(text, str):
        return text, False, False

    had_think = bool(THINK_BLOCK_RE.search(text))
    changed = False

    def _repl(m: re.Match) -> str:
        nonlocal changed
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)

        # 统计（基于替换前的 body）
        matches = list(RUN_RE.finditer(body))
        for mm in matches:
            counters["total_sequences"] += 1
            counters[categorize(mm.group(0))] += 1

        # 替换规则：若匹配片段恰为 "..."（无空格的三个点），则保留；否则替换为 "."
        def _run_repl(x: re.Match) -> str:
            s = x.group(0)
            return s if s == "..." else "."

        new_body = RUN_RE.sub(_run_repl, body)
        if new_body != body:
            changed = True

        return f"{open_tag}{new_body}{close_tag}"

    new_text = THINK_BLOCK_RE.sub(_repl, text)
    return new_text, had_think, changed

def load_any_json(path: Path) -> Tuple[Union[List[dict], dict], str]:
    """尝试读取为 JSON（数组/对象）；失败则按 JSONL 读取为列表"""
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data, "array"
        elif isinstance(data, dict):
            return data, "object"
        else:
            return data, "object"
    except json.JSONDecodeError:
        # JSON Lines
        items = []
        for ln, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"跳过第 {ln} 行（无法解析为 JSON）：{e}", file=sys.stderr)
        return items, "jsonl"

def dump_any_json(data: Union[List[dict], dict], fmt: str, out_path: Path):
    with out_path.open("w", encoding="utf-8") as f:
        if fmt == "jsonl":
            for obj in data:  # type: ignore
                f.write(json.dumps(obj, ensure_ascii=False))
                f.write("\n")
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)

def process_records(
    data: Union[List[dict], dict], fmt: str
) -> Tuple[Union[List[dict], dict], Dict[str, int]]:
    stats: Dict[str, int] = {
        "total_outputs": 0,
        "outputs_with_think": 0,
        "outputs_modified": 0,
        "total_sequences": 0,
        "dot_only": 0,
        "comma_only": 0,
        "mixed": 0,
    }

    def _handle_obj(obj: dict):
        if not isinstance(obj, dict):
            return
        if "output" in obj:
            stats["total_outputs"] += 1
            new_text, had_think, changed = clean_think_regions(obj["output"], stats)
            if had_think:
                stats["outputs_with_think"] += 1
            if changed:
                stats["outputs_modified"] += 1
                obj["output"] = new_text  # 仅当实际发生修改时回写

    if fmt == "object":
        _handle_obj(data)  # type: ignore
    else:
        for obj in data:  # type: ignore
            _handle_obj(obj)

    return data, stats

def main():
    parser = argparse.ArgumentParser(
        description="清理 JSON 中 output 的 <think>...</think> 连续标点（含空白间隔），并统计出现情况；保留恰好为 '...' 的片段。"
    )
    parser.add_argument("input", help="输入 JSON/JSONL 文件路径")
    parser.add_argument("output", help="输出文件路径（清洗后的结果）")
    parser.add_argument("--stats", help="统计结果另存为 JSON（可选）")
    args = parser.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)

    data, fmt = load_any_json(in_path)
    cleaned, stats = process_records(data, fmt)
    dump_any_json(cleaned, fmt, out_path)

    # 打印统计
    print("=== 统计结果（仅计算 <think> 内）===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    # 可选保存统计文件
    if args.stats:
        Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"统计结果已保存到：{args.stats}")

if __name__ == "__main__":
    main()
