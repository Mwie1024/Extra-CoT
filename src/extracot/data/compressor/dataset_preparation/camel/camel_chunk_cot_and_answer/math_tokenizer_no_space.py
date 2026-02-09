# -*- coding: utf-8 -*-
"""
math_tokenizer_nolatex.py

Improved whitespace-agnostic tokenizer + non-LaTeX formula span detector.

Design:
- Ignore LaTeX spans entirely (they will be handled elsewhere).
- Identify "anchors" first (comparators, arithmetic ops with proper neighbors,
  caret '^', and numeric lists like "144 and 18").
- Expand from each anchor minimally across mathy tokens and short variables,
  stopping at separators and non-mathy multi-letter words.
- Merge overlaps and filter out weak spans.

No external dependencies.
"""

from __future__ import annotations

import argparse
import unicodedata
import re
from dataclasses import dataclass
_NUM_RE = re.compile(r"(?:\d(?:[,_]?\d)*)(?:\.\d+)?(?:[eE][+-]?\d+)?|(?:\.\d+)(?:[eE][+-]?\d+)?")
from typing import List, Tuple, Optional

# -----------------------------
# Unicode normalization
# -----------------------------

def normalize_unicode(s: str) -> str:
    s = unicodedata.normalize('NFKC', s)
    s = (s.replace('−','-').replace('‐','-').replace('‒','-').replace('–','-').replace('—','-').replace('―','-'))
    s = (s.replace('“','"').replace('”','"').replace('„','"').replace('‟','"'))
    s = (s.replace("‘","'").replace("’","'").replace("‚","'").replace("‛","'"))
    return s

# -----------------------------
# LaTeX spans (to ignore in this detector)
# -----------------------------

LATEX_PATS = [
    re.compile(r'\$\$(?:(?:\\.|[^$\\])*)\$\$', re.S),
    re.compile(r'\\\[(?:(?:\\.|[^\\])*)\\\]', re.S),
    re.compile(r'\\\((?:(?:\\.|[^\\])*)\\\)', re.S),
    re.compile(r'\$(?:(?:\\.|[^$\\])*)\$', re.S),
]

def latex_spans(s: str) -> List[Tuple[int,int]]:
    spans = []
    for pat in LATEX_PATS:
        spans += [m.span() for m in pat.finditer(s)]
    spans.sort()
    merged = []
    for a,b in spans:
        if not merged or a > merged[-1][1]:
            merged.append([a,b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a,b) for a,b in merged]

def in_any_span(x: int, spans: List[Tuple[int,int]]) -> bool:
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo+hi)//2
        a,b = spans[mid]
        if a <= x < b: return True
        if x < a: hi = mid
        else: lo = mid + 1
    return False

# -----------------------------
# Tokenizer (whitespace-agnostic)
# -----------------------------

_OP_CHARS = set("+-−–—*/×·⋅÷=<>≤≥≠≈~^_|:%")
_BRACKETS = set("()[]{}")
_COMPARATORS = {"=", "==", "<", ">", "<=", ">=", "≤", "≥", "≠", "≈", "~", "∼", ":="}
_FUNC_HINTS = {"sin","cos","tan","log","ln","exp","sqrt","min","max",
               "argmin","argmax","Pr","Var","Cov","E","P"}

@dataclass
class Token:
    typ: str    # num | ident | func | op | br | other
    a: int
    b: int
    lex: str

