#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, re, sys
from typing import Dict, Any, Iterable, Optional, List, Union
from fractions import Fraction

# =============== IO ===============
def iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception as e:
                print(f"[WARN] {path}:{lineno} 解析失败: {e}", file=sys.stderr)

def write_jsonl(path: str, rows: Iterable[Dict[str, Any]]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        for obj in rows:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

# =============== utils ===============
def get_first(obj: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in obj and obj[k] is not None:
            v = obj[k]
            if isinstance(v, str):
                if v.strip():
                    return v
            else:
                return v
    return None

def normalize_id(v: Any, case_insensitive: bool = False) -> str:
    s = str(v).strip()
    return s.lower() if case_insensitive else s

# =============== 从 A.response 提取答案 ===============
PAT = re.compile(r"the\s+answer\s+is\s*:?\s*(.*)\s*$", re.IGNORECASE | re.DOTALL)

def extract_answer_from_response(text: str) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    matches = list(PAT.finditer(text))
    if not matches:
        return None
    ans = matches[-1].group(1).strip()
    return clean_answer(ans)

def clean_answer(s: str) -> str:
    t = s.strip()
    if t.startswith(("'", '"', "$")) and t.endswith(("'", '"', "$")) and len(t) >= 2:
        t = t[1:-1].strip()
    t = re.sub(r"[\.!\?]+$", "", t).strip()
    return t

# =============== 数值比较辅助 ===============
def parse_mixed_fraction(s: str) -> Optional[Fraction]:
    m = re.fullmatch(r"\s*([+-]?\d+)\s+(\d+)\s*/\s*(\d+)\s*", s)
    if not m: return None
    whole, num, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = Fraction(num, den)
    return Fraction(whole, 1) + (frac if whole >= 0 else -frac)

def parse_fraction(s: str) -> Optional[Fraction]:
    m = re.fullmatch(r"\s*([+-]?\d+)\s*/\s*(\d+)\s*", s)
    if not m: return None
    num, den = int(m.group(1)), int(m.group(2))
    if den == 0: return None
    return Fraction(num, den)

def parse_number(s: str) -> Optional[Union[Fraction, float]]:
    t = s.strip().replace(",", "")
    if t.startswith("$") and t.endswith("$") and len(t) >= 2:
        t = t[1:-1].strip()
    mf = parse_mixed_fraction(t)
    if mf is not None: return mf
    fr = parse_fraction(t)
    if fr is not None: return fr
    if re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?\s*", t):
        return float(t) if "." in t else Fraction(int(t), 1)
    return None

def answers_equal(gold: str, pred: str, tol: float = 1e-6, numeric_on: bool = True) -> bool:
    gold_c = clean_answer(gold)
    pred_c = clean_answer(str(pred))
    if numeric_on:
        gnum, pnum = parse_number(gold_c), parse_number(pred_c)
        if gnum is not None and pnum is not None:
            if isinstance(gnum, Fraction) and isinstance(pnum, Fraction):
                return gnum == pnum
            try:
                return abs(float(gnum) - float(pnum)) <= tol
            except Exception:
                pass
    # 回退到字符串（忽略大小写 + 折叠空白）
    def norm(x: str) -> str:
        return re.sub(r"\s+", " ", x.strip()).lower()
    return norm(gold_c) == norm(pred_c)

# =============== 构建金标（按 id） ===============
def build_gold_by_id(a_path: str,
                     a_id_keys: List[str],
                     a_resp_keys: List[str],
                     id_case_insensitive: bool) -> Dict[str, str]:
    gold = {}
    miss_id = miss_resp = miss_ans = 0
    for obj in iter_jsonl(a_path):
        _id = get_first(obj, a_id_keys)
        if _id is None:
            miss_id += 1; continue
        resp = get_first(obj, a_resp_keys)
        if not isinstance(resp, str) or not resp.strip():
            miss_resp += 1; continue
        ans = extract_answer_from_response(resp)
        if ans is None:
            miss_ans += 1; continue
        key = normalize_id(_id, id_case_insensitive)
        if key not in gold:           # 同一 id 多次，取首次
            gold[key] = ans
    print(f"[INFO] A 金标：{len(gold)} 条（无id={miss_id}, 无response={miss_resp}, 未抽到答案={miss_ans}）", file=sys.stderr)
    return gold

# =============== 评估 B（按 id 对齐） ===============
def evaluate_by_id(b_path: str,
                   gold: Dict[str, str],
                   b_id_keys: List[str],
                   b_pred_keys: List[str],
                   id_case_insensitive: bool,
                   tol: float,
                   numeric_on: bool,
                   out_errors: Optional[str]):
    total = aligned = compared = correct = 0
    missing_pred = 0
    errors = []

    for obj in iter_jsonl(b_path):
        total += 1
        _id = get_first(obj, b_id_keys)
        if _id is None:
            continue
        key = normalize_id(_id, id_case_insensitive)
        gold_ans = gold.get(key)
        if gold_ans is None:
            continue
        aligned += 1

        pred = get_first(obj, b_pred_keys)
        # 缺失预测 -> 计为错误
        if pred is None or (isinstance(pred, str) and not str(pred).strip()):
            compared += 1
            missing_pred += 1
            if out_errors:
                errors.append({"id": _id, "gold": gold_ans, "prediction": pred, "reason": "missing_prediction"})
            continue

        compared += 1
        ok = answers_equal(gold_ans, str(pred), tol=tol, numeric_on=numeric_on)
        if ok:
            correct += 1
        else:
            if out_errors:
                errors.append({"id": _id, "gold": gold_ans, "prediction": pred})

    acc = (correct / compared) if compared else 0.0
    print("\n===== 评估结果（按 id 对齐）=====")
    print(f"B 总条数        : {total}")
    print(f"已对齐到金标   : {aligned}")
    print(f"参与比较条数   : {compared}  （缺失预测 {missing_pred}）")
    print(f"预测正确条数   : {correct}")
    print(f"总体准确率     : {acc:.6f}")
    print("================================\n")

    if out_errors:
        write_jsonl(out_errors, errors)
        print(f"[INFO] 错误样例已写出：{out_errors}", file=sys.stderr)

# =============== main ===============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="A.jsonl（含 id/response）")
    ap.add_argument("--b", required=True, help="B.jsonl（含 id/prediction）")
    ap.add_argument("--out-errors", default="", help="可选：错误样例导出 JSONL")

    # 字段名（含常见别名）
    ap.add_argument("--a-id-key", default="id")
    ap.add_argument("--a-id-alt", default="sample_id,qid,question_id,uid,example_id,problem_id")
    ap.add_argument("--a-response-key", default="response")
    ap.add_argument("--a-response-alt", default="output,answer,text,completion")

    ap.add_argument("--b-id-key", default="id")
    ap.add_argument("--b-id-alt", default="sample_id,qid,question_id,uid,example_id,problem_id")
    ap.add_argument("--b-pred-key", default="prediction")
    ap.add_argument("--b-pred-alt", default="pred,output,answer,response,text")

    # 匹配/比较选项
    ap.add_argument("--id-case-insensitive", action="store_true",
                    help="id 匹配不区分大小写（默认区分）")
    ap.add_argument("--tol", type=float, default=1e-6, help="数值比较容差（默认 1e-6）")
    ap.add_argument("--numeric-off", action="store_true", help="关闭数值比较，仅做字符串比较")

    args = ap.parse_args()

    a_id_keys   = [args.a_id_key] + [k.strip() for k in args.a_id_alt.split(",") if k.strip()]
    a_resp_keys = [args.a_response_key] + [k.strip() for k in args.a_response_alt.split(",") if k.strip()]
    b_id_keys   = [args.b_id_key] + [k.strip() for k in args.b_id_alt.split(",") if k.strip()]
    b_pred_keys = [args.b_pred_key] + [k.strip() for k in args.b_pred_alt.split(",") if k.strip()]

    print("[INFO] A.id keys:", a_id_keys, file=sys.stderr)
    print("[INFO] A.response keys:", a_resp_keys, file=sys.stderr)
    print("[INFO] B.id keys:", b_id_keys, file=sys.stderr)
    print("[INFO] B.prediction keys:", b_pred_keys, file=sys.stderr)

    gold = build_gold_by_id(
        args.a, a_id_keys, a_resp_keys,
        id_case_insensitive=args.id_case_insensitive
    )
    evaluate_by_id(
        args.b, gold, b_id_keys, b_pred_keys,
        id_case_insensitive=args.id_case_insensitive,
        tol=args.tol, numeric_on=not args.numeric_off,
        out_errors=args.out_errors if args.out_errors else None
    )

if __name__ == "__main__":
    main()
