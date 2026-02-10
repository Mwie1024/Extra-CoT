import json
import tiktoken
import numpy as np
import matplotlib.pyplot as plt

# ===== 参数部分 =====
file_path = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/outputs/qwen2.5_7b_infer_50k_correct.jsonl"   # 你的 JSONL 文件路径
encoding_name = "cl100k_base"   # 用于 GPT-4 / GPT-3.5 的编码方式（视模型而定）

# ===== 初始化编码器 =====
enc = tiktoken.get_encoding(encoding_name)

# ===== 读取数据并计算 token 数 =====
token_counts = []
more_512 = 0

with open(file_path, "r", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        data = json.loads(line)
        model_output = data.get("model_output", "")
        tokens = enc.encode(model_output)
        if len(tokens) > 512:
            more_512 += 1
        token_counts.append(len(tokens))

# ===== 输出统计结果 =====
print(f"样本数: {len(token_counts)}")
print(f"平均 token 数: {np.mean(token_counts):.2f}")
print(f"中位数: {np.median(token_counts):.2f}")
print(f"最大 token 数: {np.max(token_counts)}")
print(f"最小 token 数: {np.min(token_counts)}")

print(more_512)