def scan_tokens(text: str, ignore_spans: Optional[List[Tuple[int,int]]] = None) -> List[Token]:
    s = normalize_unicode(text)
    toks: List[Token] = []
    i, n = 0, len(s)
    ignore_spans = ignore_spans or []
    # skip function: if i enters an ignored span, jump to its end
    def jump_if_ignored(pos: int) -> int:
        # binary search could be used; linear is OK for few spans
        for (A,B) in ignore_spans:
            if A <= pos < B:
                return B
        return pos

    while i < n:
        i2 = jump_if_ignored(i)
        if i2 != i:
            i = i2
            continue
        ch = s[i]
        if ch.isspace():
            i += 1
            continue

        if ch == '\\':
            # Treat backslash-led sequences as 'tex' tokens to fence off macros
            j = i + 1
            while j < n and s[j].isalpha():
                j += 1
            toks.append(Token("tex", i, j, s[i:j]))
            i = j
            continue

        if ch.isdigit() or (ch == '.' and i+1 < n and s[i+1].isdigit()):
            # Regex-based number: digits with optional grouping commas/underscores, optional .digits, optional exponent
            m = _NUM_RE.match(s, i)
            if m:
                j = m.end()
                toks.append(Token("num", i, j, s[i:j]))
                i = j
                continue
            # not a number; treat '.' as OTHER
            toks.append(Token("other", i, i+1, s[i:i+1]))
            i += 1
            continue

        if ch.isalpha() or ch == '_':
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == '_'):
                j += 1
            lex = s[i:j]
            typ = "func" if (j < n and s[j] == '(') or (lex in _FUNC_HINTS) else "ident"
            toks.append(Token(typ, i, j, lex))
            i = j
            continue

        if ch in _BRACKETS:
            toks.append(Token("br", i, i+1, ch))
            i += 1
            continue

        if i+1 < n and s[i:i+2] in {"<=", ">=", "==", ":="}:
            toks.append(Token("op", i, i+2, s[i:i+2]))
            i += 2
            continue

        if ch in _OP_CHARS:
            toks.append(Token("op", i, i+1, ch))
            i += 1
            continue

        toks.append(Token("other", i, i+1, s[i:i+1]))
        i += 1

    return toks

# -----------------------------
# Anchor-first formula span detection (non-LaTeX)
# -----------------------------

SEPS = set(",;，；。.!?:：\n")
CONNECTORS = {"and","or","与","及","和"}
NEGATORS = {"not","非","不是"}
ASSIGN_WORDS = {"is","equals","equal","为","是"}
REM_WORDS = {"remainder","rem","余数","mod","modulo"}

def is_mathy_token(t: Token) -> bool:
    if t.typ in {"num","op","br"}:
        return True
    if t.typ == "ident" and len(t.lex) == 1:
        return True  # variables a, b, x
    if t.typ == "func":
        return True
    return False

def is_connector_ident(t: Token) -> bool:
    return t.typ == "ident" and t.lex.lower() in (CONNECTORS | ASSIGN_WORDS | REM_WORDS)

def has_neighbor_mathy(toks: List[Token], i: int) -> bool:
    return (i-1 >= 0 and is_mathy_token(toks[i-1])) or (i+1 < len(toks) and is_mathy_token(toks[i+1]))

def is_arith_op(lex: str) -> bool:
    return lex in {"+","-","*","×","·","⋅","/","÷"}

def is_cmp_op(lex: str) -> bool:
    return lex in _COMPARATORS

def is_pow_op(lex: str) -> bool:
    return lex == "^"

def numeric_list_span(toks: List[Token], i: int, s: str) -> Optional[Tuple[int,int]]:
    """If token i is a number starting a numeric list like '144 and 18, 23', return minimal span over that list."""
    if toks[i].typ != "num":
        return None
    # Only consider when another number appears within a short window with connector/comma
    j = i
    saw_second = False
    end = toks[i].b
    k = i + 1
    while k < len(toks):
        tk = toks[k]
        if s[tk.a] in SEPS:
            # Comma/Chinese comma: allow continuation
            if tk.lex in {",","，"}:
                k += 1
                continue
            # Hard stop at sentence enders . ! ? 。 ； :
            if tk.lex in {".","!","?","。","；",";","：",":"}:
                break
        if tk.typ == "ident" and tk.lex.lower() in NEGATORS:
            break
        if tk.typ == "ident" and tk.lex.lower() in CONNECTORS:
            end = tk.b
            k += 1
            continue
        if tk.typ == "num":
            end = tk.b
            saw_second = True
            k += 1
            # allow trailing percent
            if k < len(toks) and toks[k].typ == "op" and toks[k].lex == "%":
                end = toks[k].b
                k += 1
            # after a second number, allow an immediate comma and another number (loop continues)
            continue
        # other tokens that are benign: space is skipped by tokenizer; stop on non-benign
        if tk.typ == "op" and tk.lex == "%":
            end = tk.b
            k += 1
            continue
        # fallback stop
        break
    if saw_second:
        start = toks[i].a
        return (start, end)
    return None


