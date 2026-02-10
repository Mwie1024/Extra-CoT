import json
import re

input_file = "/data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/data/0.2-1.0_seperate_0.2.json"     # 原始文件
output_file = "./0.2-1.0_seperate_0.2_auto_ratio.json"  # 输出文件

# 匹配 <COMP_数字> 的正则
comp_pattern = re.compile(r"\s*<COMP_(\d+)>$")

def process_item(item):
    inp = item["input"].rstrip()  # 去掉末尾空格
    out = item["output"]

    match = comp_pattern.search(inp)
    if match:
        comp_tag = f"<COMP_{match.group(1)}>"
        inp = comp_pattern.sub("", inp).rstrip()  # 去掉 <COMP_xx>
        out = f"{comp_tag}\n{out.lstrip()}"       # 移到 output 开头并加换行

    item["input"] = inp
    item["output"] = out
    return item

# 读取文件
with open(input_file, "r", encoding="utf-8") as f:
    data = json.load(f)

# 支持单条或多条数据
if isinstance(data, dict):
    data = process_item(data)
else:
    data = [process_item(item) for item in data]

# 写出结果
with open(output_file, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"✅ 数据已处理并保存到: {output_file}")
