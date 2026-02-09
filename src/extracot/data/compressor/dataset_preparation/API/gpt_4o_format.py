
import re
from typing import Tuple, Dict, List, Optional

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
        # 连续替换直到该模式不再匹配
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

# ---- 2) 空白分词并编号（index<TAB>token） ----
def whitespace_tokens(s: str) -> List[str]:
    # 不做词形变化，不改顺序；忠实于原文本（含标点黏连）
    return re.findall(r"\S+", s)

def enumerate_tokens(tokens: List[str]) -> str:
    return "\n".join(f"{i}\t{tok}" for i, tok in enumerate(tokens, 1))

# ---- 3) 构造要给 GPT-4o 的 System / User 提示 ----
def build_gpt4o_chunk(question: str, s: str, cap: Optional[float] = 0.55):
    """
    cap: 最大删除占比上限（如 0.55 表示 ≤55%）。设为 None 则不在提示中写删除上限。
    返回: system_text, user_text, aux({'tokens':..., 'math_map':...})
    """
    masked, math_map = mask_math_spans(s)
    tokens = whitespace_tokens(masked)
    indexed_block = enumerate_tokens(tokens)
    N = len(tokens)

#     long_prompt = """Select contiguous index ranges to KEEP so that, after compression,
# the text still presents a complete, third‑person solution path
# SPECIFIC TO THE QUESTION. You ONLY select indices; you do NOT rewrite any text.
# Think through the question internally, but output STRICT JSON only.

# Rules — Question‑Grounded Selection
# 1) Target binding (must-have): Keep the minimal spans that (a) define or isolate the target quantity asked by the question,
#    (b) substitute the concrete givens from the question into formulas, and (c) perform the decisive step(s) that lead to the final form
#    requested by the question (e.g., simplified value / inequality direction / proof conclusion / existence check).

# 2) Evidence alignment: Prefer spans that tie to the question’s entities:
#    - numbers/constants appearing in the question (e.g., 4, 7, −3; bounds, coefficients, parameters);
#    - the target variable(s)/object(s) named or implied in the question (e.g., det(A), x, prime(n), P(A));
#    - operation or concept explicitly required by the question (determinant/limit/derivative/factorization/probability…).
#    If a span does not link to these, drop it unless it is strictly needed for correctness (domain/restriction/case condition).

# 3) Mathematical integrity:
#    - Treat each [MATH_k] token as ATOMIC: if kept, keep the entire token; do not split it.
#    - Keep relation symbols and negations ( =, ≠, <, >, ≤, ≥, “not”, “non‑”, “mod”, “divisible by”, etc.).
#    - Keep minimal domain/legality checks only when relevant to the question (e.g., “x>0” before sqrt/log; parity when divisibility is asked).

# 4) De‑duplication and irrelevance:
#    - If multiple repeated or equivalent equations exist, keep the earliest correct one that is actually used later; drop restatements.
#    - Remove first‑person narration, hedges/fillers, meta‑commentary, or explorations not used in the final path for this question.

# 5) Span shape and ordering:
#    - Choose COMPLETE operation clauses (verb + object/complement), e.g., “apply ad−bc”, “substitute a=4,b=7,…”, “isolate x”.
#    - Ranges must be ascending and non‑overlapping. Multi‑token formulas or equations should be covered by a single contiguous range.

# 6) Tie‑breakers (when unsure):
#    - Prefer the span that includes the target symbol or the final substitution step.
#    - Prefer shorter spans that still preserve correctness over longer narrative spans.

# Question (use it to decide relevance):
# {question}

# Candidates (index<TAB>token):
# {indexed_block}

