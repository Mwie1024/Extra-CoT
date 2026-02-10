import json
import re
import sys
from typing import Optional, Dict, Any, List, Tuple

# ---- 可选：更强的数学等价判断支持 ----
HAVE_SYMPY = False
try:
    import sympy as sp
    from sympy.parsing.latex import parse_latex
    HAVE_SYMPY = True
except Exception:
    HAVE_SYMPY = False


# ---------- 抽取标准答案：来自 response 最末行的 "The answer is: xxx" ----------
def extract_correct_from_response(response: str) -> Optional[str]:
    """
    在 response 中查找最后一次出现的 'The answer is: xxx' 并返回 xxx。
    """
    if not isinstance(response, str):
        return None
    # 捕获每一行中的 "The answer is: xxx"
    matches = re.findall(r"The answer is:\s*(.+?)(?=$|\n)", response.strip(), flags=re.IGNORECASE)
    if not matches:
        return None
    return matches[-1].strip()


# ---------- 抽取模型答案：model_output 里最后一个 \boxed{...} 的完整内容 ----------
def extract_last_boxed(text: str) -> Optional[str]:
    """
    返回 text 中最后一次出现的 \boxed{...} 的花括号完整内容，支持嵌套括号。
    """
    if not isinstance(text, str):
        return None
    idx = text.rfind(r"\boxed")
    if idx == -1:
        return None
    # 找到 \boxed 后的第一个 '{'
    brace_start = text.find("{", idx)
    if brace_start == -1:
        return None

    depth = 0
    for i, ch in enumerate(text[brace_start:], start=brace_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start + 1 : i].strip()
    return None  # 没配平


# ---------- 一些 LaTeX/文本清洗 ----------
def strip_latex_wrappers(s: str) -> str:
    if s is None:
        return ""
    s = s.strip()

    # 去掉包裹的 $...$ / \(...\) / \[...\]
    if s.startswith("$") and s.endswith("$"):
        s = s[1:-1].strip()
    if s.startswith(r"\(") and s.endswith(r"\)"):
        s = s[2:-2].strip()
    if s.startswith(r"\[") and s.endswith(r"\]"):
        s = s[2:-2].strip()

    # 常见无关排版
    s = s.replace(r"\left", "").replace(r"\right", "")
    s = s.replace(r"\,", "").replace(r"\!", "").replace(r"\;", "").replace(r"\:", "")

    # 去掉行尾句点/多余空格
    s = s.rstrip(" .，。")
    return s.strip()


def normalize_for_string_eq(s: str) -> str:
    """
    用于最先进行的宽松字符串比较：去空白、部分等价符号、统一大小写。
    """
    if s is None:
        return ""
    s = strip_latex_wrappers(s)

    repl = {
        r"\cdot": "*",
        r"\times": "*",
        r"\div": "/",
        r"\pm": "+/-",
        r"\;": "",
        r"\,": "",
        r"\%": "%",
        "−": "-",  # 减号统一
        "–": "-",
        "—": "-",
        "°": "deg",
        r"\pi": "pi",
    }
    for k, v in repl.items():
        s = s.replace(k, v)

    # 百分号两侧清理
    s = s.replace(" %", "%")

    # 压缩空白
    s = re.sub(r"\s+", "", s)
    return s


# ---------- 数学等价判断（核心） ----------
def try_parse_sympy(expr_str: str):
    """
    使用 SymPy 解析字符串到符号表达式：
    1) 先尝试 LaTeX 解析
    2) 失败则用 sympify 解析普通表达式
    返回解析成功的表达式或 None。
    """
    if not HAVE_SYMPY:
        return None
    s = strip_latex_wrappers(expr_str)

    # 一些常见 LaTeX 转义，方便 sympify 兜底
    s_for_sympify = (
        s.replace(r"\cdot", "*")
        .replace(r"\times", "*")
        .replace(r"\div", "/")
        .replace(r"\pi", "pi")
        .replace("^", "**")
    )

    # \sqrt{...} -> sqrt(...)
    s_for_sympify = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", s_for_sympify)

    # \frac{a}{b} -> (a)/(b)
    def _frac_sub(m):
        return f"(({m.group(1)})/({m.group(2)}))"
    s_for_sympify = re.sub(r"\\(?:d)?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", _frac_sub, s_for_sympify)

    # \pm 暂不直接转，避免歧义；如果有 \pm，基本无法判为“和某单一数值相等”
    try:
        # 先试 LaTeX
        return parse_latex(s)
    except Exception:
        pass
    try:
        return sp.sympify(s_for_sympify, convert_xor=True)
    except Exception:
        return None


def tokens_if_comma_list(s: str) -> Optional[List[str]]:
    """
    若答案是逗号分隔的多项（如多个根/集合），返回分割后的 token 列表（去空白的原始片段）；
    否则返回 None。
    """
    # 粗略判断：存在逗号且不是函数/小数的明显一体情况
    if "," in s:
        # 避免把坐标 (a,b) 当集合拆分，这里保守：有圆括号包裹整体则当成一个表达式
        ss = s.strip()
        if (ss.startswith("(") and ss.endswith(")")) or (ss.startswith(r"\(") and ss.endswith(r"\)")):
            return None
        # 直接按逗号切，用户数据一般干净；必要时可加更智能的括号匹配切分
        parts = [p.strip() for p in ss.split(",") if p.strip() != ""]
        if len(parts) >= 2:
            return parts
    return None


