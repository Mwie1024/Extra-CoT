#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys
import re
import argparse

# 匹配 <think> ... </think>（大小写不敏感，允许空白/换行）
THINK_PAT = re.compile(
    r'(<\s*think\s*>)(.*?)(<\s*/\s*think\s*>)',
    flags=re.IGNORECASE | re.DOTALL
)

KEEP_KEYS = ("question", "output", "actual_ratio", "orig_tokens", "comp_tokens", "special_tokens")

def process(in_path: str, out_path: str, replace_all: bool = False) -> None:
    """
    - 仅当 output 中包含 <think>...</think> 时才保留；
    - 有 compressed_cot 则替换 think 块内容，否则保留原 output；
    - question 优先；若没有 question 但有 query，则用 query 填充 question；
    - 仅输出 5 个字段（缺失填 None）。
    """
    processed = kept = dropped_no_think = replaced = kept_no_comp = 0

    with open(in_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for ln, line in enumerate(fin, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception as e:
                sys.stderr.write(f"[WARN] {in_path}:{ln} JSON parse error: {e}\n")
                continue

            processed += 1
            output = obj.get("output") or obj.get("model_output")
            if not isinstance(output, str):
                dropped_no_think += 1
                continue

            # 必须包含 <think> ... </think>
            if not THINK_PAT.search(output):
                dropped_no_think += 1
                continue

            comp = obj.get("compressed_cot")
            if comp is not None:
                comp_str = str(comp)
                def _repl(m: re.Match) -> str:
                    return f"{m.group(1)}{comp_str}{m.group(3)}"
                count = 0 if replace_all else 1
                new_output, n_sub = THINK_PAT.subn(_repl, output, count=count)
                if n_sub > 0:
                    replaced += 1
            else:
                new_output = output
                kept_no_comp += 1

            # question 优先；若无 question 用 query 填充
            question_val = obj.get("question")
            if question_val is None:
                question_val = obj.get("query")
            
            id = obj.get("id")

            new_obj = {
                "id": id,
                "question": question_val,
                "output": new_output,
                "actual_ratio": obj.get("actual_ratio"),
                "orig_tokens": obj.get("orig_tokens"),
                "comp_tokens": obj.get("comp_tokens"),
                "special_token": obj.get("special_token"),
            }
            fout.write(json.dumps(new_obj, ensure_ascii=False) + "\n")
            kept += 1

    sys.stderr.write(
        f"[INFO] processed_lines={processed}\n"
        f"[INFO] kept={kept}, dropped_no_think={dropped_no_think}\n"
        f"[INFO] replaced_with_compressed_cot={replaced}, kept_without_compressed_cot={kept_no_comp}\n"
        f"[INFO] wrote -> {out_path}\n"
    )

def main():
    ap = argparse.ArgumentParser(
        description="Keep entries with <think>...</think>; replace with compressed_cot if present; keep only 5 keys; question falls back to query."
    )
    ap.add_argument("-i", "--input", required=True, help="输入 JSONL 路径")
    ap.add_argument("-o", "--output", required=True, help="输出 JSONL 路径")
    ap.add_argument("--replace_all", action="store_true", help="替换所有 <think> 块（默认只替第一个）")
    args = ap.parse_args()

    process(args.input, args.output, replace_all=args.replace_all)

if __name__ == "__main__":
    main()
