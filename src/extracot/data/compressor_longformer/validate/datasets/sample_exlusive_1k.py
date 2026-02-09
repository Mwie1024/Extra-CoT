#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
从指定数据集中抽样 K 条，且不与排除文件中的样本冲突。
支持 JSONL（每行对象/数组）和 JSON 数组文件；支持按 query / query+output / id 去重；
支持控制“题干来源”（仅 query / 仅 messages / 自动优先级 / 多别名并集）。

典型用法（与您的验重逻辑保持一致，仅按 query 去重）：
    python sample_dedup.py \
        --input in.jsonl \
        --output out.jsonl \
        --exclude exclude.jsonl \
        --k 1000 \
        --key_mode q \
        --key_from query_only \
        --strip_asy

Author: you + ChatGPT
"""

import os
import re
import json
import argparse
import random
import hashlib
import unicodedata
from typing import Iterable, Dict, Any, List, Set
from tqdm import tqdm

# ================== 读文件（JSONL / JSON数组 / 行内数组） ==================

def iter_records_any(path: str) -> Iterable[Dict[str, Any]]:
    """兼容 JSONL（每行对象/数组）和 JSON 数组文件；逐条产出 dict"""
    if path.endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as f:
            for ln, line in enumerate(f, 1):
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    yield obj
                elif isinstance(obj, list):
                    for it in obj:
                        if isinstance(it, dict):
                            yield it
        return
    # JSON 数组文件
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                yield it

# ================== 规范化 & 取字段 ==================

_ZW = re.compile(r"[\u200B-\u200D\uFEFF]")  # 零宽
_WS = re.compile(r"\s+")
ASY = re.compile(r"\[asy\].*?\[/asy\]", flags=re.DOTALL | re.IGNORECASE)

_PUNCT_MAP = str.maketrans({
    "“": '"', "”": '"', "‟": '"', "„": '"',
    "‘": "'", "’": "'", "‛": "'",
    "–": "-", "—": "-", "−": "-", "‐": "-",
    "\u00A0": " ",  # 不换行空格 -> 普通空格
})

def canon_text(s: str, *, strip_asy: bool = False, lowercase: bool = True) -> str:
    """
    更强的文本规范化（用于构造去重键）：
    - 可选移除 [asy]...[/asy]
    - NFKC 归一
    - 统一常见标点、移除零宽
    - 合并空白
    - 可选小写
    """
    if not s:
        return ""
    if strip_asy:
        s = ASY.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_PUNCT_MAP)
    s = _ZW.sub("", s)
    s = _WS.sub(" ", s).strip()
    if lowercase:
        s = s.lower()
    return s

def _pick_output_like(rec: Dict[str, Any]) -> str:
    # 优先 model_output/output；若皆无，用 response 兜底
    for k in ("model_output", "output", "response"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""

def _aliases_from_query_fields(rec: Dict[str, Any]) -> List[str]:
    cands = []
    for k in ("query", "original_question", "question", "prompt"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            cands.append(v)
    return cands

def _alias_from_messages(rec: Dict[str, Any]) -> str:
    if isinstance(rec.get("messages"), list) and rec["messages"]:
        c = rec["messages"][0].get("content")
        if isinstance(c, str) and c.strip():
            return c
    return ""

def all_question_aliases(rec: Dict[str, Any], key_from: str) -> List[str]:
    """
    根据策略返回用于构造 key 的题干候选列表。
    key_from:
      - 'query_only'   : 只用 query 系列字段（与您的验重逻辑一致）
      - 'messages_only': 只用 messages[0].content
      - 'auto'         : 先 query 系列，若无再 messages
      - 'multi_alias'  : query 系列 + messages 一起作为“别名并集”
    """
    key_from = key_from.lower()
    if key_from == "query_only":
        return _aliases_from_query_fields(rec)
    if key_from == "messages_only":
        m = _alias_from_messages(rec)
        return [m] if m else []
    if key_from == "auto":
        qs = _aliases_from_query_fields(rec)
        if qs:
            return qs[:1]  # 只取优先的一个，稳定单键
        m = _alias_from_messages(rec)
        return [m] if m else []
    # multi_alias
    seen = set()
    out = []
    for s in _aliases_from_query_fields(rec):
        if s not in seen:
            out.append(s); seen.add(s)
    m = _alias_from_messages(rec)
    if m and m not in seen:
        out.append(m)
    return out

# ================== 键构造（去重/排除依据） ==================

def make_keys(
    rec: Dict[str, Any],
    mode: str = "q",
    strip_asy: bool = False,
    key_from: str = "query_only",
) -> List[str]:
    """
    返回该样本的一组去重键（列表）。
    mode:
      - 'q'  : 按题干（question/query）去重
      - 'qo' : 按 题干+输出 去重
      - 'id' : 按 id 去重（无 id 则回退到 'q'）
    key_from: 参见 all_question_aliases()
    """
    m = mode.lower()
    keys: List[str] = []

    if m == "id":
        rid = rec.get("id")
        if isinstance(rid, str) and rid.strip():
            return ["id:" + rid.strip()]
        # 无 id 回退到 'q'
        m = "q"

    aliases = all_question_aliases(rec, key_from=key_from)

    if m == "q":
        if not aliases:
            # 没有可用题干，退化为整行签名
            raw = hashlib.sha1(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            return ["raw:" + raw]
        for a in aliases:
            q = canon_text(a, strip_asy=strip_asy)
            sig = hashlib.sha1(q.encode("utf-8")).hexdigest()
            keys.append("q:" + sig)
        return keys

    # m == 'qo'
    o = canon_text(_pick_output_like(rec), strip_asy=False)
    if not aliases and not o:
        raw = hashlib.sha1(json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return ["raw:" + raw]
    if not aliases:
        aliases = [""]  # 只有输出也能生成 key
    for a in aliases:
        q = canon_text(a, strip_asy=strip_asy)
        sig = hashlib.sha1((q + "\n" + o).encode("utf-8")).hexdigest()
        keys.append("qo:" + sig)
    return keys

# ================== 水塘抽样（排除冲突） ==================

def reservoir_sample_exclusive(
    iterable: Iterable[Dict[str, Any]],
    k: int,
    exclude_keys: Set[str],
    seed: int = 42,
    key_mode: str = "q",
    strip_asy: bool = False,
    key_from: str = "query_only",
) -> List[Dict[str, Any]]:
    """
    在“非冲突样本”集合上做等概率抽样（Vitter Algorithm R）
    - exclude_keys: 来自排除文件的键集合
    - seen_keys: 输入内部去重（支持多键：别名并集）
    """
    random.seed(seed)
    sample: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    n = 0  # 已看到的“合格（非冲突）”样本数

    for rec in tqdm(iterable, desc="Streaming & sampling"):
        keys = make_keys(rec, mode=key_mode, strip_asy=strip_asy, key_from=key_from)

        # 任一键命中排除/已见则跳过
        if any((key in exclude_keys or key in seen_keys) for key in keys):
            continue

        # 否则：把这条记录的全部键标记为已见（形成别名并集）
        for key in keys:
            seen_keys.add(key)

        if len(sample) < k:
            sample.append(rec)
        else:
            j = random.randint(0, n)  # 含两端
            if j < k:
                sample[j] = rec
        n += 1

    return sample

# ================== 构建排除集 & 保存 ==================

def build_exclude_set(path: str, key_mode: str = "q", strip_asy: bool = False, key_from: str = "query_only") -> Set[str]:
    if not path or not os.path.exists(path):
        print(f"[WARN] exclude 文件不存在：{path}（将不做冲突排除）")
        return set()
    keys: Set[str] = set()
    cnt = 0
    for rec in iter_records_any(path):
        for k in make_keys(rec, mode=key_mode, strip_asy=strip_asy, key_from=key_from):
            keys.add(k)
        cnt += 1
    print(f"[exclude] 读取 {cnt} 条，构建冲突键 {len(keys)} 个（mode={key_mode}, strip_asy={strip_asy}, key_from={key_from}）")
    return keys

def save_jsonl(items: List[Dict[str, Any]], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for obj in items:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# ================== 主流程 ==================

def parse_args():
    ap = argparse.ArgumentParser(
        description="从指定数据集中抽样K条，且不与指定文件中的样本冲突（支持按 query / query+output / id 去重）"
    )
    ap.add_argument("--input", required=True, help="输入数据（JSONL/JSON）")
    ap.add_argument("--output", required=True, help="输出 JSONL 路径")
    ap.add_argument("--exclude", default="", help="冲突排除文件（JSONL/JSON），可空")
    ap.add_argument("--k", type=int, default=1000, help="抽样条数（默认1000）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（保证可复现）")
    ap.add_argument(
        "--key_mode", choices=["q", "qo", "id"], default="q",
        help="去重/排除键：q=按题干，qo=按题干+输出，id=按id（无id回退q）"
    )
    ap.add_argument(
        "--key_from",
        choices=["query_only", "messages_only", "auto", "multi_alias"],
        default="query_only",
        help="题干来源：query_only=只用query系列（与验重一致，默认）；messages_only=只用messages；"
             "auto=先query无则messages；multi_alias=query+messages并集（最严格去重）"
    )
    ap.add_argument(
        "--strip_asy", action="store_true",
        help="对题干剔除 [asy]...[/asy] 块后再做归一化与签名（可减少Asy差异带来的伪重复）"
    )
    return ap.parse_args()

def main():
    args = parse_args()

    if not os.path.exists(args.input):
        raise FileNotFoundError(args.input)

    exclude_keys = build_exclude_set(
        args.exclude,
        key_mode=args.key_mode,
        strip_asy=args.strip_asy,
        key_from=args.key_from
    ) if args.exclude else set()

    sampled = reservoir_sample_exclusive(
        iter_records_any(args.input),
        k=args.k,
        exclude_keys=exclude_keys,
        seed=args.seed,
        key_mode=args.key_mode,
        strip_asy=args.strip_asy,
        key_from=args.key_from,
    )

    print(f"[result] 期望抽样 {args.k} 条，实际得到 {len(sampled)} 条。")
    if len(sampled) < args.k:
        print("[hint] 可用的非冲突样本不足 K 条；如需更多，请更换数据源或放宽冲突条件（例如 key_mode='q' 或改用 key_from='query_only'）。")

    save_jsonl(sampled, args.output)
    print(f"[done] 已写出到 {args.output}")

if __name__ == "__main__":
    main()
