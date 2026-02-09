import json

# 输入输出文件路径
input_file = "multiarith.json"   # 原始 JSON 文件（应为列表形式）
output_file = "multiarith.jsonl"  # 输出 JSONL 文件

with open(input_file, "r", encoding="utf-8") as f:
    data = json.load(f)

with open(output_file, "w", encoding="utf-8") as f_out:
    for item in data:
        new_item = {
            "question": item["question"],
            "answer": item["final_ans"]
        }
        f_out.write(json.dumps(new_item, ensure_ascii=False) + "\n")

print("✅ 转换完成，结果已保存到", output_file)