def expand_minimal(toks: List[Token], i_anchor: int, s: str, max_tokens: int = 30) -> Tuple[int,int]:
    """Expand around anchor to include a minimal mathy expression.
       Stops at sentence separators and at **top-level commas** between tokens.
    """
    # Precompute char maps
    in_num, depth = char_maps_for_tokens(s, toks)

    def has_top_level_comma_between(t_left: Token, t_right: Token) -> bool:
        if t_left.b >= t_right.a:
            return False
        for i in range(t_left.b, t_right.a):
            if s[i] in {',','，'} and not in_num[i] and depth[i] == 0:
                return True
        return False

    # left
    L = i_anchor
    steps = 0
    while L-1 >= 0 and steps < max_tokens:
        t_prev = toks[L-1]
        # stop at separators or top-level comma *before* current token
        if s[t_prev.a] in SEPS or has_top_level_comma_between(t_prev, toks[L]):
            break
        if is_mathy_token(t_prev):
            L -= 1; steps += 1; continue
        # allow short connectors only if both sides are mathy-ish
        if is_connector_ident(t_prev) and L-2 >= 0 and is_mathy_token(toks[L-2]) and is_mathy_token(toks[L]):
            L -= 1; steps += 1; continue
        # allow 'is/equals' only if immediate left is short var (1-2 letters)
        if t_prev.typ == "ident" and t_prev.lex.lower() in ASSIGN_WORDS:
            if L-2 >= 0 and toks[L-2].typ == "ident" and len(toks[L-2].lex) <= 2:
                L -= 1; steps += 1; continue
        break

    # right
    R = i_anchor
    steps = 0
    while R+1 < len(toks) and steps < max_tokens:
        t_next = toks[R+1]
        # stop at separators or top-level comma *after* current token
        if s[t_next.a] in SEPS or has_top_level_comma_between(toks[R], t_next):
            break
        if is_mathy_token(t_next):
            R += 1; steps += 1; continue
        if is_connector_ident(t_next) and R+2 < len(toks) and is_mathy_token(toks[R]) and is_mathy_token(toks[R+2]):
            R += 1; steps += 1; continue
        if t_next.typ == "ident" and t_next.lex.lower() in ASSIGN_WORDS:
            if R+2 < len(toks) and toks[R+2].typ == "num":
                R += 1; steps += 1; continue
        break

    return (toks[L].a, toks[R].b)

def merge_spans(spans: List[Tuple[int,int]]) -> List[Tuple[int,int]]:
    spans.sort()
    merged = []
    for a,b in spans:
        if not merged or a > merged[-1][1]:
            merged.append([a,b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a,b) for a,b in merged]


TRAIL_TRIM = set([",","，",";","；",".","。","!","?","、",":","："])


def _pos_in_num(pos: int, toks: List[Token]) -> bool:
    for t in toks:
        if t.typ == "num" and t.a <= pos < t.b:
            return True
    return False
def trim_span_chars(s: str, a: int, b: int) -> tuple[int,int]:
    while a < b and s[a].isspace():
        a += 1
    while a < b and s[b-1] in TRAIL_TRIM:
        b -= 1
    return (a,b)

def split_on_strong_seps(s: str, a: int, b: int, toks: List[Token]) -> list[tuple[int,int]]:
    strong = set([".","。","!","?","；",";","：",":"])
    spans: list[tuple[int,int]] = []
    last = a
    i = a
    while i < b:
        if s[i] in strong and not _pos_in_num(i, toks):
            if i > last:
                spans.append((last, i))  # exclude the separator
            last = i+1
        i += 1
    if last < b:
        spans.append((last, b))
    return spans if len(spans) >= 1 else [(a,b)]

