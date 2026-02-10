import json

def remove_model_output(input_path, output_path):
    """从 JSONL 文件中移除 model_output 字段并保存为新的 JSONL 文件"""
    count_total = 0
    count_modified = 0

    with open(input_path, 'r', encoding='utf-8') as fin, \
         open(output_path, 'w', encoding='utf-8') as fout:
        
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                print(f"⚠️ 跳过非法 JSON 行：{line[:80]}...")
                continue

            count_total += 1

            # 删除 model_output 字段
            if 'model_output' in obj:
                del obj['model_output']
                count_modified += 1

            fout.write(json.dumps(obj, ensure_ascii=False) + '\n')

    print(f"\n✅ 处理完成：共 {count_total} 条样本，其中 {count_modified} 条移除了 'model_output'。")
    print(f"📁 结果已保存至：{output_path}")


if __name__ == "__main__":
    input_path = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/dataset/metamath_145k_query/qwen3_1.7b_correct_overlap_rl_diff_30k.jsonl"           # 原始文件
    output_path = "data_no_model_output.jsonl"
    remove_model_output(input_path, output_path)
