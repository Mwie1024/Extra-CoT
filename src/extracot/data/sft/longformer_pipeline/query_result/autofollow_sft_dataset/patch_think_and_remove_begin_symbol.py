#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json, re, sys, argparse
from typing import List, Tuple, Optional

# 匹配第一段 <think>...</think>（大小写不敏感）
RE_THINK_BLOCK = re.compile(r'(?is)(<think>)([\s\S]*?)(</think>)')

def load_items(path: str) -> Tuple[List[dict], str]:
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    fmt = "json" if text.lstrip().startswith("[") else "jsonl"
    if fmt == "json":
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("JSON 文件顶层应为数组。")
    else:
        items = []
        for ln, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s:
                continue
            try:
                items.append(json.loads(s))
            except Exception as e:
                raise RuntimeError(f"第 {ln} 行 JSONL 解析失败: {e}")
    return items, fmt

def save_items(items: List[dict], path: str, fmt: str):
    with open(path, "w", encoding="utf-8") as f:
        if fmt == "json":
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")
        else:
            for obj in items:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# 找到最后一个“完整配对”的 \boxed{...}（支持嵌套花括号）
def find_last_boxed_segment(s: str) -> Optional[str]:
    pat = re.compile(r'(?is)\\boxed\s*\{')
    last_seg = None
    pos = 0
    while True:
        m = pat.search(s, pos)
        if not m:
            break
        i = m.end()
        depth = 1
        while i < len(s) and depth > 0:
            ch = s[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        if depth == 0:
            last_seg = s[m.start():i]  # 包含 \boxed{...}
            pos = i
        else:
            # 最后一次出现不完整，直接停止；保留前面最后一个完整段（若有）
            break
    return last_seg

def transform_output(out_text: str, boxed: str) -> Tuple[str, bool]:
    """
    在第一段 <think>...</think> 内：
      1) 若开头是换行后紧跟 '.' 或 ','（含中文 '，'），移除该标点及其后的空白
      2) 末尾追加一行 'The final answer is \boxed{...}'
    """
    if not isinstance(out_text, str):
        return out_text, False

    def _repl(m: re.Match) -> str:
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)

        # 去掉开头的 ". " 或 ", " 或 "， "（保留前导空白/换行）
        body = re.sub(r'^(\s*)[.,，]\s+', r'\1', body)

        # body = body.rstrip() + "\n" + f"The final answer is {boxed}"
        body = body.rstrip() + "\n"
        return f"{open_tag}{body}{close_tag}"

    new_text, nsub = RE_THINK_BLOCK.subn(_repl, out_text, count=1)
    return (new_text if nsub else out_text), bool(nsub)

def main():
    ap = argparse.ArgumentParser(
        description="在每条样本的 output 中修正 <think> 起始 '. ' 或 ', '，并在 </think> 前追加 'The final answer is \\boxed{...}'；若存在 <think> 但找不到完整 \\boxed{}，则丢弃该样本。"
    )
    ap.add_argument("input", help="输入 JSON 或 JSONL（顶层数组或 .jsonl）")
    ap.add_argument("-o", "--output", required=True, help="输出路径（格式随输入）")
    args = ap.parse_args()

    items, fmt = load_items(args.input)
    kept, dropped_no_box, unchanged_no_think, changed = 0, 0, 0, 0

    new_items = []
    for idx, obj in enumerate(items, 1):
        out = obj.get("output")
        if not isinstance(out, str):
            new_items.append(obj)
            kept += 1
            continue

        has_think = bool(RE_THINK_BLOCK.search(out))
        if not has_think:
            # 没有 <think>：按你的描述未要求丢弃，保留原样
            # 如需丢弃，把下面两行改为：dropped_no_box += 1; continue
            unchanged_no_think += 1
            new_items.append(obj)
            kept += 1
            continue

        boxed = find_last_boxed_segment(out)
        # if boxed is None:
        #     # 有 <think> 但找不到完整 \boxed{} => 丢弃该样本
        #     dropped_no_box += 1
        #     continue

        new_out, did = transform_output(out, boxed)
        if did:
            obj = dict(obj)
            obj["output"] = new_out
            changed += 1
        new_items.append(obj)
        kept += 1

    save_items(new_items, args.output, fmt)
    print(
        f"# Done. total_in={len(items)} kept={kept} changed={changed} "
        f"unchanged_no_think={unchanged_no_think} dropped_no_box={dropped_no_box}",
        file=sys.stderr
    )

if __name__ == "__main__":
    main()
