

import json
import tiktoken
import re

# 指定文件路径
jsonl_path = "input.json"


# 选择模型对应的分词器
encoder = tiktoken.encoding_for_model("gpt-4")

# 统计变量
think_token_total = 0
visible_token_total = 0
think_count = 0
visible_count = 0

# 正则匹配 <think>...</think>
think_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL)

with open(jsonl_path, "r", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            text = data.get("model_output", "")
            
            # 提取 <think>...</think> 部分
            think_match = think_pattern.search(text)
            
            if think_match:
                think_text = think_match.group(1)
                visible_text = text[think_match.end():]  # </think> 之后的部分

                # 分别编码统计 token 数
                think_tokens = len(encoder.encode(think_text))
                visible_tokens = len(encoder.encode(visible_text))

                think_token_total += think_tokens
                visible_token_total += visible_tokens
                think_count += 1
                visible_count += 1
            else:
                # 没有 think 标签的行，可选：你可以选择是否统计
                pass

        except json.JSONDecodeError:
            print(f"⚠️ 跳过无效 JSON 行: {line.strip()}")

# 输出结果
if think_count > 0:
    avg_think_tokens = think_token_total / think_count
    avg_visible_tokens = visible_token_total / visible_count
    print(f"平均 <think>...</think> token 数: {avg_think_tokens:.2f}")
    print(f"平均 可见输出部分 token 数: {avg_visible_tokens:.2f}")
else:
    print("没有找到任何包含 <think> 标签的 model_output 数据。")

