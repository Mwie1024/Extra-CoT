# diff_by_query_against_bc.py
# 用法示例：
#   python diff_by_query_against_bc.py \
#       --a A.jsonl \
#       --b B.jsonl \
#       --c C.jsonl \
#       --out A_only.jsonl \
#       --query-key query
#
# 说明：
# - 从 B/C 每条样本的所有字符串字段中，提取模式：
#   "Please reason step by step, and put your final answer within \boxed{}.\n<xxx> <COMP_AUTO>"
#   中的 <xxx>；将其与 A 的 record[query_key] 做匹配（标准化后精确相等）。
# - 输出 A 中“query 不在 B 或 C 提取集合里”的样本到 out。
# - 结束时打印最终保留条数。

import argparse
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set

# --- 配置正则：匹配 B/C 模板，捕获 xxx ---
# 说明：
#   - \\boxed{} 在实际字符串中是 \boxed{}，正则需写成 \\boxed\{\}
#   - 句点后允许可选空白与可选换行：\.\s*\n?
#   - xxx 跨行捕获，直到 <COMP_AUTO> 之前
BC_PATTERN = re.compile(
    r"Please reason step by step, and put your final answer within\s+\\boxed\{\}\.\s*\n?(.*?)\s*<COMP_AUTO>",
    flags=re.DOTALL
)

def normalize(s: str) -> str:
    """对比前的标准化：统一换行、压缩空白、去首尾空白。"""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def iter_strings(obj: Any) -> Iterable[str]:
    """递归遍历对象中所有字符串值。"""
    if obj is None:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)

def extract_xxx_from_record(record: Dict[str, Any]) -> List[str]:
    """从一条 B/C 记录里提取所有符合模板的 xxx（可能有 0~多处）。"""
    results: List[str] = []
    for s in iter_strings(record):
        m = BC_PATTERN.search(s)
        if m:
            results.append(m.group(1).strip())
    return results

def load_xxx_set(jsonl_path: str) -> Set[str]:
    """
    读取 B/C.jsonl，提取所有 xxx（按上面的模板），做 normalize 后去重成集合。
    忽略解析失败或无匹配的行。
    """
    found: Set[str] = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for xxx in extract_xxx_from_record(rec):
                found.add(normalize(xxx))
    return found

def filter_a(a_path: str, bc_xxx: Set[str], out_path: str, query_key: str = "query") -> int:
    """
    遍历 A.jsonl：若 normalize(record[query_key]) 不在 bc_xxx 中，则写出到 out。
    返回保留条数。
    """
    kept = 0
    with open(a_path, "r", encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            q = rec.get(query_key, None)
            q_norm = normalize(q)
            if q_norm and q_norm not in bc_xxx:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                kept += 1
            # 如果 q 为空/缺失：默认当作“保留”，也可以按需改为丢弃
            elif not q_norm:
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
                kept += 1
    return kept

def main():
    ap = argparse.ArgumentParser(description="从A.jsonl中提取不属于B.jsonl和C.jsonl的样本（按模板抽取 B/C 的 xxx 与 A.query 匹配）")
    ap.add_argument("--a", required=True, help="A.jsonl 路径（含 query 字段）")
    ap.add_argument("--b", required=True, help="B.jsonl 路径")
    ap.add_argument("--c", required=True, help="C.jsonl 路径")
    ap.add_argument("--out", required=True, help="输出 JSONL（A_only）")
    ap.add_argument("--query-key", default="query", help="A.jsonl 里用于匹配的字段名（默认 query）")
    args = ap.parse_args()

    set_b = load_xxx_set(args.b)
    set_c = load_xxx_set(args.c)
    bc_all = set_b | set_c

    kept = filter_a(args.a, bc_all, args.out, query_key=args.query_key)
    print(f"最终保留条数: {kept}")

if __name__ == "__main__":
    main()
