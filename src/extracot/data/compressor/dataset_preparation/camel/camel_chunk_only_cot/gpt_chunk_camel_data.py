
from __future__ import annotations

import re
import json
import statistics
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any, Optional

# -----------------------------
# Tokenizer
# -----------------------------
try:
    from transformers import LongformerTokenizerFast as _TFast
    TOKENIZER_FAST = _TFast
except Exception:  # pragma: no cover
    TOKENIZER_FAST = None

try:
    from transformers import LongformerTokenizer as _TSlow
    TOKENIZER_SLOW = _TSlow
except Exception:  # pragma: no cover
    TOKENIZER_SLOW = None

# -----------------------------
# Optional math detectors (user's module); fallbacks provided
# -----------------------------
def _fallback_latex_spans(s: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for m in re.finditer(r'(?<!\$)\$\$(.+?)\$\$', s, flags=re.DOTALL):
        spans.append(m.span())
    for m in re.finditer(r'\\\((.+?)\\\)', s, flags=re.DOTALL):
        spans.append(m.span())
    for m in re.finditer(r'\\\[(.+?)\\\]', s, flags=re.DOTALL):
        spans.append(m.span())
    for env in ['equation','equation*','align','align*','gather','gather*','multline','multline*','cases','split']:
        for m in re.finditer(rf'\\begin\{{{env}\}}(.+?)\\end\{{{env}\}}', s, flags=re.DOTALL):
            spans.append(m.span())
    i = 0; n = len(s)
    while i < n:
        a = s.find('$', i)
        if a == -1: break
        if a+1<n and s[a+1] == '$': i = a+2; continue
        b = s.find('$', a+1)
        while b != -1 and b+1 < n and s[b+1] == '$':
            b = s.find('$', b+2)
        if b == -1: break
        body = s[a+1:b]
        if re.search(r'[0-9]|[=+\-*/<>≤≥≠≈^_]|\\[a-zA-Z]+|[\(\)\[\]\{\}]', body) and len(body) <= 240:
            spans.append((a, b+1))
            i = b+1
        else:
            i = a+1
    spans.sort()
    merged = []
    for a,b in spans:
        if not merged or a > merged[-1][1]:
            merged.append([a,b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a,b) for a,b in merged]

def _fallback_plaintext_math_spans(s: str) -> List[Tuple[int,int]]:
    spans: List[Tuple[int,int]] = []
    latex = _fallback_latex_spans(s)
    def in_ltx(i: int) -> bool:
        for a,b in latex:
            if a <= i < b: return True
        return False
    anchors = []
    for m in re.finditer(r'[\^=+\-*/<>]|≤|≥|≠|≈', s):
        if in_ltx(m.start()): continue
        anchors.append(m.start())
    for i in anchors:
        L = i; R = i
        while L-1 >= 0 and not in_ltx(L-1) and s[L-1] not in '.。!?；;：:\n':
            L -= 1
            if s[L] in ',，': L += 1; break
        while R+1 < len(s) and not in_ltx(R+1) and s[R+1] not in '.。!?；;：:\n':
            R += 1
            if s[R] in ',，': R -= 1; break
        if R - L >= 2:
            spans.append((L, R+1))
    if not spans: return []
    spans.sort()
    merged = []
    for a,b in spans:
        if not merged or a > merged[-1][1]:
            merged.append([a,b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a,b) for a,b in merged]

def _merge_spans(spans: List[Tuple[int,int]]) -> List[Tuple[int,int]]:
    if not spans: return []
    spans.sort()
    merged = []
    for a,b in spans:
        if not merged or a > merged[-1][1]:
            merged.append([a,b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    return [(a,b) for a,b in merged]

try:
    from math_tokenizer_no_space import latex_spans as _user_latex_spans
    from math_tokenizer_no_space import find_formula_spans_nolatex as _user_plaintext_spans
    from math_tokenizer_no_space import merge_spans as _user_merge_spans
    HAVE_USER_MATH = True
except Exception:
    _user_latex_spans = _fallback_latex_spans
    _user_plaintext_spans = _fallback_plaintext_math_spans
    _user_merge_spans = _merge_spans
    HAVE_USER_MATH = False

def _sanitize_unbalanced_latex(s: str) -> str:
    spans = _user_latex_spans(s)
    mask = [False]*len(s)
    for a,b in spans:
        for i in range(a,b):
            if 0 <= i < len(mask): mask[i] = True
    chars = list(s)
    for i,ch in enumerate(chars):
        if mask[i]: continue
        if ch == '$': chars[i] = '＄'
        elif ch == '\\': chars[i] = '＼'
    return ''.join(chars)

def _bisect_in_spans(pos: int, spans: List[Tuple[int,int]]) -> bool:
    """
    二分查找：当前位置 pos 是否落在任一 [a, b) 区间内？
    要求 spans 已按起点排序，且互不重叠（我们在 get_all_math_spans 里已 merge）。
    """
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        a, b = spans[mid]
        if a <= pos < b:
            return True
        if pos < a:
            hi = mid
        else:
            lo = mid + 1
    return False

# -----------------------------
# Sentence unit
# -----------------------------
@dataclass
class Sentence:
    content: str
    start: int
    end: int
    tokens: int = 0
    contains_math: bool = False
    is_conclusion: bool = False
    ends_strong: bool = False
    ends_semicolon: bool = False
    ends_colon: bool = False
    is_case_start: bool = False

# -----------------------------
# Chunker
# -----------------------------
class SmartCoTChunkerV2:
    def __init__(self, tokenizer_name: str = '/Users/mwie/Downloads/longformer'):
        if TOKENIZER_FAST is not None:
            self.tokenizer = TOKENIZER_FAST.from_pretrained(tokenizer_name)
            self._fast = True
        elif TOKENIZER_SLOW is not None:
            self.tokenizer = TOKENIZER_SLOW.from_pretrained(tokenizer_name)
            self._fast = False
        else:  # pragma: no cover
            raise RuntimeError("transformers not installed")

        self.target_tokens = 512
        self.upper_slack = 48
        self.lower_slack = 96
        self.hard_cap = 640
        self.min_merge_tokens = 128

        self.alpha_over = 1.0
        self.beta_under = 0.6
        self.boundary_bonus_conclusion = 2.0
        self.boundary_bonus_strong = 1.2
        self.boundary_bonus_semicolon = 0.6
        self.boundary_bonus_colon = 0.3
        self.boundary_bonus_case = 1.0

        self._offsets: List[Tuple[int,int]] = []

    # ---------- boundary guards ----------
    _ABBREV_SET = set([
        "e.g", "i.e", "etc", "approx", "vs", "cf", "resp", "mr", "ms", "dr",
        "fig", "eq", "thm", "cor", "prop", "no", "sec"
    ])

    def _peek_nonspace(self, s: str, i: int, step: int) -> int:
        j = i
        n = len(s)
        while 0 <= j < n:
            if not s[j].isspace():
                return j
            j += step
        return -1

    def _is_decimal_dot(self, s: str, i: int) -> bool:
        if i < 1 or i+1 >= len(s): return False
        L = self._peek_nonspace(s, i-1, -1)
        if L == -1 or not s[L].isdigit():
            return False
        # allow thousands separator before the dot
        k = L
        while k-1 >= 0 and s[k-1] in ',':
            k -= 2 if (k-2 >= 0 and s[k-2].isdigit()) else 1
            if k < 0: break
        R = i+1
        while R < len(s) and s[R] in ' \t\'"':
            R += 1
        return R < len(s) and s[R].isdigit()

    def _is_ratio_colon(self, s: str, i: int) -> bool:
        if s[i] != ':': return False
        L = self._peek_nonspace(s, i-1, -1)
        R = self._peek_nonspace(s, i+1, +1)
        return (L != -1 and R != -1 and s[L].isdigit() and s[R].isdigit())

    def _is_abbrev_dot(self, s: str, i: int) -> bool:
        if s[i] != '.': return False
        j = i-1
        while j >= 0 and s[j].isalpha():
            j -= 1
        word = s[j+1:i].lower()
        if word in self._ABBREV_SET:
            return True
        return word in {'e', 'i'} and (i+1 < len(s) and s[i+1] in {'g', 'e'})

    # ---------- math spans ----------
    def get_all_math_spans(self, text: str, sanitize_unbalanced: bool = True) -> List[Tuple[int,int]]:
        s = _sanitize_unbalanced_latex(text) if sanitize_unbalanced else text
        L = _user_latex_spans(s)
        P = _user_plaintext_spans(s)
        return _user_merge_spans(L + P)

    # ---------- tokenization helpers ----------
    def _pretokenize(self, text: str):
        if self._fast:
            enc = self.tokenizer(text, return_offsets_mapping=True, add_special_tokens=False)
            self._offsets = [(a,b) for (a,b) in enc.offset_mapping]
        else:
            self._offsets = []

    def _token_count_span(self, start: int, end: int, fallback_text: Optional[str] = None) -> int:
        if self._fast and self._offsets:
            offs = self._offsets
            lo, hi = 0, len(offs)
            while lo < hi:
                mid = (lo+hi)//2
                if offs[mid][1] <= start: lo = mid + 1
                else: hi = mid
            count = 0; i = lo
            while i < len(offs) and offs[i][0] < end:
                if not (offs[i][1] <= start or offs[i][0] >= end):
                    count += 1
                i += 1
            return count
        else:
            if not fallback_text: return 0
            return len(self.tokenizer.tokenize(fallback_text))

    # ---------- sentence splitting ----------
    _CASE_START_RE = re.compile(r'(?i)^\s*case\s*\d+|^\s*情况[一二三四五六七八九十百]+|^\s*情形[一二三四五六七八九十百]+|^\s*[（(]?\s*\d+\s*[)）]\s*|^\s*[①②③④⑤⑥⑦⑧⑨⑩]\s*')
    _CONC_RE = re.compile(r'(因此|所以|综上|总之|Hence|Therefore|Thus)\b')
    _STRONG_END_RE = re.compile(r'[。.!?！？]\s*$')
    _SC_RE = re.compile(r'[；;]\s*$')
    _COLON_RE = re.compile(r'[：:]\s*$')

    def split_into_sentences(self, text: str) -> List[Sentence]:
        s = _sanitize_unbalanced_latex(text)
        math_spans = self.get_all_math_spans(s, sanitize_unbalanced=False)
        math_spans.sort()
        n = len(s)

        terms = set('。.!?！？；;：:\n')
        boundaries = [0]
        i = 0
        while i < n:
            ch = s[i]
            if ch in terms and not _bisect_in_spans(i, math_spans):
                if ch == '.' and (self._is_decimal_dot(s, i) or self._is_abbrev_dot(s, i)):
                    i += 1; continue
                if ch == ':' and self._is_ratio_colon(s, i):
                    i += 1; continue
                boundaries.append(i+1)
            i += 1
        if boundaries[-1] != n:
            boundaries.append(n)

        self._pretokenize(s)
        sentences: List[Sentence] = []
        for k in range(len(boundaries)-1):
            a, b = boundaries[k], boundaries[k+1]
            while a < b and s[a].isspace():
                a += 1
            if a >= b: continue
            content = s[a:b]
            contains_math = any((ma < b and mb > a) for (ma,mb) in math_spans)
            is_conclusion = bool(self._CONC_RE.search(content))
            ends_strong = bool(self._STRONG_END_RE.search(content))
            ends_semicolon = bool(self._SC_RE.search(content))
            ends_colon = bool(self._COLON_RE.search(content))
            is_case_start = bool(self._CASE_START_RE.search(content))
            tok_cnt = self._token_count_span(a, b, fallback_text=content)
            sentences.append(Sentence(
                content=content, start=a, end=b, tokens=tok_cnt,
                contains_math=contains_math, is_conclusion=is_conclusion,
                ends_strong=ends_strong, ends_semicolon=ends_semicolon,
                ends_colon=ends_colon, is_case_start=is_case_start
            ))
        return sentences

    # ---------- merging short sentences ----------
    def merge_short_sentences(self, sentences: List[Sentence], min_group_tokens: int = 40, max_group_len: int = 3) -> List[Sentence]:
        if not sentences: return []
        merged: List[Sentence] = []
        group: List[Sentence] = [sentences[0]]
        group_tok = sentences[0].tokens
        for sent in sentences[1:]:
            if group_tok < min_group_tokens and len(group) < max_group_len:
                group.append(sent); group_tok += sent.tokens
            else:
                merged.append(self._merge_group(group)); group = [sent]; group_tok = sent.tokens
        if group: merged.append(self._merge_group(group))
        return merged

    def _merge_group(self, group: List[Sentence]) -> Sentence:
        if len(group) == 1:
            return group[0]
        a = group[0].start; b = group[-1].end
        return Sentence(
            content='', start=a, end=b,
            tokens=sum(s.tokens for s in group),
            contains_math=any(s.contains_math for s in group),
            is_conclusion=group[-1].is_conclusion,
            ends_strong=group[-1].ends_strong,
            ends_semicolon=group[-1].ends_semicolon,
            ends_colon=group[-1].ends_colon,
            is_case_start=group[0].is_case_start
        )

    # ---------- DP splitting ----------
    def _chunk_cost(self, tok: int, boundary_feat: Sentence) -> float:
        over = max(0, tok - (self.target_tokens + self.upper_slack))
        under = max(0, (self.target_tokens - self.lower_slack) - tok)
        cost = self.alpha_over * (over / 32.0) ** 2 + self.beta_under * (under / 32.0) ** 2
        bonus = 0.0
        if boundary_feat.is_conclusion: bonus += self.boundary_bonus_conclusion
        if boundary_feat.ends_strong: bonus += self.boundary_bonus_strong
        if boundary_feat.ends_semicolon: bonus += self.boundary_bonus_semicolon
        if boundary_feat.ends_colon: bonus += self.boundary_bonus_colon
        return max(0.0, cost - bonus)

    def _dp_split(self, sentences: List[Sentence]) -> List[Tuple[int,int]]:
        n = len(sentences)
        if n == 0: return []
        prefix = [0]*(n+1)
        for i in range(n):
            prefix[i+1] = prefix[i] + sentences[i].tokens
        INF = 1e18
        dp = [INF]*(n+1)
        prev = [-1]*(n+1)
        dp[0] = 0.0
        for j in range(1, n+1):
            for i in range(0, j):
                tok = prefix[j] - prefix[i]
                if tok > self.hard_cap:
                    continue
                cost = self._chunk_cost(tok, sentences[j-1])
                if j < n and sentences[j].is_case_start:
                    cost = max(0.0, cost - self.boundary_bonus_case)
                cand = dp[i] + cost
                if cand < dp[j]:
                    dp[j] = cand; prev[j] = i
        cuts = []; k = n
        while k > 0:
            i = prev[k] if prev[k] != -1 else 0
            cuts.append((i, k)); k = i
        cuts.reverse()
        return cuts

    # ---------- Reconstruction & post-fix ----------
    def _reconstruct_chunks(self, text: str, sentences: List[Sentence], cuts: List[Tuple[int,int]]) -> List[str]:
        chunks: List[str] = []
        for (i,j) in cuts:
            a = sentences[i].start; b = sentences[j-1].end
            chunks.append(text[a:b])
        return chunks

    def _last_chunk_merge_fix(self, chunks: List[str]) -> List[str]:
        if len(chunks) <= 1: return chunks
        last = chunks[-1]
        last_tok = self.get_token_count(last)
        if last_tok < self.min_merge_tokens:
            merged = chunks[-2] + chunks[-1]
            if self.get_token_count(merged) <= self.target_tokens + self.upper_slack:
                return chunks[:-2] + [merged]
        return chunks

    # ---------- Public API ----------
    def get_token_count(self, text: str) -> int:
        if text.strip() == '': return 0
        if TOKENIZER_FAST is not None and self._fast:
            enc = self.tokenizer(text, add_special_tokens=False)
            return len(enc.input_ids)
        else:
            return len(self.tokenizer.tokenize(text))

    def chunk_text(self, text: str, validate: bool = True) -> Dict[str, Any]:
        sents_raw = self.split_into_sentences(text)
        sents = self.merge_short_sentences(sents_raw)
        if sents and (sents[0].content == '' or any(si.content == '' for si in sents)):
            for si in sents:
                if si.content == '':
                    si.content = text[si.start:si.end]
                si.tokens = self.get_token_count(si.content)
        cuts = self._dp_split(sents)
        chunks = self._reconstruct_chunks(text, sents, cuts)
        chunks = self._last_chunk_merge_fix(chunks)
        chunk_spans = [(sents[i].start, sents[j-1].end) for (i,j) in cuts]
        result = {
            'chunks': chunks,
            'chunk_spans': chunk_spans,
            'chunk_count': len(chunks),
        }
        if validate:
            result['validation'] = self.validate_chunks(text, chunks)
        return result

    # ---------- Validation ----------
    def validate_chunks(self, original_text: str, chunks: List[str]) -> Dict[str, Any]:
        stats = {'total_chunks': len(chunks), 'token_distribution': []}
        warnings = []
        errors = []
        valid = True
        for i, ch in enumerate(chunks):
            t = self.get_token_count(ch)
            stats['token_distribution'].append({'chunk_id': i, 'tokens': t, 'chars': len(ch)})
            if t > self.target_tokens + self.upper_slack:
                warnings.append(f'Chunk {i} exceeds soft limit: {t} tokens')
        try:
            spans = self.get_all_math_spans(original_text, sanitize_unbalanced=True)
            for (a,b) in spans:
                formula = original_text[a:b]
                if not any(formula in ch for ch in chunks):
                    valid = False
                    errors.append(f'Math formula possibly broken: {formula[:50]}...')
        except Exception as e:
            warnings.append(f'Math validation failed: {e}')
        return {'valid': valid, 'errors': errors, 'warnings': warnings, 'stats': stats}

    # ---------- JSONL batch processing ----------
    def read_jsonl_file(self, file_path: str) -> List[Dict[str, Any]]:
        samples = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if line:
                        try:
                            sample = json.loads(line)
                            samples.append(sample)
                        except json.JSONDecodeError as e:
                            print(f"警告: 第{line_num}行JSON解析失败: {e}")
            print(f"成功读取 {len(samples)} 个样本")
        except FileNotFoundError:
            print(f"错误: 文件 {file_path} 未找到")
        except Exception as e:
            print(f"读取文件时发生错误: {str(e)}")
        return samples

    def process_all_samples(self, samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results = []
        for i, sample in enumerate(samples):
            print(f"处理样本 {i+1}/{len(samples)}: {sample.get('id', f'sample_{i+1}')}")
            try:
                cot_text = sample.get('answer', sample.get('cot', sample.get('original_cot', '')))
                if not cot_text:
                    print(f"  警告: 样本 {sample.get('id', f'sample_{i+1}')} 没有找到CoT文本")
                    results.append({
                        'id': sample.get('id', f'sample_{i+1}'),
                        'question': sample.get('question', ''),
                        'original_cot': '',
                        'error': 'No CoT text found',
                        'chunks': [],
                        'chunk_count': 0
                    })
                    continue
                result = self.chunk_text(cot_text, validate=True)
                processed = {
                    'id': sample.get('id', f'sample_{i+1}'),
                    'question': sample.get('question', ''),
                    'original_cot': cot_text,
                    'chunks': result['chunks'],
                    'chunk_count': result['chunk_count'],
                    'chunk_spans': result.get('chunk_spans', []),
                    'validation': result.get('validation', {}),
                    'token_distribution': result.get('validation', {}).get('stats', {}).get('token_distribution', []),
                }
                results.append(processed)
                print(f"  ✅ 成功处理，生成 {result['chunk_count']} 个chunks")
            except Exception as e:
                print(f"  ❌ 处理失败: {str(e)}")
                results.append({
                    'id': sample.get('id', f'sample_{i+1}'),
                    'question': sample.get('question', ''),
                    'original_cot': sample.get('answer', sample.get('cot', sample.get('reasoning', ''))),
                    'error': str(e),
                    'chunks': [],
                    'chunk_count': 0
                })
        return results

    def save_processed_results(self, processed_results: List[Dict[str, Any]], output_path: str):
        with open(output_path, 'w', encoding='utf-8') as f:
            for r in processed_results:
                item = {
                    'id': r.get('id'),
                    'question': r.get('question', ''),
                    'original_cot': r.get('original_cot', ''),
                    'chunks': r.get('chunks', []),
                    'chunk_spans': r.get('chunk_spans', []),
                    'chunk_count': r.get('chunk_count', 0),
                    'processing_status': 'failed' if 'error' in r else 'success',
                }
                if 'error' in r:
                    item['error'] = r['error']
                if 'validation' in r:
                    item['validation_info'] = {
                        'valid': r['validation'].get('valid', True),
                        'token_distribution': r.get('token_distribution', []),
                        'math_formula_integrity': r['validation'].get('valid', True)
                    }
                f.write(json.dumps(item, ensure_ascii=False) + '\n')

    def generate_token_statistics(self, processed_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        ok = [r for r in processed_results if 'error' not in r]
        if not ok:
            return {"error": "No successful results to analyze"}
        all_tokens = [td['tokens'] for r in ok for td in r.get('token_distribution', [])]
        if not all_tokens:
            return {"error": "No token data available"}
        stats = {
            "total_chunks": len(all_tokens),
            "mean_tokens": statistics.mean(all_tokens),
            "median_tokens": statistics.median(all_tokens),
            "min_tokens": min(all_tokens),
            "max_tokens": max(all_tokens),
            "std_tokens": statistics.stdev(all_tokens) if len(all_tokens) > 1 else 0,
            "token_ranges": {
                "under_400": sum(1 for t in all_tokens if t < 400),
                "400_to_450": sum(1 for t in all_tokens if 400 <= t < 450),
                "450_to_512": sum(1 for t in all_tokens if 450 <= t <= 512),
                "over_512": sum(1 for t in all_tokens if t > 512),
            }
        }
        return stats

    def analyze_chunk_distribution(self, processed_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        ok = [r for r in processed_results if 'error' not in r]
        counts = [r.get('chunk_count', 0) for r in ok]
        if not counts:
            return {"error": "No successful results to analyze"}
        return {
            "samples_with_1_chunk": sum(1 for c in counts if c == 1),
            "samples_with_2_chunks": sum(1 for c in counts if c == 2),
            "samples_with_3_chunks": sum(1 for c in counts if c == 3),
            "samples_with_4_plus_chunks": sum(1 for c in counts if c >= 4),
            "max_chunks_in_sample": max(counts),
            "average_chunks_per_sample": statistics.mean(counts),
        }

    def save_statistics(self, stats: Dict[str, Any], stats_path: str):
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    # ---------- single-string test helper ----------
    def test_single_string(self, text: str) -> str:
        res = self.chunk_text(text, validate=True)
        out = []
        out.append(f"Total chunks: {res['chunk_count']}")
        for i, ch in enumerate(res['chunks'], 1):
            tokens = self.get_token_count(ch)
            start_preview = ch[:80].replace('\n','\\n')
            end_preview = ch[-80:].replace('\n','\\n')
            out.append(f"\n=== CHUNK {i} ({tokens} tokens) ===")
            out.append(f"START: {start_preview}...")
            out.append(f"END  : ...{end_preview}")
        v = res.get('validation', {})
        if v:
            out.append("\nValidation:")
            out.append(f"  valid={v.get('valid', True)}")
            if v.get('errors'): out.append(f"  errors={v['errors']}")
            if v.get('warnings'): out.append(f"  warnings={v['warnings']}")
        return "\n".join(out)

    # ---------- Auditing helpers ----------
    def audit_chunks(self, original_text: str, chunk_spans: List[Tuple[int,int]]) -> Dict[str, Any]:
        issues = {
            'cuts_in_latex': [],
            'cuts_in_plain_formula': [],
            'cuts_in_decimal': [],
            'cuts_in_ratio': [],
            'cuts_in_number_unit': [],
            'abbr_dot_boundary': [],
        }
        cuts = [b for (_,b) in chunk_spans[:-1]]
        latex = _user_latex_spans(original_text)
        plain = _user_plaintext_spans(original_text)
        dec_pat = re.compile(r'(?<![A-Za-z])[-+]?(?:\d{1,3}(?:,\d{3})*|\d+)\s*\.\s*[\'"]?\s*\d+')
        dec_spans = [(m.start(), m.end()) for m in dec_pat.finditer(original_text)]
        ratio_pat = re.compile(r'\d\s*:\s*\d')
        ratio_spans = [(m.start(), m.end()) for m in ratio_pat.finditer(original_text)]
        unit_pat = re.compile(r'(?:\d(?:,\d{3})*|\d+)(?:\.\d+)?\s*(%|cm|mm|m|km|kg|g|mg|s|ms|μs|N|Pa|J|W|V|A|K|°C)')
        unit_spans = [(m.start(), m.end()) for m in unit_pat.finditer(original_text)]

        def in_spans(pos, spans):
            for a,b in spans:
                if a < pos < b: return True
            return False

        for pos in cuts:
            if in_spans(pos, latex): issues['cuts_in_latex'].append(pos)
            if in_spans(pos, plain): issues['cuts_in_plain_formula'].append(pos)
            if in_spans(pos, dec_spans): issues['cuts_in_decimal'].append(pos)
            if in_spans(pos, ratio_spans): issues['cuts_in_ratio'].append(pos)
            if in_spans(pos, unit_spans): issues['cuts_in_number_unit'].append(pos)
            if pos-1 >= 0 and self._is_abbrev_dot(original_text, pos-1):
                issues['abbr_dot_boundary'].append(pos)

        summary = {k: len(v) for k,v in issues.items()}
        return {'summary': summary, 'positions': issues}

    def test_and_audit_single(self, text: str) -> str:
        res = self.chunk_text(text, validate=True)
        spans = res.get('chunk_spans', [])
        audit = self.audit_chunks(text, spans)
        lines = [self.test_single_string(text)]
        lines.append("\n--- AUDIT SUMMARY ---")
        for k,v in audit['summary'].items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)


def _demo():
    txt = "Total 8010+178=8188. 8188 +89=8277. Difference between 8191 and 8188 is 3. Not divisible.\\n\\nSo since none of the primes up to 89 divide 8191, and since sqrt(8191) is about 90. 5, which we've covered, that means 8191 is a prime number."
    chunker = SmartCoTChunkerV2()
    print(chunker.test_and_audit_single(txt))

if __name__ == "__main__":
    chunker = SmartCoTChunkerV2()
    samples = chunker.read_jsonl_file('./only_cot_data.jsonl')
    processed = chunker.process_all_samples(samples)
    chunker.save_processed_results(processed, './chunked_result.jsonl')
    stats = {
        "token_statistics": chunker.generate_token_statistics(processed),
        "chunk_distribution": chunker.analyze_chunk_distribution(processed),
    }
    chunker.save_statistics(stats, './stats.json')
