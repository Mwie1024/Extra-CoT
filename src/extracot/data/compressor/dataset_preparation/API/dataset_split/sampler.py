# sample_level_sampler.py
import re, json, random
from collections import defaultdict, Counter
from typing import List, Dict, Tuple

MATH_LATEX_PAT = re.compile(
    r"\$\$[\s\S]*?\$\$"              # $$ ... $$
    r"|\$[^$]+\$"                    # $ ... $
    r"|\\\[[\s\S]*?\\\]"             # \[ ... \]
    r"|\\\([\s\S]*?\\\)"             # \( ... \)
    r"|```math[\s\S]*?```"           # ```math ... ```
)
OPS = re.compile(r"[=<>≤≥≠+\-\*/×÷^]")

def mask_math_spans(s: str) -> str:
    # 只原子化 LaTeX（与标注口径一致）
    out, i = s, 1
    while True:
        m = MATH_LATEX_PAT.search(out)
        if not m: break
        out = out[:m.start()] + f"[MATH_{i}]" + out[m.end():]
        i += 1
    return out

def whitespace_tokens(s: str) -> List[str]:
    return re.findall(r"\S+", s)

def sample_key(question: str, chunks: List[str]) -> Tuple[str,...]:
    # 1) 拼整段（chunk之间至少 1 空格；不加 <CHUNK_SEP>）
    text = " ".join(chunks)
    masked = mask_math_spans(text)

    # 2) 可计算特征
    toks = whitespace_tokens(masked)
    n_tok = len(toks)
    # 长度桶（可按你的分布调整边界）
    if n_tok < 1200: len_bin = "L1"
    elif n_tok < 2000: len_bin = "L2"
    elif n_tok < 2800: len_bin = "L3"
    elif n_tok < 3600: len_bin = "L4"
    else: len_bin = "L5"

    # 数学密度：LaTeX 段数 + 运算符密度（每 80 token 记 1）
    n_ltx = len(MATH_LATEX_PAT.findall(text))
    n_ops = len(OPS.findall(text))
    m = n_ltx + n_ops // 80
    math_bin = "M0" if m==0 else ("M1" if m==1 else ("M2-4" if m<=4 else "M5+"))

    # 口吻信号
    concl = bool(re.search(r"\b(Therefore|Thus|Hence|Answer|\\boxed)\b|所以|综上", text))
    expl  = bool(re.search(r"\b(try|maybe|suppose|let me)\b|尝试|猜测", text))
    cbin = "C1" if concl else "C0"
    ebin = "E1" if expl  else "E0"

    # question 长度
    qlen = len(whitespace_tokens(question or ""))
    qbin = "Qshort" if qlen < 15 else ("Qmid" if qlen < 40 else "Qlong")

    # chunk 数（可选）
    cnum = len(chunks)
    nbin = "N1" if cnum==1 else ("N2-3" if cnum<=3 else "N4+")

    return (len_bin, math_bin, cbin, ebin, qbin, nbin), n_tok

def stratified_pick(jsonl_in: str, jsonl_out: str, target_n: int = 10000, seed: int = 42):
    random.seed(seed)
    buckets: Dict[Tuple[str,...], List[dict]] = defaultdict(list)

    with open(jsonl_in, 'r', encoding='utf-8') as f:
        for ln in f:
            if not ln.strip(): continue
            o = json.loads(ln)
            q = o.get('question') or o.get('original_question') or o.get('problem') or ""
            chunks = o.get('chunk_list') or []
            if not chunks: 
                # 退化：用 ori_cot 拆成 1 段
                cot = o.get('ori_cot') or o.get('original_cot') or ""
                if cot: chunks=[cot]
                else: continue
            key, _ = sample_key(q, chunks)
            buckets[key].append(o)

    # 为每个非空桶分配配额（均匀+按桶大小调和）
    non_empty = [k for k,v in buckets.items() if v]
    K = len(non_empty)
    if K == 0: 
        raise RuntimeError("没有可用样本")
    # 初始均分
    base = target_n // K
    # 细调：大桶多给一点，小桶给到桶容量
    chosen=[]
    remain = target_n
    # 先给每桶 min(base, len)
    for k in non_empty:
        n = min(base, len(buckets[k]))
        picked = random.sample(buckets[k], n)
        chosen.extend(picked); remain -= n
        # 从桶里删掉已选
        left = [x for x in buckets[k] if x not in picked]
        buckets[k] = left
    # 余量按桶剩余大小轮询分配
    pool = [(k, len(v)) for k,v in buckets.items() if v]
    pool.sort(key=lambda x: -x[1])
    i=0
    keys=[k for k,_ in pool]
    while remain>0 and keys:
        k = keys[i % len(keys)]
        if not buckets[k]:
            keys.pop(i % len(keys)); 
            continue
        chosen.append(buckets[k].pop())
        remain -= 1; i += 1

    with open(jsonl_out, 'w', encoding='utf-8') as f:
        for o in chosen:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print(f"采样完成：{len(chosen)} 条样本写入 {jsonl_out}")

if __name__ == "__main__":
    stratified_pick(
        jsonl_in="/Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/camel/camel_chunk_only_cot/chunked_result.jsonl",
        jsonl_out="/Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/API/dataset_split/4k_sampled_data.jsonl",
        target_n=4000,
        seed=42
    )