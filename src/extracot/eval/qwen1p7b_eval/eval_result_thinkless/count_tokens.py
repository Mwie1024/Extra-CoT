import json

# 加载 JSON 文件
with open("/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/eval_result_thinkless/150/gsm8k/3.0/prediction.json", "r", encoding="utf-8") as f:
    data = json.load(f)

# 过滤出 has_closed_think = true 的样本，并提取 cot_length
cot_lengths = [
    item["cot_length"]
    for item in data
    if item.get("has_closed_think") is True and "cot_length" in item
]

# 计算平均值
average_cot_length = sum(cot_lengths) / len(cot_lengths) if cot_lengths else 0

print("平均 cot_length:", average_cot_length)
