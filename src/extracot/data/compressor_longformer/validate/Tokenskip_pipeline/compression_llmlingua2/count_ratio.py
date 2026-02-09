import json
import tiktoken

# 选择模型对应的编码器（例如 gpt-4 或 gpt-3.5-turbo）
encoding = tiktoken.encoding_for_model("gpt-4")

# 你的 JSONL 文件路径
jsonl_path = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/longformer_pipeline/metamath-8k/train_outputs_compressed_ratio_0.8.jsonl"

ratios = []
with open(jsonl_path, "r", encoding="utf-8") as f:
    for line in f:
        sample = json.loads(line)
        cot = sample.get("cot", "")
        compressed_cot = sample.get("compressed_cot", "")
        
        cot_tokens = len(encoding.encode(cot))
        compressed_tokens = len(encoding.encode(compressed_cot))
        
        if cot_tokens > 0:
            ratio = compressed_tokens / cot_tokens
            ratios.append(ratio)

# 输出每条样本的比值和平均比值
for i, r in enumerate(ratios, 1):
    print(f"Sample {i}: ratio = {r:.4f}")

if ratios:
    avg_ratio = sum(ratios) / len(ratios)
    print(f"\nAverage compression ratio: {avg_ratio:.4f}")
else:
    print("No valid samples found.")
