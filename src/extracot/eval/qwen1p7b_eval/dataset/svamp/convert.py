import json

# 输入输出文件路径
input_file = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/amc23.jsonl"   # 原始JSON文件（包含列表）
output_file = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/amc23.jsonll"  # 输出JSONL文件

with open(input_file, "r", encoding="utf-8") as f:
    data = json.load(f)

with open(output_file, "w", encoding="utf-8") as f_out:
    for item in data:
        new_item = {
            "id": item["id"],
            "question": item["question"]",
            "answer": str(item["Answer"])
        }
        f_out.write(json.dumps(new_item, ensure_ascii=False) + "\n")

print("✅ 转换完成，结果已保存到", output_file)
