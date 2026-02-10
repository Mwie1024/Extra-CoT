import json
from typing import List, Tuple, Dict, Any, Optional
from gpt_chunk_camel_data import SmartCoTChunkerV2

def _reconstruct_spans_by_scan(text: str, chunks: List[str]) -> Optional[List[Tuple[int,int]]]:
    """
    按顺序在原文中搜索每个 chunk 的首次出现位置（从上一个 chunk 的末尾继续找），
    以尽量还原 (start, end) 坐标。若出现找不到（例如重复片段、预处理有空格差异等），返回 None。
    """
    spans: List[Tuple[int,int]] = []
    cursor = 0
    for ch in chunks:
        if not ch:
            return None
        idx = text.find(ch, cursor)
        if idx == -1:
            # 尝试从全文开头搜索一次（可能上一个 cursor 走偏），如还找不到则失败
            idx = text.find(ch)
            if idx == -1:
                return None
        a, b = idx, idx + len(ch)
        spans.append((a, b))
        cursor = b
    return spans

def audit_prechunked_jsonl(
    input_jsonl: str,
    audit_report_path: str,
    max_examples_per_cat: int = 5,
    show_progress: bool = True,
):
    """
    仅读取“已分块”的 JSONL（每行是一个样本 dict），
    使用其中的 chunk_spans 或按 chunks 还原 spans，
    运行 SmartCoTChunkerV2.audit_chunks 做质量审计。
    """
    chunker = SmartCoTChunkerV2()  # 仅用于审计：不会重新分块

    summary = {
        'cuts_in_latex': 0,
        'cuts_in_plain_formula': 0,
        'cuts_in_decimal': 0,
        'cuts_in_ratio': 0,
        'cuts_in_number_unit': 0,
        'abbr_dot_boundary': 0,
        'total_samples': 0,
        'skipped_samples': 0,          # 无法审计（缺字段/无法还原 spans）
        'warnings': 0,                 # 还原 spans 失败的计数
    }
    examples: Dict[str, List[Dict[str, Any]]] = {
        k: [] for k in summary if k not in ('total_samples', 'skipped_samples', 'warnings')
    }
    line_no = 0

    def ctx(s: str, pos: int, w: int = 48) -> str:
        return s[max(0, pos - w):pos] + '⟂' + s[pos: pos + w]

    with open(input_jsonl, 'r', encoding='utf-8') as f:
        for line in f:
            line_no += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                summary['skipped_samples'] += 1
                continue

            # 取原文 COT
            text = (
                rec.get('original_cot') or
                rec.get('cot') or
                rec.get('answer') or
                rec.get('reasoning') or
                ''
            )
            if not text:
                summary['skipped_samples'] += 1
                continue

            # 拿 spans：若无 chunk_spans 则尝试由 chunks 还原
            spans = rec.get('chunk_spans')
            if not spans:
                chunks = rec.get('chunks', [])
                if chunks:
                    spans = _reconstruct_spans_by_scan(text, chunks)
                    if spans is None:
                        summary['warnings'] += 1
                        summary['skipped_samples'] += 1
                        continue
                else:
                    summary['skipped_samples'] += 1
                    continue

            # 审计
            audit = chunker.audit_chunks(text, spans)
            summary['total_samples'] += 1
            for k, v in audit['summary'].items():
                summary[k] += v

            # 抽样例
            for cat, positions in audit['positions'].items():
                if not positions:
                    continue
                room = max(0, max_examples_per_cat - len(examples[cat]))
                for p in positions[:room]:
                    examples[cat].append({
                        'id': rec.get('id'),
                        'pos': p,
                        'context': ctx(text, p),
                    })

            if show_progress and summary['total_samples'] % 100 == 0:
                print(f"[audit] processed={summary['total_samples']}  skipped={summary['skipped_samples']}  warnings={summary['warnings']}")

    report = {'summary': summary, 'examples': examples}
    with open(audit_report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("审计完成，结果写入：", audit_report_path)


# 用法示例（你可以直接替换为你的真实路径）
if __name__ == "__main__":
    audit_prechunked_jsonl(
        input_jsonl="/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/CHUNKED_RESULT.jsonl",
        audit_report_path="./audit_report.json",
        max_examples_per_cat=5,
        show_progress=True,
    )
