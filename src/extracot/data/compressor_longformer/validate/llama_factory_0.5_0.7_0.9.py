import os
import json
import random
import numpy as np

def load_json(file, encoding='utf-8'):
    data = []
    with open(file, 'r', encoding=encoding) as f:
        for j in f:
            data.append(json.loads(j))
    return data

def write_list_to_json(lst, file_path):
    # 确保输出目录存在
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(lst, f, ensure_ascii=False, indent=1)

def seed_everything(seed: int):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)

def load_all_data(
    input_dir="/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/longformer_pipeline/qwen2.5-7b-metamath"
):
    # 原始数据（用于取答案）
    original_data = load_json(
        os.path.join("/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/outputs/qwen2.5_correct.jsonl")
    )
    # 只加载 0.9 / 0.7 / 0.5 三个压缩比
    comp = {
        0.9: load_json(os.path.join(input_dir, "train_outputs_compressed_ratio_0.9.jsonl")),
        0.7: load_json(os.path.join(input_dir, "train_outputs_compressed_ratio_0.7.jsonl")),
        0.5: load_json(os.path.join(input_dir, "train_outputs_compressed_ratio_0.5.jsonl")),
    }
    return original_data, comp

def get_llamafactory_input():
    target_ratios = [0.5, 0.7, 0.9]
    original_data, comp_by_ratio = load_all_data()

    n = len(original_data)
    # 长度一致性校验
    for r in target_ratios:
        if len(comp_by_ratio[r]) != n:
            raise ValueError(
                f"长度不一致：ratio={r} 有 {len(comp_by_ratio[r])} 条，但原始数据有 {n} 条。请检查数据对齐。"
            )

    datalines = []
    for i in range(n):
        answer = original_data[i]['gold_extracted']
        for r in target_ratios:
            compressed_item = comp_by_ratio[r][i]
            input_data = f"{compressed_item['question']}<|eot_id|>{r}<|eot_id|>"
            cot = compressed_item['compressed_cot']
            output_data = f"{cot}\n\nThe final answer is: " + "$\\boxed{" + answer + "}$"

            data = {
                "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
                "input": input_data,
                "output": output_data
            }
            datalines.append(data)

    random.shuffle(datalines)
    print(f"构建完成：原始 {n} 条 × {len(target_ratios)} 个压缩比 = {len(datalines)} 条")
    write_list_to_json(datalines, './outputs/qwen2.5_7b_metamath_ratios_0.5_0.7_0.9.json')

if __name__ == '__main__':
    seed_everything(42)
    get_llamafactory_input()
