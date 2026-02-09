import json

def filter_correct_samples(incorrect_samples_file, all_samples_file, output_file):
    """
    筛选出所有正确的样本
    
    Args:
        incorrect_samples_file: 包含错误答案样本的文件路径
        all_samples_file: 包含所有样本的文件路径
        output_file: 输出正确样本的文件路径
    """
    
    # 读取错误答案样本的ID
    incorrect_ids = set()
    with open(incorrect_samples_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():  # 跳过空行
                data = json.loads(line)
                incorrect_ids.add(data['id'])
    
    print(f"错误样本数量: {len(incorrect_ids)}")
    
    # 读取所有样本，筛选出正确的样本
    correct_samples = []
    total_samples = 0
    
    with open(all_samples_file, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip():  # 跳过空行
                data = json.loads(line)
                total_samples += 1
                
                # 如果ID不在错误样本中，则为正确样本
                if data['id'] not in incorrect_ids:
                    correct_samples.append(data)
    
    print(f"总样本数量: {total_samples}")
    print(f"正确样本数量: {len(correct_samples)}")
    
    # 保存正确样本到输出文件
    with open(output_file, 'w', encoding='utf-8') as f:
        for sample in correct_samples:
            f.write(json.dumps(sample, ensure_ascii=False) + '\n')
    
    print(f"正确样本已保存到: {output_file}")
    
    return correct_samples

# 使用示例
if __name__ == "__main__":
    # 请根据实际情况修改文件路径
    incorrect_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_26k_0714_not_matched.jsonl"
    all_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_26k_0714_qwen3_res.jsonl"  # 请确认这个路径
    output_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_26k_0714_correct.jsonl"
    
    # 执行筛选
    correct_samples = filter_correct_samples(incorrect_file, all_file, output_file)
    
    # 可选：查看一些正确样本的示例
    if correct_samples:
        print("\n前3个正确样本的ID:")
        for i, sample in enumerate(correct_samples[:3]):
            print(f"{i+1}. {sample['id']}")