def split_on_commas_tokenaware(s: str, a: int, b: int, toks: List[Token]) -> List[tuple[int,int]]:
    cuts = []
    i = a
    while i < b:
        if s[i] in {',','，'} and not _pos_in_num(i, toks):
            cuts.append(i)
        i += 1
    if not cuts:
        return [(a,b)]
    segs = []
    last = a
    for c in cuts:
        if last < c:
            segs.append((last, c))
        last = c+1
    if last < b:
        segs.append((last, b))
    return segs

def char_maps_for_tokens(s: str, toks: List[Token]):
    in_num = [False]*len(s)
    for t in toks:
        if t.typ == "num":
            for i in range(t.a, t.b):
                if 0 <= i < len(in_num):
                    in_num[i] = True
    # bracket depth for (),[],{}
    depth = [0]*len(s)
    d = 0
    opens = set("([{")
    closes = set(")]}")
    pairs = {')':'(', ']':'[', '}':'{'}
    for i,ch in enumerate(s):
        if ch in opens:
            d += 1
        depth[i] = d
        if ch in closes:
            # decrement depth after marking pos
            d = max(0, d-1)
    return in_num, depth

def split_on_strong_seps_tokenaware(s: str, a: int, b: int, toks: List[Token]) -> List[tuple[int,int]]:
    strong = set([".","。","!","?","；",";","：",":"])
    in_num, _ = char_maps_for_tokens(s, toks)
    cuts = []
    for i in range(a, b):
        if s[i] in strong and not in_num[i]:
            cuts.append(i)
    if not cuts:
        return [(a,b)]
    spans = []
    last = a
    for c in cuts:
        if last < c:
            spans.append((last, c))
        last = c+1
    if last < b:
        spans.append((last, b))
    return spans

def trim_span_edges(s: str, a: int, b: int, toks: List[Token]) -> tuple[int,int]:
    # basic whitespace/punct trim
    while a < b and s[a].isspace():
        a += 1
    while a < b and s[b-1] in TRAIL_TRIM:
        b -= 1
    # trim leading connectors (e.g., "and ") if immediately followed by mathy token
    # find first token overlapping [a,b)
    first_tok = None
    for t in toks:
        if t.b <= a: continue
        if t.a >= b: break
        first_tok = t
        break
    if first_tok and first_tok.typ == "ident" and first_tok.lex.lower() in CONNECTORS:
        # peek next token
        idx = toks.index(first_tok)
        nxt = toks[idx+1] if idx+1 < len(toks) else None
        if nxt and is_mathy_token(nxt):
            a = max(a, first_tok.b)
            while a < b and s[a].isspace():
                a += 1
    return (a,b)

def split_on_commas_tokenaware(s: str, a: int, b: int, toks: List[Token]) -> List[tuple[int,int]]:
    in_num, depth = char_maps_for_tokens(s, toks)
    cuts = []
    for i in range(a, b):
        if s[i] in {',','，'} and not in_num[i] and depth[i] == 0:
            cuts.append(i)
    if not cuts:
        return [(a,b)]
    spans = []
    last = a
    for c in cuts:
        if last < c:
            spans.append((last, c))
        last = c+1
    if last < b:
        spans.append((last, b))
    return spans
