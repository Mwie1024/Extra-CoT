#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, argparse
from typing import List, Dict, Any, Optional, Tuple

# -------- 可选：SymPy（若安装则更稳） --------
HAVE_SYMPY = False
try:
    import sympy as sp
    from sympy.parsing.latex import parse_latex
    HAVE_SYMPY = True
except Exception:
    HAVE_SYMPY = False

EPS = 1e-9

# ===================== 抽取最后一个 \boxed{...}（支持嵌套） =====================
def extract_last_boxed(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    idx_boxed = text.rfind(r"\boxed")
    idx_fbox  = text.rfind(r"\fbox")
    idx = max(idx_boxed, idx_fbox)
    if idx == -1:
        return None
    i = idx
    while i < len(text) and text[i] != "{":
        i += 1
    if i >= len(text) or text[i] != "{":
        brace_start = text.find("{", idx)
    else:
        brace_start = i
    if brace_start == -1:
        return None
    depth = 0
    for j, ch in enumerate(text[brace_start:], start=brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : j].strip()
    return None

# ===================== 轻量 LaTeX 清洗 =====================
def strip_latex_wrappers(s: str) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    if s.startswith("$$") and s.endswith("$$"):
        s = s[2:-2].strip()
    if s.startswith("$") and s.endswith("$"):
        s = s[1:-1].strip()
    if s.startswith(r"\(") and s.endswith(r"\)"):
        s = s[2:-2].strip()
    if s.startswith(r"\[") and s.endswith(r"\]"):
        s = s[2:-2].strip()
    s = s.replace(r"\left", "").replace(r"\right", "")
    s = s.replace(r"\,", "").replace(r"\!", "").replace(r"\;", "").replace(r"\:", "")
    return s.strip()

# ===================== 数学等价判定（SymPy 可选） =====================
def _split_top_level_commas(s: str) -> Optional[List[str]]:
    if not isinstance(s, str):
        s = str(s)
    ss = s.strip()
    if len(ss) >= 2 and ((ss[0], ss[-1]) in {("(", ")"), ("[", "]")}):
        return None

    parts, buf = [], []
    p = b = c = 0
    i, n = 0, len(ss)
    while i < n:
        ch = ss[i]
        if ch == "\\":
            buf.append(ch)
            i += 1
            while i < n and ss[i].isalpha():
                buf.append(ss[i]); i += 1
            continue
        if ch == "(": p += 1
        elif ch == ")": p = max(0, p-1)
        elif ch == "[": b += 1
        elif ch == "]": b = max(0, b-1)
        elif ch == "{": c += 1
        elif ch == "}": c = max(0, c-1)
        elif ch == "," and p == b == c == 0:
            parts.append("".join(buf).strip()); buf.clear(); i += 1; continue
        buf.append(ch); i += 1

    if parts:
        parts.append("".join(buf).strip())
        parts = [x for x in parts if x]
        if len(parts) >= 2:
            return parts
    return None

def _latex_to_plain_basic(s: str) -> str:
    s = strip_latex_wrappers(s)
    s = s.replace("\u2212","-").replace("−","-")
    s = re.sub(r"\\(?:[dt])?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\(left|right|,|;|!|:)", "", s)
    s = s.strip()
    if "=" in s:
        s = s.split("=")[-1].strip()
    return s

_NUM_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_FRAC_RE = re.compile(r"^[+-]?\d+\s*/\s*[+-]?\d+$")
_PCT_RE  = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")

def _to_numeric(s: str) -> Tuple[str, Any]:
    t = s.strip()
    if not t: return ("string","")
    if _PCT_RE.match(t): return ("percent", float(t[:-1]))
    if _FRAC_RE.match(t.replace(" ", "")):
        a,b = t.replace(" ","").split("/")
        try: return ("fraction", (float(a), float(b)))
        except: return ("string", t.lower())
    if _NUM_RE.match(t):
        try: return ("number", float(t))
        except: return ("string", t.lower())
    return ("string", t.lower())

def try_parse_sympy(expr_str: str):
    if not HAVE_SYMPY:
        return None
    s = strip_latex_wrappers(expr_str)
    s_for_sympify = (s.replace(r"\cdot","*").replace(r"\times","*").replace("×","*")
                       .replace(r"\div","/").replace("÷","/")
                       .replace(r"\pi","pi").replace("π","pi")
                       .replace("^","**"))
    s_for_sympify = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", s_for_sympify)
    s_for_sympify = re.sub(r"\\(?:[dt])?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"((\1)/(\2))", s_for_sympify)
    try:
        return parse_latex(s)
    except Exception:
        pass
    try:
        return sp.sympify(s_for_sympify, convert_xor=True)
    except Exception:
        return None

def sympy_equal(ea, eb) -> bool:
    try:
        if ea == eb:
            return True
        if HAVE_SYMPY and isinstance(ea, sp.Expr) and isinstance(eb, sp.Expr):
            try:
                if sp.simplify(ea - eb) == 0:
                    return True
            except Exception:
                pass
            try:
                if ea.is_number and eb.is_number:
                    if abs(float(sp.N(ea)) - float(sp.N(eb))) < EPS:
                        return True
            except Exception:
                pass
            try:
                eq = ea.equals(eb)
                if eq is True:
                    return True
            except Exception:
                pass
        if HAVE_SYMPY and isinstance(ea, sp.Set) and isinstance(eb, sp.Set):
            try:
                return bool(ea.equals(eb))
            except Exception:
                return False
    except Exception:
        return False
    return False

def math_verify(pred_raw: str, gold_raw: str) -> bool:
    if pred_raw is None or gold_raw is None:
        return False

    def _norm(s: str) -> str:
        s = strip_latex_wrappers(s)
        s = (s.replace(r"\cdot","*").replace(r"\times","*").replace("×","*")
               .replace(r"\div","/").replace("÷","/")
               .replace(r"\pi","pi").replace("π","pi")
               .replace("−","-").replace("–","-").replace("—","-"))
        s = re.sub(r"\s+","",s)
        return s.rstrip(".")
    if _norm(pred_raw) == _norm(gold_raw):
        return True

    a_tokens = _split_top_level_commas(pred_raw)
    b_tokens = _split_top_level_commas(gold_raw)
    if a_tokens is not None and b_tokens is not None and len(a_tokens) == len(b_tokens):
        used = [False]*len(b_tokens)
        for at in a_tokens:
            ok = False
            for j, bt in enumerate(b_tokens):
                if used[j]: continue
                if HAVE_SYMPY:
                    ea = try_parse_sympy(at); eb = try_parse_sympy(bt)
                    if ea is not None and eb is not None and sympy_equal(ea, eb):
                        used[j]=True; ok=True; break
                if _norm(at) == _norm(bt):
                    used[j]=True; ok=True; break
                ua = _latex_to_plain_basic(at); ub = _latex_to_plain_basic(bt)
                ka,va = _to_numeric(ua); kb,vb = _to_numeric(ub)
                try:
                    if   ka=="fraction" and kb=="fraction" and abs(va[0]/va[1]-vb[0]/vb[1])<EPS: used[j]=True; ok=True; break
                    elif ka=="fraction" and kb=="number"   and abs(va[0]/va[1]-vb)         <EPS: used[j]=True; ok=True; break
                    elif ka=="number"   and kb=="fraction" and abs(vb[0]/vb[1]-va)         <EPS: used[j]=True; ok=True; break
                    elif ka=="number"   and kb=="number"   and abs(va-vb)                  <EPS: used[j]=True; ok=True; break
                    elif ka=="percent"  and kb=="percent"  and abs(va-vb)              <100*EPS: used[j]=True; ok=True; break
                    elif ka=="percent"  and kb=="number"   and abs(va/100.0-vb)           <EPS: used[j]=True; ok=True; break
                    elif ka=="number"   and kb=="percent"  and abs(va-vb/100.0)           <EPS: used[j]=True; ok=True; break
                except Exception:
                    pass
            if not ok:
                return False
        return all(used)

    if HAVE_SYMPY:
        ea = try_parse_sympy(pred_raw); eb = try_parse_sympy(gold_raw)
        if ea is not None and eb is not None and sympy_equal(ea, eb):
            return True

    ua = _latex_to_plain_basic(pred_raw)
    ub = _latex_to_plain_basic(gold_raw)
    ka,va = _to_numeric(ua); kb,vb = _to_numeric(ub)
    try:
        if   ka=="fraction" and kb=="fraction": return abs(va[0]/va[1]-vb[0]/vb[1])<EPS
        elif ka=="fraction" and kb=="number":   return abs(va[0]/va[1]-vb)         <EPS
        elif ka=="number"   and kb=="fraction": return abs(vb[0]/vb[1]-va)         <EPS
        elif ka=="number"   and kb=="number":   return abs(va-vb)                  <EPS
        elif ka=="percent"  and kb=="percent":  return abs(va-vb)              <100*EPS
        elif ka=="percent"  and kb=="number":   return abs(va/100.0-vb)           <EPS
        elif ka=="number"   and kb=="percent":  return abs(va-vb/100.0)           <EPS
    except Exception:
        pass

    return _norm(ua) == _norm(ub)

# ===================== 读/写与数据集索引 =====================
def load_json_or_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()
    try:
        obj = json.loads(txt)
        if isinstance(obj, list):
            return obj
        if isinstance(obj, dict):
            for k in ("data","results","items","rows"):
                if k in obj and isinstance(obj[k], list):
                    return obj[k]
            return [obj]
    except Exception:
        pass
    out = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln: continue
        try:
            o = json.loads(ln)
            if isinstance(o, dict): out.append(o)
        except Exception:
            continue
    return out

def write_json(items: List[Dict[str, Any]], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def load_dataset_index(dataset_path: str) -> Dict[str, Dict[str, Any]]:
    """把 all_stem.jsonl 按 id 建索引"""
    records = load_json_or_jsonl(dataset_path)
    idx = {}
    for r in records:
        rid = r.get("id") or r.get("question_id") or r.get("qid")
        if rid: idx[rid] = r
    return idx

def get_answer_content_from_rec(stem_rec: Dict[str, Any]) -> Optional[str]:
    """从数据集条目里尽可能拿到 answer_content（兼容若字段名略有不同）"""
    for k in ["answer_content", "answer_text", "gold_content", "gold", "reference_answer"]:
        if k in stem_rec and isinstance(stem_rec[k], (str, int, float)):
            return str(stem_rec[k])
    # 如果只有选项与正确项字母，也尽力还原：
    # 例如 {"options":["2","3","4","5"], "answer":"C"}
    options = stem_rec.get("options") or stem_rec.get("choices") or stem_rec.get("answers")
    ans_letter = stem_rec.get("answer")
    if isinstance(options, list) and isinstance(ans_letter, str):
        letter = normalize_option_token(ans_letter)
        if letter is not None:
            idx = ord(letter) - ord('A')
            if 0 <= idx < len(options):
                return str(options[idx])
    return None

# ===================== 选项字母判定 =====================
_OPTION_LETTERS = set(list("ABCDEFGHIJ"))

def normalize_option_token(s: Any) -> Optional[str]:
    """把 'B' / '(B)' / '选项B' / 'option:B' / 'Choice b.' 等归一为 'B'；否则返回 None。"""
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    t = s.strip()
    if not t:
        return None
    t = t.replace(" ", "")
    t = t.replace("答案", "").replace("选项", "").replace("选", "")
    t = re.sub(r"(?i)^(answer|ans|option|choice)[:：]?", "", t)
    # 单字母或被括起来、带点的情况
    m = re.match(r"^[\(\[\{]?\s*([A-Ja-j])\s*[\)\]\}]?\.?$", t)
    if m:
        return m.group(1).upper()
    return None

# ===================== 核心：重新评估并用 answer_content 兜底 =====================
def recheck_file(path: str, dataset_idx: Dict[str, Dict[str, Any]], inplace: bool=False) -> Tuple[int,int,int,int,int]:
    """
    返回 (总数, 修正为 True 的样本数, 预测有变更数, 最终正确数, 用content修正为True数)
    """
    records = load_json_or_jsonl(path)
    if not records:
        print(f"[warn] empty or unreadable: {path}")
        return (0,0,0,0,0)

    total = len(records)
    fixed_true = 0
    pred_changed = 0
    final_correct = 0
    content_fixed_true = 0

    for rec in records:
        rid   = rec.get("id", "")
        mo    = rec.get("model_output","") or ""
        gold  = rec.get("answer","") or ""
        old_pred = rec.get("prediction","") or ""
        old_acc  = bool(rec.get("accuracy", False))

        # 优先用最后一个 \boxed{...} 提取；若没有再用原 prediction
        new_pred = extract_last_boxed(mo) or old_pred
        new_pred = str(new_pred) if new_pred is not None else ""
        rec["prediction"] = new_pred
        rec["has_pred"]   = bool(new_pred)

        # 两条路径：
        # 1) 预测是选项字母 -> 直接与 gold（选项字母）比
        # 2) 预测不是字母  -> 若 gold 是字母，则用数据集 answer_content 与预测做 math_verify
        pred_opt = normalize_option_token(new_pred)
        gold_opt = normalize_option_token(gold)

        new_acc = False
        used_answer_content = False

        if pred_opt is not None:
            # 预测就是选项字母
            new_acc = (gold_opt is not None and pred_opt == gold_opt)
        else:
            # 预测是具体值/表达式
            if gold_opt is not None:
                # gold 是字母，则尝试用数据集的 answer_content 兜底
                stem_rec = dataset_idx.get(rid)
                content = get_answer_content_from_rec(stem_rec) if stem_rec else None
                if content:
                    new_acc = math_verify(new_pred, str(content))
                    used_answer_content = True
                else:
                    # 找不到 content 时，退化为与 gold 直接比较（有些数据集 gold 就是内容）
                    new_acc = math_verify(new_pred, gold)
            else:
                # gold 不是字母，直接用 math_verify
                new_acc = math_verify(new_pred, gold)

        # 统计
        if new_acc and not old_acc:
            fixed_true += 1
        if (new_pred or "") != (old_pred or ""):
            pred_changed += 1
        if new_acc:
            final_correct += 1
        if new_acc and used_answer_content and not old_acc:
            content_fixed_true += 1

        # 回写
        rec["accuracy"] = bool(new_acc)
        if used_answer_content:
            rec["verified_by_answer_content"] = True

    out_path = path if inplace else (os.path.splitext(path)[0] + ".rechecked.json")
    write_json(records, out_path)
    print(f"[ok] {path} -> {out_path} | total={total} fixed_to_true={fixed_true} "
          f"pred_changed={pred_changed} final_correct={final_correct} content_fixed_true={content_fixed_true}")
    return (total, fixed_true, pred_changed, final_correct, content_fixed_true)

def walk_and_recheck(root: str, dataset_idx: Dict[str, Dict[str, Any]], inplace: bool=False):
    if os.path.isfile(root):
        recheck_file(root, dataset_idx, inplace=inplace)
        return
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn == "prediction.json":  # 只处理你的评测输出
                recheck_file(os.path.join(dirpath, fn), dataset_idx, inplace=inplace)

# ===================== CLI =====================
def main():
    ap = argparse.ArgumentParser(description="Re-check predictions with \\boxed extraction + math verify; use answer_content when pred is not a choice letter.")
    ap.add_argument("--path", required=True, help="prediction.json 文件或包含它们的目录")
    ap.add_argument("--dataset", default="/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/all_stem.jsonl",
                    help="题库 all_stem.jsonl 的路径（用于按 id 取 answer_content）")
    ap.add_argument("--inplace", action="store_true", help="就地覆盖原文件（默认写 *.rechecked.json）")
    args = ap.parse_args()

    dataset_idx = load_dataset_index(args.dataset)
    print(f"[info] loaded dataset index: {len(dataset_idx)} items from {args.dataset}")
    walk_and_recheck(args.path, dataset_idx, inplace=args.inplace)

if __name__ == "__main__":
    main()
