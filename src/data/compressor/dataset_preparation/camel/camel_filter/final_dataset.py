import json
import os

def combine_question_response(correct_file, filtered_file, output_file):
    """
    从两个文件中提取对应的question和response，组合成新的数据集
    
    Args:
        correct_file: 包含response的文件路径 (merged_camel_18k_correct.jsonl)
        filtered_file: 包含message1的文件路径 (camel_math_id_filtered.jsonl)
        output_file: 输出文件路径
    """
    
    print("开始处理文件...")
    
    # 读取包含response的文件
    print(f"正在读取文件: {correct_file}")
    response_data = {}
    response_count = 0
    
    try:
        with open(correct_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    item_id = data.get('id')
                    if item_id:
                        response_data[item_id] = data.get('response', '')
                        response_count += 1
        print(f"成功读取 {response_count} 条response记录")
    except FileNotFoundError:
        print(f"错误: 文件 {correct_file} 不存在")
        return
    except Exception as e:
        print(f"读取response文件时出错: {e}")
        return
    
    # 读取包含question的文件
    print(f"正在读取文件: {filtered_file}")
    question_data = {}
    question_count = 0
    
    try:
        with open(filtered_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    item_id = data.get('id')
                    if item_id:
                        question_data[item_id] = data.get('message_1', '')
                        question_count += 1
        print(f"成功读取 {question_count} 条question记录")
    except FileNotFoundError:
        print(f"错误: 文件 {filtered_file} 不存在")
        return
    except Exception as e:
        print(f"读取question文件时出错: {e}")
        return
    
    # 找到共同的ID
    common_ids = set(response_data.keys()) & set(question_data.keys())
    print(f"找到 {len(common_ids)} 个匹配的ID")
    
    if len(common_ids) == 0:
        print("警告: 没有找到匹配的ID，请检查文件内容")
        return
    
    # 创建新的数据集
    print("正在创建新数据集...")
    new_samples = []
    
    # 按ID排序以保持一致性
    sorted_ids = sorted(common_ids, key=lambda x: int(x.split('_')[1]) if '_' in x and x.split('_')[1].isdigit() else 0)
    
    for item_id in sorted_ids:
        question = question_data[item_id]
        response = response_data[item_id]
        
        # 创建新样本
        new_sample = {
            "id": item_id,
            "question": question,
            "cot": response
        }
        new_samples.append(new_sample)
    
    # 保存到输出文件
    print(f"正在保存到文件: {output_file}")
    try:
        # 确保输出目录存在
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for sample in new_samples:
                f.write(json.dumps(sample, ensure_ascii=False) + '\n')
        
        print(f"成功保存 {len(new_samples)} 条记录到: {output_file}")
        
    except Exception as e:
        print(f"保存文件时出错: {e}")
        return
    
    # 显示统计信息
    print("\n=== 处理统计 ===")
    print(f"Response文件记录数: {response_count}")
    print(f"Question文件记录数: {question_count}")
    print(f"匹配的记录数: {len(common_ids)}")
    print(f"最终输出记录数: {len(new_samples)}")
    
    # 显示未匹配的ID统计
    response_only = set(response_data.keys()) - common_ids
    question_only = set(question_data.keys()) - common_ids
    
    if response_only:
        print(f"仅在response文件中的ID数量: {len(response_only)}")
    if question_only:
        print(f"仅在question文件中的ID数量: {len(question_only)}")
    
    # 显示前几个样本预览
    print("\n=== 样本预览 ===")
    for i, sample in enumerate(new_samples[:3]):
        print(f"\n样本 {i+1} (ID: {sample['id']}):")
        print(f"Question: {sample['question'][:100]}..." if len(sample['question']) > 100 else f"Question: {sample['question']}")
        print(f"Response: {sample['cot'][:100]}..." if len(sample['cot']) > 100 else f"Response: {sample['cot']}")
    
    return new_samples

def validate_output(output_file):
    """验证输出文件的格式"""
    print(f"\n=== 验证输出文件 ===")
    try:
        with open(output_file, 'r', encoding='utf-8') as f:
            line_count = 0
            valid_samples = 0
            
            for line in f:
                if line.strip():
                    line_count += 1
                    try:
                        data = json.loads(line)
                        if 'id' in data and 'question' in data and 'cot' in data:
                            valid_samples += 1
                        else:
                            print(f"第 {line_count} 行缺少必要字段")
                    except json.JSONDecodeError:
                        print(f"第 {line_count} 行JSON格式错误")
            
            print(f"总行数: {line_count}")
            print(f"有效样本数: {valid_samples}")
            print(f"格式正确率: {valid_samples/line_count*100:.1f}%" if line_count > 0 else "0%")
            
    except Exception as e:
        print(f"验证文件时出错: {e}")

# 使用示例
if __name__ == "__main__":
    # 文件路径
    correct_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/merged_camel_18k_correct.jsonl"
    filtered_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_math_id_filtered.jsonl"
    output_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/FINAL_CAMEL_DATA_CORRECT.jsonl"
    
    # 执行合并
    new_dataset = combine_question_response(correct_file, filtered_file, output_file)
    
    # 验证输出文件
    if new_dataset:
        validate_output(output_file)