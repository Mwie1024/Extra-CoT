import json

# 读取 JSONL 文件
queries = []

with open("/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/outputs/qwen2.5_7b_infer_50k_correct.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:  # 跳过空行
            data = json.loads(line)
            if "query" in data:
                queries.append(data["query"])

# 计算唯一 query 数量
unique_queries = set(queries)

print(f"总共的 query 数量: {len(queries)}")
print(f"唯一的 query 数量: {len(unique_queries)}")