def math_verify(ans_pred: str, ans_gold: str) -> bool:
    """
    判断两答案数学等价：
    - 先做宽松字符串比较（去空白、等价符号）；
    - 若可用 SymPy：解析为表达式，比对 a-b 是否可化简为 0（或数值近似相等）；
    - 若是逗号分隔的多解，做“集合相等”比较（顺序无关）。
    """
    if ans_pred is None or ans_gold is None:
        return False

    a_raw = strip_latex_wrappers(ans_pred)
    b_raw = strip_latex_wrappers(ans_gold)

    # 快速字符串等价（非常宽松）
    a_norm = normalize_for_string_eq(a_raw)
    b_norm = normalize_for_string_eq(b_raw)
    if a_norm == b_norm:
        return True

    # 逗号分隔的多解，按集合比较（每个元素再递归走一次解析）
    a_tokens = tokens_if_comma_list(a_raw)
    b_tokens = tokens_if_comma_list(b_raw)
    if a_tokens is not None and b_tokens is not None and len(a_tokens) == len(b_tokens):
        # 用 SymPy（若可用）做元素级等价；否则用字符串宽松等价
        matched = [False] * len(b_tokens)
        for at in a_tokens:
            ok = False
            for j, bt in enumerate(b_tokens):
                if matched[j]:
                    continue
                if HAVE_SYMPY:
                    ea = try_parse_sympy(at)
                    eb = try_parse_sympy(bt)
                    if ea is not None and eb is not None:
                        try:
                            if (ea - eb).simplify() == 0:
                                matched[j] = True
                                ok = True
                                break
                            # 数值近似
                            if ea.is_number and eb.is_number and abs(sp.N(ea) - sp.N(eb)) < sp.nsimplify("1e-9"):
                                matched[j] = True
                                ok = True
                                break
                        except Exception:
                            pass
                # 兜底：宽松字符串比较
                if normalize_for_string_eq(at) == normalize_for_string_eq(bt):
                    matched[j] = True
                    ok = True
                    break
            if not ok:
                return False
        return all(matched)

    # 单表达式比较
    if HAVE_SYMPY:
        ea = try_parse_sympy(a_raw)
        eb = try_parse_sympy(b_raw)
        if ea is not None and eb is not None:
            try:
                diff = sp.simplify(ea - eb)
                if diff == 0:
                    return True
            except Exception:
                pass
            # 数值近似（仅在均为数值时）
            try:
                if ea.is_number and eb.is_number and abs(sp.N(ea) - sp.N(eb)) < sp.nsimplify("1e-9"):
                    return True
            except Exception:
                pass

    # 兜底再做一次极简规整后字符串对比
    a_last = a_norm.strip().rstrip(".")
    b_last = b_norm.strip().rstrip(".")
    return a_last == b_last


# ---------- 主评测逻辑 ----------
def evaluate_jsonl(jsonl_path: str) -> Dict[str, Any]:
    total = 0                 # 行数（包括无法评估的）
    evaluable = 0             # 可评估样本数（两侧都成功抽到答案）
    correct = 0               # 命中数
    bad_lines: List[Tuple[int, str]] = []  # 记录无法评估的行（原因）

    # breakpoint()

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1

            try:
                obj = json.loads(line)
            except Exception as e:
                bad_lines.append((ln, f"JSON parse error: {e}"))
                continue

            response = obj.get("response", "")
            model_output = obj.get("model_output", "")
            answer = obj.get("answer", "")

            if answer == "":
                gold = extract_correct_from_response(response)
            else:
                gold = answer
            pred = extract_last_boxed(model_output)

            if gold is None or pred is None:
                reason = []
                if gold is None:
                    reason.append("no_gold(The answer is: ...)")
                if pred is None:
                    reason.append(r"no_pred(\boxed{...})")
                bad_lines.append((ln, " & ".join(reason)))
                continue

            evaluable += 1
            if math_verify(pred, gold):
                correct += 1

    acc = (correct / evaluable) if evaluable > 0 else 0.0
    return {
        "total_lines": total,
        "evaluable": evaluable,
        "correct": correct,
        "accuracy": acc,
        "unscored_examples": bad_lines,
    }


# ---------- 命令行入口 ----------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python evaluate_jsonl.py path/to/file.jsonl")
        sys.exit(1)
    path = sys.argv[1]
    result = evaluate_jsonl(path)
    print(f"总行数: {result['total_lines']}")
    print(f"可评估: {result['evaluable']}")
    print(f"正确数: {result['correct']}")
    print(f"正确率: {result['accuracy']:.4f}")
    if result["unscored_examples"]:
        print("\n以下行未计入（未成功抽取标准/模型答案）：")
        for ln, why in result["unscored_examples"][:20]:  # 只展示前 20 条
            print(f"  - 行 {ln}: {why}")
