import json

def check_output_start(file_path):
    print(f"正在检查文件: {file_path} ...")
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("❌ 错误：找不到文件，请检查路径。")
        return
    except json.JSONDecodeError:
        print("❌ 错误：文件不是有效的 JSON 格式。")
        return

    total_count = len(data)
    invalid_samples = []

    for index, item in enumerate(data):
        # 获取 output 内容，如果不存在则为空字符串
        output_content = item.get("output", "")
        
        # 核心检查逻辑：必须以 '<' 开头
        # 如果你允许前面有空格，可以使用 output_content.strip().startswith('<')
        if not output_content.startswith('<'):
            invalid_samples.append({
                "index": index,
                "start_content": output_content[:50] # 截取前50个字符方便预览
            })

    # 输出结果
    print(f"✅ 检查完成！共扫描 {total_count} 条数据。")
    
    if len(invalid_samples) == 0:
        print("🎉 完美！所有数据的 output 都是以 '<' 开头的。")
    else:
        print(f"⚠️ 发现 {len(invalid_samples)} 条数据格式不符合要求：")
        print("-" * 50)
        for sample in invalid_samples:
            # 打印前 5 条错误（避免刷屏），如果很少则全部打印
            if len(invalid_samples) > 20 and invalid_samples.index(sample) >= 20:
                print(f"... 还有 {len(invalid_samples) - 20} 条未显示 ...")
                break
            
            # 显示具体的错误位置和内容预览
            clean_content = sample['start_content'].replace('\n', '\\n')
            print(f"[第 {sample['index']} 条] 开头是: \"{clean_content}\"")
            
        print("-" * 50)
        print("建议：请检查这些样本，看是否是开头有多余的空格或换行符。")

# ==========================================
# 👇 在这里把文件名改成你真实的文件名
json_file_path = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/longformer_pipeline/query_result/autofollow_sft_dataset/qwen3_1.7b_full_ratio_72k_cleaned.json" 
# ==========================================

if __name__ == "__main__":
    check_output_start(json_file_path)