def find_formula_spans_nolatex(text: str) -> List[Tuple[int,int]]:
    """Return tight spans of math-like expressions, **ignoring LaTeX** regions."""
    s = normalize_unicode(text)
    LTX = latex_spans(s)
    toks = scan_tokens(s, ignore_spans=LTX)
    if not toks:
        return []

    spans: List[Tuple[int,int]] = []

    # 0) numeric lists like "144 and 18, 23" (stop before "not ...")
    used = [False]*len(toks)
    for i,tk in enumerate(toks):
        if tk.typ == "num" and not used[i]:
            nl = numeric_list_span(toks, i, s)
            if nl is not None:
                spans.append(nl)
                # mark tokens inside span as used (approx)
                for k,tt in enumerate(toks):
                    if nl[0] <= tt.a < nl[1]:
                        used[k] = True

    # 1) anchors: comparators, caret, arithmetic ops with mathy neighbors
    def add_span(a,b):
        if b - a <= 2:  # too short
            return
        spans.append((a,b))

    for i,tk in enumerate(toks):
        if used[i]:  # already part of a numeric list span
            continue
        if tk.typ == "op" and is_cmp_op(tk.lex):
            a,b = expand_minimal(toks, i, s)
            add_span(a,b)
            continue
        if tk.typ == "op" and is_pow_op(tk.lex):
            # require number/ident neighbor
            if has_neighbor_mathy(toks, i) and not (i-1>=0 and toks[i-1].typ=="tex") and not (i+1<len(toks) and toks[i+1].typ=="tex"):
                a,b = expand_minimal(toks, i, s)
                add_span(a,b)
            continue
        if tk.typ == "op" and is_arith_op(tk.lex):
            # require at least one neighbor be num/ident/br
            if has_neighbor_mathy(toks, i) and not (i-1>=0 and toks[i-1].typ=="tex") and not (i+1<len(toks) and toks[i+1].typ=="tex"):
                a,b = expand_minimal(toks, i, s)
                add_span(a,b)
            continue
        # textual assign like "a is 5"
        if tk.typ == "ident" and tk.lex.lower() in ASSIGN_WORDS:
            # left should be short var, right should be number or mathy
            if i-1 >= 0 and toks[i-1].typ == "ident" and len(toks[i-1].lex) <= 2:
                if i+1 < len(toks) and (toks[i+1].typ in {"num","ident","func"} or (toks[i+1].typ=="op" and toks[i+1].lex in {"-","+"})):
                    a,b = expand_minimal(toks, i, s)
                    add_span(a,b)
            continue
        # remainder/mod textual anchors inside non-LaTeX
        if tk.typ == "ident" and tk.lex.lower() in REM_WORDS:
            a,b = expand_minimal(toks, i, s)
            add_span(a,b)
            continue

    
    
    # merge and lightly filter (must contain at least 2 mathy tokens or a comparator)
    merged = merge_spans(spans)

    def span_ok_range(a,b,toks_local) -> bool:
        cnt_mathy = 0
        has_cmp = False
        for t in toks_local:
            if t.a >= b or t.b <= a:
                continue
            if is_mathy_token(t):
                cnt_mathy += 1
            if t.typ == "op" and is_cmp_op(t.lex):
                has_cmp = True
        if has_cmp:
            return True
        return cnt_mathy >= 2

    # split merged spans on strong sentence separators and trim punctuation
    split_spans = []
    for (a,b) in merged:
        for (aa,bb) in split_on_strong_seps(s, a, b, toks):
            ta,tb = trim_span_edges(s, aa, bb, toks)
            if tb - ta > 0:
                if SPLIT_COMMAS:
                    # optional comma-based finer split
                    for (ca,cb) in split_on_commas_tokenaware(s, ta, tb, toks):
                        cta, ctb = trim_span_chars(s, ca, cb)
                        if span_ok_range(cta, ctb, toks):
                            split_spans.append((cta, ctb))
                else:
                    split_spans.append((ta,tb))

    final = [(a,b) for (a,b) in split_spans if span_ok_range(a,b,toks)]
    return final



# -----------------------------
# Visualization
# -----------------------------

def visualize_spans(text: str, spans: List[Tuple[int,int]]) -> str:
    s = normalize_unicode(text)
    out = []
    last = 0
    for a,b in spans:
        out.append(s[last:a]); out.append("[MATH]"); out.append(s[a:b]); out.append("[/MATH]")
        last = b
    out.append(s[last:])
    return ''.join(out)

def print_tokens(text: str, toks: List[Token]):
    for t in toks:
        print(f"{t.typ:<5} [{t.a:>4},{t.b:<4}] {repr(text[t.a:t.b])}")

# -----------------------------
# Demo
# -----------------------------

