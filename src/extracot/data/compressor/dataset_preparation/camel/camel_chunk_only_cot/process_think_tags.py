#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
处理包含<think>和</think>标签的JSONL文件
- 检查每个样本是否包含完整的<think>和</think>标签
- 提取<think>和</think>之间的内容
- 统计字符数变化情况
"""

import json
import re
import os
from typing import Dict, List, Tuple

def process_think_tags(input_file: str, output_file: str) -> None:
    """
    处理JSONL文件，提取<think>和</think>之间的内容
    
    Args:
        input_file: 输入文件路径
        output_file: 输出文件路径
    """
    processed_samples = []
    total_samples = 0
    valid_samples = 0
    total_original_chars = 0
    total_processed_chars = 0
    
    print(f"开始处理文件: {input_file}")
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
                
            try:
                # 解析JSON
                sample = json.loads(line)
                total_samples += 1
                
                # 获取cot字段
                cot = sample.get('cot', '')
                original_chars = len(cot)
                total_original_chars += original_chars
                
                # 检查是否包含<think>和</think>标签
                if '<think>' not in cot or '</think>' not in cot:
                    print(f"样本 {sample.get('id', f'line_{line_num}')} 缺少<think>或</think>标签，跳过")
                    continue
                
                # 提取<think>和</think>之间的内容
                think_pattern = r'<think>(.*?)</think>'
                matches = re.findall(think_pattern, cot, re.DOTALL)
                
                if not matches:
                    print(f"样本 {sample.get('id', f'line_{line_num}')} 未找到<think>内容，跳过")
                    continue
                
                # 合并所有<think>块的内容
                think_content = '\n\n'.join(matches)
                processed_chars = len(think_content)
                total_processed_chars += processed_chars
                
                # 创建新的样本
                new_sample = {
                    'id': sample.get('id', f'item_{line_num}'),
                    'question': sample.get('question', ''),
                    'cot': think_content
                }
                
                processed_samples.append(new_sample)
                valid_samples += 1
                
                # 输出每个样本的统计信息
                print(f"样本 {new_sample['id']}: 原始字符数 {original_chars}, 处理后字符数 {processed_chars}, 减少 {original_chars - processed_chars} 字符")
                
            except json.JSONDecodeError as e:
                print(f"第 {line_num} 行JSON解析错误: {e}")
                continue
            except Exception as e:
                print(f"处理第 {line_num} 行时出错: {e}")
                continue
    
    # 保存处理后的数据
    with open(output_file, 'w', encoding='utf-8') as f:
        for sample in processed_samples:
            f.write(json.dumps(sample, ensure_ascii=False) + '\n')
    
    # 输出总体统计信息
    print("\n" + "="*60)
    print("处理完成！统计信息：")
    print(f"总样本数: {total_samples}")
    print(f"有效样本数: {valid_samples}")
    print(f"淘汰样本数: {total_samples - valid_samples}")
    print(f"总原始字符数: {total_original_chars:,}")
    print(f"总处理后字符数: {total_processed_chars:,}")
    print(f"总减少字符数: {total_original_chars - total_processed_chars:,}")
    print(f"平均每个样本原始字符数: {total_original_chars / total_samples if total_samples > 0 else 0:.1f}")
    print(f"平均每个样本处理后字符数: {total_processed_chars / valid_samples if valid_samples > 0 else 0:.1f}")
    print(f"平均每个样本减少字符数: {(total_original_chars - total_processed_chars) / valid_samples if valid_samples > 0 else 0:.1f}")
    print(f"字符压缩率: {((total_original_chars - total_processed_chars) / total_original_chars * 100) if total_original_chars > 0 else 0:.1f}%")
    print(f"输出文件: {output_file}")

def main():
    """主函数"""
    input_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk_only_cot/successful_latex_samples.jsonl"
    output_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk_only_cot/processed_think_samples.jsonl"
    
    # 检查输入文件是否存在
    if not os.path.exists(input_file):
        print(f"错误: 输入文件不存在: {input_file}")
        return
    
    # 确保输出目录存在
    output_dir = os.path.dirname(output_file)
    os.makedirs(output_dir, exist_ok=True)
    
    # 处理文件
    process_think_tags(input_file, output_file)

if __name__ == "__main__":
    main()
