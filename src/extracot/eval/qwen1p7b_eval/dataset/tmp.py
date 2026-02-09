import json

input_file = "amc23.jsonl"
output_file = "amc23_str.jsonl"

with open(input_file, "r", encoding="utf-8") as fin, open(output_file, "w", encoding="utf-8") as fout:
    for line in fin:
        if not line.strip():
            continue
        obj = json.loads(line)
        # 将 answer 转为字符串
        obj["answer"] = str(obj.get("answer", ""))
        fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

print(f"已处理完成，结果保存在：{output_file}")
