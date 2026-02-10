#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import argparse
import random
import re
from pathlib import Path
from typing import Any, List

# 把 <think> 后面的任意空白（含已有换行）规范为恰好一个 \n；大小写不敏感
THINK_FIX = re.compile(r'(<\s*think\s*>)\s*', flags=re.IGNORECASE)

def load_json_array(path: Path) -> List[Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    # 若不是数组，尝试常见包装
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    raise ValueError(f"{path} 不是 JSON 数组（或不含 data 数组）")

def fix_output_think(output: Any) -> Any:
    if isinstance(output, str):
        # 对每个 <think> 应用一次规范化
        return THINK_FIX.sub(r"\1\n", output)
    return output

def main():
    ap = argparse.ArgumentParser(description="拼接并打乱两个 JSON（数组），并将 output 中的 <think> 后统一加上换行。")
    ap.add_argument("--json1", required=True, type=Path)
    ap.add_argument("--json2", required=True, type=Path)
    ap.add_argument("--out",   required=True, type=Path)
    ap.add_argument("--seed",  type=int, default=42, help="随机种子（用于可复现打乱）")
    args = ap.parse_args()

    data1 = load_json_array(args.json1)
    data2 = load_json_array(args.json2)
    merged = data1 + data2

    # 处理 output
    for item in merged:
        if isinstance(item, dict) and "output" in item:
            item["output"] = fix_output_think(item["output"])

    # 打乱
    random.seed(args.seed)
    random.shuffle(merged)

    # 输出为 JSON 数组
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
