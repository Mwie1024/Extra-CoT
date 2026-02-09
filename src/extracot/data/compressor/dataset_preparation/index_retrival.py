# reconstruct_from_raw.py
# -*- coding: utf-8 -*-
import re
import sys
import json
from dataclasses import dataclass
from typing import List, Tuple, Dict, Iterable, Optional

# ---- 1) 数学原子化：把 $...$, $$...$$, \(...\), \[...\], ```math ... ``` 替换成 [MATH_i] ----
MATH_PATTERNS = [
    r"\$\$(?:.|\n)*?\$\$",      # $$...$$
    r"\$(?:.|\n)*?\$",          # $...$
    r"\\\((?:.|\n)*?\\\)",      # \(...\)
    r"\\\[(?:.|\n)*?\\\]",      # \[...\]
    r"```math(?:.|\n)*?```"     # ```math ... ```
]

def mask_math_spans(s: str) -> Tuple[str, Dict[str, str]]:
    out = s
    mapping: Dict[str, str] = {}
    i = 1
    for pat in MATH_PATTERNS:
        while True:
            m = re.search(pat, out, flags=re.MULTILINE)
            if not m:
                break
            span = m.group(0)
            key = f"[MATH_{i}]"
            out = out[:m.start()] + key + out[m.end():]
            mapping[key] = span
            i += 1
    return out, mapping

# -------------------------------
# 2) 空白分词 & 枚举（与示例一致）
# -------------------------------
def whitespace_tokens(s: str) -> List[str]:
    # 忠实于原文本，不做词形变化；\S+ 切分（含标点黏连）
    return re.findall(r"\S+", s)

def enumerate_tokens(tokens: List[str]) -> List[Tuple[int, str]]:
    # 返回 [(1, tok1), (2, tok2), ...]
    return list(enumerate(tokens, 1))

# -------------------------------
# keep 解析与工具函数
# -------------------------------
def _digits(s: str) -> List[int]:
    return [int(x) for x in re.findall(r"\d+", s)]

def build_keep_indices(keep_spec: Dict, n: Optional[int] = None) -> List[int]:
    ranges = keep_spec.get("ranges", []) or []
    singles = keep_spec.get("single", []) or []

    keep: set[int] = set()

    for r in ranges:
        if isinstance(r, str):
            nums = _digits(r)
            if len(nums) >= 2:
                a, b = nums[0], nums[1]
                if a > b:
                    a, b = b, a
                keep.update(range(a, b + 1))
        elif isinstance(r, (list, tuple)) and len(r) == 2:
            a, b = int(r[0]), int(r[1])
            if a > b:
                a, b = b, a
            keep.update(range(a, b + 1))

    for s in singles:
        if isinstance(s, int):
            keep.add(s)
        else:
            ds = _digits(str(s))
            if ds:
                keep.add(ds[0])

    if n is not None:
        keep = {i for i in keep if 1 <= i <= n}

    return sorted(keep)

def merge_contiguous(idxs: Iterable[int]) -> List[Tuple[int, int]]:
    idxs = list(idxs)
    if not idxs:
        return []
    segs: List[Tuple[int, int]] = []
    s = e = idxs[0]
    for i in idxs[1:]:
        if i == e + 1:
            e = i
        else:
            segs.append((s, e))
            s = e = i
    segs.append((s, e))
    return segs

def zero_pad_width_from_sources(
    segs: List[Tuple[int, int]],
    total_tokens: int,
    provided_ranges: List
) -> int:
    widths = []
    for r in provided_ranges:
        if isinstance(r, str):
            for d in re.findall(r"\d+", r):
                widths.append(len(d))
    if widths:
        return max(widths)
    return max(3, len(str(total_tokens)))

def format_segments(segs: List[Tuple[int, int]], width: int) -> List[str]:
    out = []
    for a, b in segs:
        if a == b:
            out.append(f"{a:0{width}d}")
        else:
            out.append(f"{a:0{width}d}-{b:0{width}d}")
    return out

def reconstruct_text(tokens: List[str], kept_indices: List[int]) -> str:
    return " ".join(tokens[i - 1] for i in kept_indices if 1 <= i <= len(tokens))

def unmask_math(text: str, mapping: Dict[str, str]) -> str:
    for k in sorted(mapping.keys(), key=len, reverse=True):
        text = text.replace(k, mapping[k])
    return text

@dataclass
class Stats:
    total_tokens: int
    kept_tokens: int
    dropped_tokens: int
    keep_ratio: float
    num_ranges_provided: int
    num_single_provided: int
    num_segments_after_merge: int
    merged_segments_fmt: List[str]

