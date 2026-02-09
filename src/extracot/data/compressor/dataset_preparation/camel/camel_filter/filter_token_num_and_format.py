import json
import re
from transformers import LongformerTokenizer
import os

def check_think_tags(cot):
    """
    检查response中是否有成对的<think>和</think>标签
    
    Args:
        cot: 要检查的response文本
        
    Returns:
        bool: 如果有成对的标签返回True，否则返回False
    """
    if not cot:
        return False
    
    # 查找所有<think>和</think>标签
    think_open = re.findall(r'<think>', cot)
    think_close = re.findall(r'</think>', cot)
    
    # 检查是否有成对的标签
    if len(think_open) > 0 and len(think_open) == len(think_close):
        return True
    else:
        return False

def filter_dataset(input_file, output_file, max_tokens=4050):
    """
    根据条件筛选数据集
    
    Args:
        input_file: 输入文件路径
        output_file: 输出文件路径
        max_tokens: 最大token数量限制
    """
    
    print("正在初始化Longformer tokenizer...")
    try:
        # 初始化Longformer tokenizer
        tokenizer = LongformerTokenizer.from_pretrained('/Users/mwie/Downloads/longformer')
        print("Longformer tokenizer 初始化成功")
    except Exception as e:
        print(f"初始化tokenizer失败: {e}")
        print("请确保已安装transformers库并且有网络连接下载模型")
        return
    
    print(f"开始处理文件: {input_file}")
    
    # 统计变量
    total_samples = 0
    think_tag_failed = 0
    token_length_failed = 0
    valid_samples = []
    
    # 详细失败原因统计
    no_think_tags = 0
    unpaired_think_tags = 0
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                
                total_samples += 1
                
                try:
                    data = json.loads(line)
                    item_id = data.get('id', f'line_{line_num}')
                    question = data.get('question', '')
                    cot = data.get('cot', '')
                    
                    # 条件1: 检查<think>标签
                    if not check_think_tags(cot):
                        think_tag_failed += 1
                        
                        # 详细分析失败原因
                        think_open_count = len(re.findall(r'<think>', cot))
                        think_close_count = len(re.findall(r'</think>', cot))
                        
                        if think_open_count == 0 and think_close_count == 0:
                            no_think_tags += 1
                        else:
                            unpaired_think_tags += 1
                        
                        if total_samples <= 5:  # 只显示前5个失败的详细信息
                            print(f"ID {item_id}: think标签检查失败 - <think>: {think_open_count}, </think>: {think_close_count}")
                        
                        continue
                    
                    # 条件2: 检查token长度
                    combined_text = question + " " + cot
                    try:
                        tokens = tokenizer.tokenize(combined_text)
                        token_count = len(tokens)
                        
                        if token_count > max_tokens:
                            token_length_failed += 1
                            if len(valid_samples) + token_length_failed <= 5:  # 只显示前5个失败的详细信息
                                print(f"ID {item_id}: token长度超限 - {token_count} > {max_tokens}")
                            continue
                    except Exception as e:
                        print(f"ID {item_id}: tokenize失败 - {e}")
                        token_length_failed += 1
                        continue
                    
                    # 通过所有检查的样本
                    valid_samples.append({
                        'id': item_id,
                        'question': question,
                        'cot': cot,
                        'token_count': token_count
                    })
                    
                    # 进度显示
                    if total_samples % 1000 == 0:
                        print(f"已处理 {total_samples} 条记录，当前有效样本: {len(valid_samples)}")
                
                except json.JSONDecodeError:
                    print(f"第 {line_num} 行JSON格式错误，跳过")
                    continue
                except Exception as e:
                    print(f"处理第 {line_num} 行时出错: {e}")
                    continue
        
    except FileNotFoundError:
        print(f"错误: 文件 {input_file} 不存在")
        return
    except Exception as e:
        print(f"读取文件时出错: {e}")
        return
    
    # 保存过滤后的数据
    print(f"正在保存到文件: {output_file}")
    try:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for sample in valid_samples:
                # 移除token_count字段，只保存原始数据
                output_sample = {
                    'id': sample['id'],
                    'question': sample['question'],
                    'cot': sample['cot']
                }
                f.write(json.dumps(output_sample, ensure_ascii=False) + '\n')
        
        print(f"成功保存 {len(valid_samples)} 条有效记录")
        
    except Exception as e:
        print(f"保存文件时出错: {e}")
        return
    
    # 显示详细统计信息
    print("\n=== 筛选统计 ===")
    print(f"总输入样本数: {total_samples}")
    print(f"有效样本数: {len(valid_samples)}")
    print(f"总过滤样本数: {think_tag_failed + token_length_failed}")
    print(f"过滤率: {(think_tag_failed + token_length_failed) / total_samples * 100:.1f}%")
    print()
    
    print("=== 过滤原因详细统计 ===")
    print(f"think标签问题: {think_tag_failed} 条")
    print(f"  - 完全没有think标签: {no_think_tags} 条")
    print(f"  - think标签不成对: {unpaired_think_tags} 条")
    print(f"token长度超限: {token_length_failed} 条")
    print()
    
    if valid_samples:
        # token长度统计
        token_counts = [sample['token_count'] for sample in valid_samples]
        avg_tokens = sum(token_counts) / len(token_counts)
        max_tokens_in_data = max(token_counts)
        min_tokens_in_data = min(token_counts)
        
        print("=== Token长度统计 ===")
        print(f"平均token长度: {avg_tokens:.1f}")
        print(f"最大token长度: {max_tokens_in_data}")
        print(f"最小token长度: {min_tokens_in_data}")
        
        # 显示前3个样本预览
        print("\n=== 样本预览 ===")
        for i, sample in enumerate(valid_samples[:3]):
            print(f"\n样本 {i+1} (ID: {sample['id']}, Tokens: {sample['token_count']}):")
            print(f"Question: {sample['question'][:100]}..." if len(sample['question']) > 100 else f"Question: {sample['question']}")
            print(f"Response: {sample['cot'][:100]}..." if len(sample['cot']) > 100 else f"Response: {sample['cot']}")

def main():
    """主函数"""
    # 文件路径设置
    input_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_filter/FINAL_CAMEL_DATA_CORRECT.jsonl"
    output_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_filter/LONGFORMER_DATA.jsonl"
    
    # 执行过滤
    filter_dataset(input_file, output_file, max_tokens=4050)

if __name__ == "__main__":
    main()