# Output format (STRICT JSON ONLY; zero‑pad to the width shown in the candidates):
# {{"ranges": ["005-012","030-037"]}}
#     """
    prompt = """Task
Select contiguous index ranges to KEEP so the compressed text still shows a complete,
third‑person solution path SPECIFIC TO THE QUESTION. Output STRICT JSON only.

Rules (question‑grounded)
- Target steps: Keep minimal spans that (a) define/isolate the target asked, (b) substitute givens
  from the question, (c) execute decisive steps to the required final form.
- Link to question: Prefer spans tied to the question’s numbers/variables/entities/operation
  (e.g., det/limit/derivative/prime/probability). Drop spans with no linkage unless strictly needed
  for correctness (domain/case/legality).
- Integrity: [MATH_k] is atomic; keep relation/negation symbols (=, ≠, <, >, ≤, ≥, “not”, “mod”…);
  keep only necessary domain/validity checks.
- De‑dup & prune: Keep the earliest correct equation that is actually used; remove narration,
  hedges/meta‑comments, dead‑end explorations, and repeated restatements.
- Span shape: Choose COMPLETE operation clauses (verb + object/complement). Ranges must be ascending,
  non‑overlapping; multi‑token equations/formulas are covered by one span.
- Tie‑breakers: Prefer spans touching the target symbol or final substitution; choose the shortest span
  that remains correct.

Question (use it to decide relevance):
{question}

Candidates (index<TAB>token):
{indexed_block}

Output (STRICT JSON ONLY; zero‑pad to the shown width):
{{"ranges": ["005-012","030-037"]}}
    """



    aux = {"tokens": tokens, "math_map": math_map}
    
    user_text = prompt.format(question=question, indexed_block=indexed_block)
    
    return user_text, aux

# ======== 使用示例 ========
if __name__ == "__main__":
    question = "A company A and a company B both produce the same type of product. The market demand for this product is given by the equation P = 300 - Q, where P is the price and Q is the total quantity of the product produced by both companies. The production cost for each company is given by the equation C(Qi) = 50Qi, where Qi is the quantity produced by company i. The two companies are playing a Cournot game in which they simultaneously choose their production levels. What is the Nash equilibrium quantity of product produced by each company?"
    s = " Therefore, the reaction function for B is qB = (250 - qA)/2.\n\nNow, to find the Nash equilibrium, we need to solve these two reaction functions together. Let me substitute one into the other. Let's take qA = (250 - qB)/2 and plug in qB from B's reaction function. Since qB = (250 - qA)/2, substitute that into A's equation:\n\nqA = [250 - (250 - qA)/2]/2.\n\nLet me compute that step by step. First, expand the numerator inside the brackets:\n\n250 - (250 - qA)/2. Let me write 250 as 500/2 to have a common denominator:\n\n500/2 - 250/2 + qA/2 = (500 - 250)/2 + qA/2 = 250/2 + qA/2 = 125 + qA/2.\n\nTherefore, qA = [125 + qA/2]/2 = 125/2 + qA/4.\n\nNow, subtract qA/4 from both sides:\n\nqA - qA/4 = 125/2 => (3/4)qA = 125/2.\n\nMultiply both sides by 4/3:\n\nqA = (125/2) * (4/3) = (125 * 4) / (2 * 3) = 500 / 6 ≈ 83.333...\n\nWait, but let me check if I did the algebra correctly. Let me redo the substitution step.\n\nOriginal equations:\n\nqA = (250 - qB)/2\n\nqB = (250 - qA)/2\n\nSo substitute qB into qA's equation:\n\nqA = [250 - (250 - qA)/2]/2\n\nLet me compute the numerator first: 250 - [(250 - qA)/2]\n\nMultiply numerator and denominator appropriately:\n\nLet me write 250 as 500/2:\n\n500/2 - (250 - qA)/2 = [500 - 250 + qA]/2 = [250 + qA]/2\n\nTherefore, qA = [250 + qA]/2 divided by 2 again? Wait, no. Wait, the entire expression is divided by 2. Wait:\n\nOriginal substitution:\n\n$$qA = [250 - qB]/2$$, where qB is $$(250 - qA)/2$$. "
    user_text, aux = build_gpt4o_chunk(question, s, cap=0.5)

    print("\n=== USER ===\n", user_text)  # 只预览前 500 字