# -------------------------------
# 主流程：从原始 CoT 文本直接构建压缩输出
# -------------------------------
def compress_from_raw_text(raw_text: str, keep_spec: Dict, restore_math: bool = False) -> Dict:
    # 1) 数学掩码
    masked_text, mapping = mask_math_spans(raw_text)

    # 2) 空白分词并枚举（仅内存）
    tokens = whitespace_tokens(masked_text)
    pairs = enumerate_tokens(tokens)  # [(idx, tok), ...]

    # 3) 解析 keep → 索引列表
    kept_indices = build_keep_indices(keep_spec, n=len(tokens))

    # 4) 重建压缩文本
    compressed_text = reconstruct_text(tokens, kept_indices)
    compressed_text_unmasked = unmask_math(compressed_text, mapping) if restore_math else None

    # 5) 段合并与统计
    segs = merge_contiguous(kept_indices)
    width = zero_pad_width_from_sources(segs, len(tokens), keep_spec.get("ranges", []) or [])
    seg_strings = format_segments(segs, width)

    stats = Stats(
        total_tokens=len(tokens),
        kept_tokens=len(kept_indices),
        dropped_tokens=len(tokens) - len(kept_indices),
        keep_ratio=(len(kept_indices) / len(tokens)) if tokens else 0.0,
        num_ranges_provided=len(keep_spec.get("ranges", []) or []),
        num_single_provided=len(keep_spec.get("single", []) or []),
        num_segments_after_merge=len(segs),
        merged_segments_fmt=seg_strings
    )

    return {
        "compressed_text_masked": compressed_text,
        "compressed_text_unmasked": compressed_text_unmasked,  # 若 restore_math=False 则为 None
        "kept_indices": kept_indices,
        "merged_segments": seg_strings,
        "stats": {
            "total_tokens": stats.total_tokens,
            "kept_tokens": stats.kept_tokens,
            "dropped_tokens": stats.dropped_tokens,
            "keep_ratio": round(stats.keep_ratio, 6),
            "num_ranges_provided": stats.num_ranges_provided,
            "num_single_provided": stats.num_single_provided,
            "num_segments_after_merge": stats.num_segments_after_merge
        }
    }

# -------------------------------
# 示例：直接在此处填写 RAW_TEXT 或传入文件路径
# -------------------------------
DEFAULT_KEEP_SPEC = {"ranges": ["008-012", "035-041", "051-055", "061-067", "076-086", "098-102", "114-126", "128-141", "142-165"]}




def main():
    # 用法：
    # 1) python reconstruct_from_raw.py <path/to/cot.txt>
    # 2) 或不带参数，直接在下方 RAW_TEXT 里填入你的原始 CoT 文本
    if len(sys.argv) > 1:
        raw_text = open(sys.argv[1], "r", encoding="utf-8").read()
    else:
        RAW_TEXT = " Therefore, the reaction function for B is qB = (250 - qA)/2.\n\nNow, to find the Nash equilibrium, we need to solve these two reaction functions together. Let me substitute one into the other. Let's take qA = (250 - qB)/2 and plug in qB from B's reaction function. Since qB = (250 - qA)/2, substitute that into A's equation:\n\nqA = [250 - (250 - qA)/2]/2.\n\nLet me compute that step by step. First, expand the numerator inside the brackets:\n\n250 - (250 - qA)/2. Let me write 250 as 500/2 to have a common denominator:\n\n500/2 - 250/2 + qA/2 = (500 - 250)/2 + qA/2 = 250/2 + qA/2 = 125 + qA/2.\n\nTherefore, qA = [125 + qA/2]/2 = 125/2 + qA/4.\n\nNow, subtract qA/4 from both sides:\n\nqA - qA/4 = 125/2 => (3/4)qA = 125/2.\n\nMultiply both sides by 4/3:\n\nqA = (125/2) * (4/3) = (125 * 4) / (2 * 3) = 500 / 6 ≈ 83.333...\n\nWait, but let me check if I did the algebra correctly. Let me redo the substitution step.\n\nOriginal equations:\n\nqA = (250 - qB)/2\n\nqB = (250 - qA)/2\n\nSo substitute qB into qA's equation:\n\nqA = [250 - (250 - qA)/2]/2\n\nLet me compute the numerator first: 250 - [(250 - qA)/2]\n\nMultiply numerator and denominator appropriately:\n\nLet me write 250 as 500/2:\n\n500/2 - (250 - qA)/2 = [500 - 250 + qA]/2 = [250 + qA]/2\n\nTherefore, qA = [250 + qA]/2 divided by 2 again? Wait, no. Wait, the entire expression is divided by 2. Wait:\n\nOriginal substitution:\n\nqA = [250 - qB]/2, where qB is (250 - qA)/2. "
        raw_text = RAW_TEXT

    keep_spec = DEFAULT_KEEP_SPEC  # 如需从外部 JSON 读，可自行替换

    result = compress_from_raw_text(raw_text, keep_spec, restore_math=True)

    print("# === 压缩后的文本（已还原 [MATH_i]） ===")
    print(result["compressed_text_unmasked"] or result["compressed_text_masked"])

    print("\n# === 合并后的连续段 ===")
    print(result["merged_segments"])

    print("\n# === 数据统计 ===")
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))

    # 如需查看保留索引预览：
    # print(result["kept_indices"][:50])

if __name__ == "__main__":
    main()