EXAMPLES = [
    "<think>\nOkay, so I need to find the next perfect number after 28. Hmm, let me start by recalling what a perfect number is. A perfect number is a positive integer that is equal to the sum of its proper divisors, excluding itself. Proper divisors are numbers less than the number that divide it evenly. \n\nAlright, so 28 is a perfect number. Let me verify that quickly. The divisors of 28 are 1, 2, 4, 7, 14. Adding them up: 1 + 2 is 3, plus 4 is 7, plus 7 is 14, plus 14 is 28. Yep, that checks out. So the sum of its proper divisors equals the number itself.\n\nNow, the question is asking for the next perfect number after 28. I remember that perfect numbers are pretty rare. From what I learned, they are related to Mersenne primes. There's a formula for even perfect numbers, which are generated by the formula $2^{p-1}(2^p - 1)$ where $2^p - 1$ is a Mersenne prime. So each Mersenne prime corresponds to an even perfect number. \n\nFirst, let me confirm that 28 fits into this formula. Let's see. Let's try p=3. Then $2^{3-1}(2^3 - 1) = 4*(8-1) = 4*7 = 28$. Yes, that's exactly 28. So the next perfect number would correspond to the next Mersenne prime. \n\nSo, Mersenne primes are primes of the form $2^p - 1$. For this to be prime, p itself must be a prime number. So the next prime after 3 is 5. Let me check if $2^5 - 1$ is prime. $2^5 is 32, so 32 - 1 = 31, which is a prime number. Therefore, using the formula, the next perfect number would be $2^{5-1}(2^5 - 1) = 16 * 31. Let me calculate that. 16*30 is 480, plus 16 is 496. So 496 would be the next perfect number after 28. \n\nBut wait, let me verify that 496 is indeed a perfect number. Let me try adding its proper divisors. But that might take a while. Maybe there's a smarter way. Alternatively, since we know that the formula generates perfect numbers, and since Euclid proved that this formula gives a perfect number whenever $2^p - 1$ is prime, and Euler later showed that all even perfect numbers are of this form. So if we trust the formula (which we should), then 496 is the next one. \n\nBut just to be thorough, let me check the sum of divisors for 496. Let's find all the proper divisors of 496. \n\nFirst, factorize 496. Since we know it's 16 * 31, which is $2^4 * 31$. Therefore, the divisors can be generated by multiplying combinations of these prime factors. \n\nThe divisors would be:\n\n1, 2, 4, 8, 16, 31, 62, 124, 248. \n\nLet me add them up:\n\nStart with 1 + 2 = 3.\n\n3 + 4 = 7.\n\n7 + 8 = 15.\n\n15 + 16 = 31.\n\n31 + 31 = 62.\n\n62 + 62 = 124.\n\n124 + 124 = 248.\n\n248 + 248 = 496. \n\nWait, let me do that step by step properly. The divisors are 1, 2, 4, 8, 16, 31, 62, 124, 248. \n\nAdding them:\n\n1 + 2 = 3.\n\n3 + 4 = 7.\n\n7 + 8 = 15.\n\n15 + 16 = 31.\n\n31 + 31 (the next divisor) = 62.\n\n62 + 62 = 124.\n\n124 + 124 = 248.\n\n248 + 248 = 496. \n\nWait, but hold on, that seems like I'm adding some numbers twice. Let me write them all out:\n\n1 + 2 + 4 + 8 + 16 + 31 + 62 + 124 + 248.\n\nLet me compute step by step:\n\nStart with 1 + 2 = 3.\n\n3 + 4 = 7.\n\n7 + 8 = 15.\n\n15 + 16 = 31.\n\n31 + 31 = 62.\n\n62 + 62 = 124.\n\n124 + 124 = 248.\n\n248 + 248 = 496. \n\nWait, but that seems to be adding each term once. Wait, no, the actual sum is:\n\n1 + 2 + 4 + 8 + 16 + 31 + 62 + 124 + 248.\n\nLet me compute this:\n\nFirst, add the powers of 2: 1 + 2 + 4 + 8 + 16 = 31.\n\nThen add the rest: 31 + 31 + 62 + 124 + 248.\n\nWait, but 31 is already included once as a divisor. Wait, no, the divisors are 1, 2, 4, 8, 16, 31, 62, 124, 248. So the sum is:\n\n(1 + 2 + 4 + 8 + 16) + (31 + 62 + 124 + 248).\n\nFirst part: 1+2+4+8+16 = 31.\n\nSecond part: 31 + 62 is 93, plus 124 is 217, plus 248 is 465.\n\nTotal sum: 31 + 465 = 496. \n\nYes! So the sum of the proper divisors is 496, which confirms that 496 is indeed a perfect number. Therefore, the next perfect number after 28 is 496.\n\nBut just to make sure there are no perfect numbers between 28 and 496, let me check if there are any others. The known perfect numbers are 6, 28, 496, 8128, etc. So between 28 and 496, the next one is 496. Let me check if maybe there's an odd perfect number? But as far as I know, no odd perfect numbers have been discovered yet. All known perfect numbers are even, generated by the formula I mentioned. So there's no need to check for odd ones here. Therefore, 496 is indeed the next one after 28.\n\nSo the answer should be 496.\n</think>\n\nTo determine the next perfect number after 28, we begin by recalling the definition of a **perfect number**: a positive integer that is equal to the sum of its **proper divisors**, i.e., all its positive divisors excluding itself.\n\n---\n\n### Step 1: Understanding the Structure of Even Perfect Numbers\n\nAll known even perfect numbers are generated by the formula:\n\n$$\n2^{p-1}(2^p - 1)\n$$\n\nwhere $2^p - 1$ is a **Mersenne prime** (a prime number of the form $2^p - 1$), and $p$ is also a prime number.\n\nThis formula was first discovered by Euclid and later expanded by Euler, who proved that all even perfect numbers are of this form.\n\n---\n\n### Step 2: Identifying the Next Mersenne Prime After 28\n\nWe know that 28 is a perfect number corresponding to $p = 3$:\n\n$$\n2^{3-1}(2^3 - 1) = 4 \\times 7 = 28\n$$\n\nThe next prime number after 3 is 5. Let's compute the corresponding perfect number:\n\n$$\n2^{5-1}(2^5 - 1) = 16 \\times 31 = 496\n$$\n\nSo, the next perfect number after 28 is **496**.\n\n---\n\n### Step 3: Verifying 496 is a Perfect Number\n\nTo confirm, we sum the proper divisors of 496. The prime factorization of 496 is:\n\n$$\n496 = 2^4 \\times 31\n$$\n\nThe proper divisors are:\n\n$$\n1, 2, 4, 8, 16, 31, 62, 124, 248\n$$\n\nSumming them:\n\n$$\n1 + 2 + 4 + 8 + 16 + 31 + 62 + 124 + 248 = 496\n$$\n\nThis confirms that 496 is indeed a perfect number.\n\n---\n\n### Step 4: Ensuring No Perfect Numbers Exist Between 28 and 496\n\nThere are no known perfect numbers between 28 and 496. The sequence of known perfect numbers is:\n\n$$\n6,\\ 28,\\ 496,\\ 8128,\\ \\ldots\n$$\n\nAlso, no odd perfect numbers have been discovered to date, so we can confidently say that 496 is the next perfect number after 28.\n\n---\n\n### Final Answer\n\n$$\n\\boxed{496}\n$$"
    ]

SPLIT_COMMAS = True

def main():
    global SPLIT_COMMAS
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", type=str, help="Custom text to analyze")
    ap.add_argument("--show", action="store_true", help="Show tokens (non-LaTeX only)")
    ap.add_argument("--split-commas", action="store_true", help="Further split math spans at commas if both sides look mathy")
    args = ap.parse_args()

    SPLIT_COMMAS = bool(args.split_commas) or SPLIT_COMMAS

    if args.text:
        texts = [args.text]
    else:
        texts = EXAMPLES

    for i,txt in enumerate(texts):
        s = normalize_unicode(txt)
        spans = find_formula_spans_nolatex(s)
        print("="*80)
        print(f"EX[{i}] input : {txt}")

        print("=====" * 20)

        print(f"EX[{i}] vis   : {visualize_spans(s, spans)}")
        print(f"EX[{i}] spans : {spans}")
        if args.show:
            toks = scan_tokens(s, ignore_spans=latex_spans(s))
            print("TOKENS(non-LaTeX):")
            print_tokens(s, toks)

if __name__ == "__main__":
    main()
