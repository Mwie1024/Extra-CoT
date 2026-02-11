# filter_correct_jsonl_last_boxed.py
# 用法示例：
#   python filter_correct_jsonl_last_boxed.py \
#       --input data.jsonl \
#       --output correct.jsonl \
#       --keep-keys id,prompt,model_output,pred_extracted,gt_extracted,is_correct \
#       --meta-out meta.json \
#       --response-key response \
#       --output-key model_output
#

import argparse
import json
import re
from typing import Optional, Dict, Any, List

# ---------- 解析工具 ----------

def extract_gt_from_response(response: str) -> Optional[str]:
    """
    从 response 的末尾解析 ground truth。
    固定格式：\\nThe answer is: xxx.
    仅提取 xxx（去掉末尾句点）。
    """
    m = re.search(r"The answer is:\s*(.+?)\s*$", response, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return m.group(1).strip()

def extract_last_boxed_expr(text: str) -> Optional[str]:
    """
    从右往左，仅提取 **最后一个** \\boxed{...} 的内容。
    使用括号计数以支持内部包含花括号（如 \\frac{1}{2}）。
    """
    if not isinstance(text, str):
        return None
    needle = r'\boxed{'
    start = text.rfind(needle)
    if start == -1:
        return None

    i = start + len(needle)
    depth = 1
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start + len(needle): i].strip()
        i += 1
    return None  # 未成功配对

# ---------- math_verify 判等 ----------

def get_math_verify():
    try:
        from math_verify import parse, verify  # type: ignore
        return parse, verify
    except Exception as e:
        raise RuntimeError(
            "需要安装并可导入 'math_verify' 以进行答案等价判定。"
        ) from e

def math_equal(pred_expr: str, gt_expr: str, parse, verify) -> bool:
    return verify(parse(pred_expr), parse(gt_expr))

# ---------- 仅保留指定键 ----------

def build_selected_record(
    original: Dict[str, Any],
    keep_keys: List[str],
    derived: Dict[str, Any],
    fill_missing_with_none: bool = True,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in keep_keys:
        if k in derived:
            out[k] = derived[k]
        elif k in original:
            out[k] = original[k]
        elif fill_missing_with_none:
            out[k] = None
    return out

# ---------- 主流程 ----------

def process_jsonl(
    input_path: str,
    output_path: str,
    keep_keys: List[str],
    response_key: str = "response",
    output_key: str = "model_output",
    meta_out: Optional[str] = None,
    encoding: str = "utf-8",
) -> Dict[str, Any]:
    parse, verify = get_math_verify()

    total = 0
    valid_json = 0
    bad_json = 0
    missing_gt = 0
    missing_pred = 0
    verify_errors = 0
    evaluated = 0
    correct = 0
    incorrect = 0

    with open(input_path, "r", encoding=encoding) as fin, \
         open(output_path, "w", encoding=encoding) as fout:

        for line in fin:
            total += 1
            line = line.strip()
            if not line:
                continue

            try:
                ex = json.loads(line)
                valid_json += 1
            except Exception:
                bad_json += 1
                continue

            resp = ex.get(response_key, "")
            out = ex.get(output_key, "")

            # breakpoint()

            # 1) 解析 gt（末尾固定格式）
            gt = extract_gt_from_response(resp) if isinstance(resp, str) else None
            if gt is None:
                missing_gt += 1
                continue

            # 2) 解析预测：只取 model_output 中 **最后一个** \boxed{...}
            pred = extract_last_boxed_expr(out)
            if pred is None:
                missing_pred += 1
                continue

            # 3) 判等
            is_correct = False
            try:
                is_correct = math_equal(pred, gt, parse, verify)
                evaluated += 1
            except Exception:
                verify_errors += 1
                evaluated += 1
                is_correct = False

            # 4) 写出正确样本（只保留指定键）
            if is_correct:
                correct += 1
                derived = {
                    "pred_extracted": pred,
                    "gt_extracted": gt,
                    "is_correct": True,
                }
                selected = build_selected_record(ex, keep_keys, derived)
                fout.write(json.dumps(selected, ensure_ascii=False) + "\n")
            else:
                incorrect += 1

    meta = {
        "total_lines": total,
        "valid_json": valid_json,
        "bad_json_lines": bad_json,
        "evaluated": evaluated,          # 成功进行 verify 的条数
        "correct": correct,
        "incorrect": incorrect,
        "missing_gt": missing_gt,
        "missing_pred": missing_pred,
        "verify_errors": verify_errors,
        "accuracy_on_evaluated": (correct / evaluated) if evaluated else 0.0,
        "accuracy_on_total": (correct / total) if total else 0.0,
        "response_key": response_key,
        "output_key": output_key,
        "keep_keys": keep_keys,
    }

    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta_out:
        with open(meta_out, "w", encoding=encoding) as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    return meta

def parse_args():
    ap = argparse.ArgumentParser(description="筛选 JSONL 中模型输出正确的样本（只取 model_output 的最后一个 \\boxed{...} 与 gt 对比）")
    ap.add_argument("--input", required=True, help="输入 JSONL 文件路径")
    ap.add_argument("--output", required=True, help="输出 JSONL（仅包含答对样本，且只保留指定键）")
    ap.add_argument("--keep-keys", required=True,
                    help="逗号分隔的键名列表；如需输出派生字段，请包含 pred_extracted,gt_extracted,is_correct")
    ap.add_argument("--response-key", default="response",
                    help="存放 gt（末尾固定格式 The answer is: xxx.）的字段名，默认 response")
    ap.add_argument("--output-key", default="model_output",
                    help="存放模型输出的字段名（只取最后一个 \\boxed{...}），默认 model_output")
    ap.add_argument("--meta-out", default=None,
                    help="可选：将统计信息写入此文件（JSON）")
    ap.add_argument("--encoding", default="utf-8", help="文件编码（默认 utf-8）")
    return ap.parse_args()

if __name__ == "__main__":
    args = parse_args()
    keep_keys = [k.strip() for k in args.keep_keys.split(",") if k.strip()]
    process_jsonl(
        input_path=args.input,
        output_path=args.output,
        keep_keys=keep_keys,
        response_key=args.response_key,
        output_key=args.output_key,
        meta_out=args.meta_out,
        encoding=args.encoding,
    )
