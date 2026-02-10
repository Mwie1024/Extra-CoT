#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
根据 B.jsonl 的 question 与 A.jsonl 的 query 匹配，
从 A.jsonl 中保留交集部分并重命名字段输出。
"""

import json

def load_jsonl(path):
    """读取 JSONL 文件"""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)

def normalize(text):
    """标准化字符串用于匹配"""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).lower()

def main():
    A_PATH = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/longformer_pipeline/query_result/Compression/SFT_actual_ratio_intesection/selected_sft_seed_15k.jsonl"
    B_PATH = "recat_r020.jsonl"
    OUT_PATH = "recat_r100.jsonl"

    # 读取数据
    A = list(load_jsonl(A_PATH))
    B = list(load_jsonl(B_PATH))

    # 构建 B 的 question 集合（标准化）
    b_question_set = {normalize(b.get("question", "")) for b in B}

    out_records = []

    for a in A:
        a_query_norm = normalize(a.get("query", ""))
        if a_query_norm in b_question_set:
            new_item = {
                "id": a.get("id"),
                "question": a.get("query"),  # A.query -> question
                "output": a.get("model_output"),  # A.model_output -> output
                "orig_tokens": None,
                "comp_tokens": None,
            }

            out_records.append(new_item)

    # 写出结果
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for rec in out_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"✅ 完成，共保留 {len(out_records)} 条匹配记录，输出文件：{OUT_PATH}")

if __name__ == "__main__":
    main()
