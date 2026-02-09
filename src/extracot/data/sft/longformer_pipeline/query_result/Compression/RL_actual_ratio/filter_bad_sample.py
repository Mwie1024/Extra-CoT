#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_recat_multi.py

目的：
- 对五档压缩比 recat_r020/040/060/080/100.jsonl 进行“跨档位”联合清洗，
  标出并剔除易诱发复读/结构异常/锚点流失严重的样本。
- 仅检查 <think> ... </think> 之间的内容。

用法示例：
  python filter_recat_multi.py --input_dir /path/to/dir --out_dir /path/to/cleaned

可调阈值在 CONFIG 中（文件内顶部）。
"""

import os, re, json, argparse, collections, math, csv
from typing import Dict, List, Any, Tuple, Optional
import tiktoken

# ====================== 可调阈值 ======================

CONFIG = {
    # 1) 长度阈值（tiktoken 计数，仅 <think> 内）
    "min_think_tokens": {  # 可按档位调
        "020": 100,
        "040": 120,
        "060": 140,
        "080": 160,
        "100": 180,  # r100 太短也容易是噪声 CoT
    },

    # 2) 重复度阈值（词级）
    "ttr_min": 0.25,                 # 词型占比（type-token ratio）过低 → 噪声/复读
    "bigram_repeat_max": 0.50,       # bigram 重复率上限
    "trigram_repeat_max": 0.35,      # trigram 重复率上限
    "max_adjacent_ngram_run": 3,     # 相邻 n-gram 连续重复超过该阈值 → 剔除

    # 3) 锚点保留（跨档对 r100）
    # 低档位应保留足够的数字/数学符号，避免“只剩连接词”
    # 阈值是 “#anchors(rxx) / #anchors(r100)”
    "anchor_min_retain": {
        "020": 0.35,
        "040": 0.45,
        "060": 0.58,
        "080": 0.70,
        "100": 1.00
    },

    # 4) 压缩一致性
    "ratio_tolerance_abs": 0.06,     # 实际压缩比 vs 目标压缩比 的绝对偏差允许值
    "enforce_monotonic": True,       # token 数应随档位单调递增（020<=...<=100）

    # 5) 符号/标点占比上限（仅统计 <think> 内字符）
    "punct_ratio_max": 0.55,

    # 6) 字符级复读：同一字符的最长连跑长度上限
    "max_char_run": 20,

    # 7) COMP 标签一致性：若样本输出首行含 <COMP_xx>，应与文件档位一致
    "enforce_comp_tag_match": True,

    # tiktoken 编码（与训练保持一致，常用 cl100k_base；Qwen 系列一般也可）
    "tiktoken_encoding": "cl100k_base",
}

# ====================== 工具函数 ======================

THINK_OPEN  = re.compile(r"<think>", re.IGNORECASE)
THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)
COMP_TAG    = re.compile(r"<\s*COMP[_\-]?(\d+)\s*>", re.IGNORECASE)

NUM_RE = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?(?![A-Za-z])")
LATEX_MATH_MACROS = [
    r"\\frac", r"\\cdot", r"\\times", r"\\sqrt", r"\\sum", r"\\prod",
    r"\\left", r"\\right", r"\\begin", r"\\end"
]
MATH_SYMS = set(list("=+-*/^%()[]{}<>|,:;"))

def extract_output_text(rec: Dict[str, Any]) -> str:
    # 尽量兼容不同数据格式
    if isinstance(rec.get("output"), str):
        return rec["output"]
    if isinstance(rec.get("response"), str):
        return rec["response"]
    if "messages" in rec and isinstance(rec["messages"], list):
        # 取最后一个 assistant
        for m in reversed(rec["messages"]):
            if isinstance(m, dict) and m.get("role") == "assistant":
                return str(m.get("content") or "")
    if isinstance(rec.get("text"), str):
        return rec["text"]
    return ""

def extract_think_inner(text: str) -> str:
    if not text:
        return ""
    low = text.lower()
    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    if m1 and m2 and m1.start() < m2.start():
        return text[m1.end():m2.start()]
    return ""

def has_closed_think(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    m1 = THINK_OPEN.search(low); m2 = THINK_CLOSE.search(low)
    return bool(m1 and m2 and m1.start() < m2.start())

def first_comp_tag(text: str) -> Optional[str]:
    if not text: return None
    m = COMP_TAG.search(text)
    return m.group(1) if m else None

def words_for_ngram(s: str) -> List[str]:
    # 词级粗分：保留数字/字母/反斜杠，其他符号当作分隔
    return [w for w in re.split(r"[^\w\\]+", s) if w]

def ngram_counts(seq: List[str], n: int) -> Dict[Tuple[str,...], int]:
    cnt = collections.Counter()
    for i in range(len(seq)-n+1):
        cnt[tuple(seq[i:i+n])] += 1
    return cnt

def repeat_fraction(seq: List[str], n: int) -> float:
    # n-gram 重复率：出现次数>=2 的 n-gram 覆盖了多少 n-gram 位置
    L = max(0, len(seq)-n+1)
    if L == 0: return 0.0
    cnt = ngram_counts(seq, n)
    rep_positions = sum(c for c in cnt.values() if c >= 2)
    return rep_positions / L

def longest_adjacent_repeated_ngram_run(seq: List[str], n: int) -> int:
    # 检测相邻 n-gram 连续重复的最长 run
    if len(seq) < 2*n: return 1
    max_run, run = 1, 1
    prev = tuple(seq[0:n])
    for i in range(1, len(seq)-n+1):
        cur = tuple(seq[i:i+n])
        if cur == prev:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
        prev = cur
    return max_run

def ttr_ratio(seq: List[str]) -> float:
    if not seq: return 0.0
    return len(set(seq)) / len(seq)

def count_char_run(s: str) -> int:
    max_run, run = 1, 1
    for i in range(1, len(s)):
        if s[i] == s[i-1]:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 1
    return max_run

def punct_ratio(s: str) -> float:
    if not s: return 0.0
    total = len(s)
    puncts = sum(1 for ch in s if ch in MATH_SYMS or not ch.isalnum())
    return puncts / total if total else 0.0

def collect_anchors(s: str) -> Tuple[int, int]:
    # 统计“锚点”：数字 + 数学符号/LaTeX 宏（粗略近似）
    nums = len(NUM_RE.findall(s))
    macros = 0
    for pat in LATEX_MATH_MACROS:
        macros += len(re.findall(pat, s))
    syms = sum(1 for ch in s if ch in MATH_SYMS)
    anchors = nums + macros + syms
    return anchors, nums

def tok_count(encoder, s: str) -> int:
    if not s: return 0
    return len(encoder.encode(s, disallowed_special=()))

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            s = ln.strip()
            if not s: continue
            try:
                obj = json.loads(s)
                out.append(obj)
            except Exception:
                pass
    return out

# ====================== 主流程 ======================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_dir", required=True)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--encoding", default=CONFIG["tiktoken_encoding"])
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    # 读取 5 档文件
    files = {
        "020": os.path.join(args.input_dir, "recat_r020.jsonl"),
        "040": os.path.join(args.input_dir, "recat_r040.jsonl"),
        "060": os.path.join(args.input_dir, "recat_r060.jsonl"),
        "080": os.path.join(args.input_dir, "recat_r080.jsonl"),
        "100": os.path.join(args.input_dir, "recat_r100.jsonl"),
    }
    for k, p in files.items():
        if not os.path.isfile(p):
            raise FileNotFoundError(f"缺少文件: {p}")

    enc = tiktoken.get_encoding(args.encoding)

    # 载入所有样本
    data_by_ratio: Dict[str, List[Dict[str, Any]]] = {k: load_jsonl(p) for k, p in files.items()}

    # 建立 id -> r100 think 文本 / token 数 / anchors
    base_map: Dict[str, Dict[str, Any]] = {}
    for ex in data_by_ratio["100"]:
        rid = str(ex.get("id") or ex.get("sample_id") or ex.get("uid") or "")
        out = extract_output_text(ex)
        think = extract_think_inner(out)
        base_map[rid] = {
            "think": think,
            "tok": tok_count(enc, think),
            "anchors": collect_anchors(think)[0],
        }

    # 汇总指标并判定
    ratios = ["020","040","060","080","100"]
    target_ratio = {"020":0.2,"040":0.4,"060":0.6,"080":0.8,"100":1.0}

    # 结果表
    scores_path = os.path.join(args.out_dir, "scores.tsv")
    drop_path   = os.path.join(args.out_dir, "dropped.tsv")

    kept_by_ratio: Dict[str, List[Dict[str, Any]]] = {r: [] for r in ratios}
    dropped_rows: List[List[Any]] = []
    score_rows:   List[List[Any]] = []

    headers = [
        "ratio","id","think_tokens","ttr","bi_rep","tri_rep",
        "adj_tri_run","punct_ratio","max_char_run",
        "anchors","nums","anchor_retain_vs_r100","tok_vs_r100",
        "closed_think","comp_tag_match","actual_ratio","reasons"
    ]
    with open(scores_path, "w", encoding="utf-8", newline="") as fs, \
         open(drop_path,   "w", encoding="utf-8", newline="") as fd:

        tsvw = csv.writer(fs, delimiter="\t")
        tsvw.writerow(headers)
        tsvd = csv.writer(fd, delimiter="\t")
        tsvd.writerow(["ratio","id","reasons","debug"])

        # 先把各 ratio 的 token 数统计出来，便于单调性检查
        tok_grid: Dict[str, Dict[str,int]] = collections.defaultdict(dict)
        think_texts: Dict[str, Dict[str,str]] = collections.defaultdict(dict)
        comp_tag_mismatch: Dict[str, Dict[str,bool]] = collections.defaultdict(dict)

        for r in ratios:
            for ex in data_by_ratio[r]:
                rid = str(ex.get("id") or ex.get("sample_id") or ex.get("uid") or "")
                out = extract_output_text(ex)
                think = extract_think_inner(out)
                think_texts[rid][r] = think
                tok_grid[rid][r] = tok_count(enc, think)
                # comp tag
                tag = first_comp_tag(out)
                if CONFIG["enforce_comp_tag_match"] and tag is not None:
                    comp_tag_mismatch[rid][r] = (tag != str(int(float(target_ratio[r]*100))))
                else:
                    comp_tag_mismatch[rid][r] = False

        # 单调性判定（跨档）
        monotonic_bad: Dict[str, bool] = {}
        if CONFIG["enforce_monotonic"]:
            for rid, row in tok_grid.items():
                bad = False
                prev = -1
                for r in ratios:
                    cur = row.get(r, 0)
                    if cur < prev - 5:  # 允许极小抖动
                        bad = True
                        break
                    prev = cur
                monotonic_bad[rid] = bad
        else:
            monotonic_bad = {rid: False for rid in tok_grid.keys()}

        # 逐样本逐档位打分+筛选
        for r in ratios:
            for ex in data_by_ratio[r]:
                rid = str(ex.get("id") or ex.get("sample_id") or ex.get("uid") or "")
                out = extract_output_text(ex)
                think = think_texts.get(rid, {}).get(r, "") or ""
                reasons = []

                # a) 结构
                closed = has_closed_think(out)
                if not closed:
                    reasons.append("no_closed_think")

                if comp_tag_mismatch.get(rid, {}).get(r, False):
                    reasons.append("comp_tag_mismatch")

                # b) 长度
                tok_len = tok_grid.get(rid, {}).get(r, 0)
                min_len = CONFIG["min_think_tokens"][r]
                if tok_len < min_len:
                    reasons.append(f"too_short({tok_len}<{min_len})")

                # c) 重复度（词级）
                words = words_for_ngram(think)
                ttr = ttr_ratio(words)
                bi_rep = repeat_fraction(words, 2)
                tri_rep = repeat_fraction(words, 3)
                adj_tri_run = longest_adjacent_repeated_ngram_run(words, 3)

                if ttr < CONFIG["ttr_min"]:
                    reasons.append(f"low_ttr({ttr:.2f})")
                if bi_rep > CONFIG["bigram_repeat_max"]:
                    reasons.append(f"high_bigram_rep({bi_rep:.2f})")
                if tri_rep > CONFIG["trigram_repeat_max"]:
                    reasons.append(f"high_trigram_rep({tri_rep:.2f})")
                if adj_tri_run > CONFIG["max_adjacent_ngram_run"]:
                    reasons.append(f"adj_trigram_run({adj_tri_run})")

                # d) 符号/标点占比 & 字符连跑
                pr = punct_ratio(think)
                if pr > CONFIG["punct_ratio_max"]:
                    reasons.append(f"punct_ratio_high({pr:.2f})")
                mcr = count_char_run(think)
                if mcr > CONFIG["max_char_run"]:
                    reasons.append(f"char_run({mcr})")

                # e) 锚点保留（对 r100）
                base = base_map.get(rid)
                anchors, nums = collect_anchors(think)
                retain_ratio = None
                tok_vs_r100  = None
                if base and base["anchors"] > 0:
                    retain_ratio = anchors / max(1, base["anchors"])
                    need = CONFIG["anchor_min_retain"][r]
                    if retain_ratio < need:
                        reasons.append(f"anchor_retain_low({retain_ratio:.2f}<{need:.2f})")
                    tok_vs_r100 = tok_len / max(1, base["tok"])
                    # f) 压缩比贴合度
                    target = target_ratio[r]
                    if abs(tok_vs_r100 - target) > CONFIG["ratio_tolerance_abs"]:
                        reasons.append(f"ratio_off({tok_vs_r100:.2f} vs {target:.2f})")

                # g) 跨档单调性（对该 id）
                if monotonic_bad.get(rid, False):
                    reasons.append("non_monotonic_across_ratios")

                # 记分
                score_rows.append([
                    r, rid, tok_len, f"{ttr:.3f}", f"{bi_rep:.3f}", f"{tri_rep:.3f}",
                    adj_tri_run, f"{pr:.3f}", mcr, anchors, nums,
                    f"{retain_ratio:.3f}" if retain_ratio is not None else "",
                    f"{tok_vs_r100:.3f}" if tok_vs_r100 is not None else "",
                    int(closed), int(not comp_tag_mismatch.get(rid, {}).get(r, False)),
                    f"{tok_len/max(1,base_map.get(rid,{}).get('tok',0)):.3f}" if base_map.get(rid) else "",
                    ";".join(reasons)
                ])

                # 决策：有任一“硬理由”则丢弃
                if reasons:
                    dropped_rows.append([r, rid, ";".join(reasons), f"tok={tok_len} ttr={ttr:.2f} rep2={bi_rep:.2f} rep3={tri_rep:.2f} anchors={anchors}"])
                else:
                    kept_by_ratio[r].append(ex)

        # 写表
        for row in score_rows:
            tsvw.writerow(row)
        for row in dropped_rows:
            tsvd.writerow(row)

    # 写清洗后的 5 个文件
    for r in ratios:
        outp = os.path.join(args.out_dir, f"recat_r{r}.clean.jsonl")
        with open(outp, "w", encoding="utf-8") as f:
            for ex in kept_by_ratio[r]:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    # 统计
    for r in ratios:
        total = len(data_by_ratio[r])
        kept  = len(kept_by_ratio[r])
        print(f"[{r}] kept={kept} dropped={total-kept} total={total}")

if __name__ == "__main__":
    main()
