#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
将JSONL文件中的id字段从字符串转换为整数
"""

import json
import argparse
import os
from typing import Dict, Any

def convert_id_to_int(input_file: str, output_file: str = None) -> None:
    """
    将JSONL文件中的id字段从字符串转换为整数
    
    Args:
        input_file: 输入的JSONL文件路径
        output_file: 输出的JSONL文件路径，如果为None则覆盖原文件
    """
    if output_file is None:
        output_file = input_file
    
    # 创建临时文件
    temp_file = output_file + '.tmp'
    
    converted_count = 0
    total_count = 0
    
    try:
        with open(input_file, 'r', encoding='utf-8') as infile, \
             open(temp_file, 'w', encoding='utf-8') as outfile:
            
            for line_num, line in enumerate(infile, 1):
                line = line.strip()
                if not line:
                    outfile.write(line + '\n')
                    continue
                
                try:
                    data = json.loads(line)
                    total_count += 1
                    
                    # 检查是否有id字段
                    if 'id' in data:
                        # 将id从字符串转换为整数
                        if isinstance(data['id'], str):
                            data['id'] = int(data['id'])
                            converted_count += 1
                        elif isinstance(data['id'], int):
                            # 已经是整数，不需要转换
                            pass
                        else:
                            print(f"警告: 第{line_num}行的id字段类型不是字符串或整数: {type(data['id'])}")
                    
                    # 写入转换后的数据
                    outfile.write(json.dumps(data, ensure_ascii=False) + '\n')
                    
                except json.JSONDecodeError as e:
                    print(f"错误: 第{line_num}行JSON解析失败: {e}")
                    outfile.write(line + '\n')
                except ValueError as e:
                    print(f"错误: 第{line_num}行id转换失败: {e}")
                    outfile.write(line + '\n')
                except Exception as e:
                    print(f"错误: 第{line_num}行处理失败: {e}")
                    outfile.write(line + '\n')
        
        # 如果处理成功，替换原文件
        if output_file != input_file:
            # 如果输出文件不同，直接重命名
            os.rename(temp_file, output_file)
        else:
            # 如果覆盖原文件，先删除原文件再重命名
            os.remove(input_file)
            os.rename(temp_file, output_file)
        
        print(f"转换完成!")
        print(f"总行数: {total_count}")
        print(f"转换的id字段数: {converted_count}")
        print(f"输出文件: {output_file}")
        
    except Exception as e:
        print(f"处理文件时发生错误: {e}")
        # 清理临时文件
        if os.path.exists(temp_file):
            os.remove(temp_file)
        raise

def main():
    parser = argparse.ArgumentParser(description="将JSONL文件中的id字段从字符串转换为整数")
    parser.add_argument("--input_file", required=True, help="输入的JSONL文件路径")
    parser.add_argument("--output_file", help="输出的JSONL文件路径（可选，默认覆盖原文件）")
    parser.add_argument("--backup", action="store_true", help="创建原文件的备份")
    
    args = parser.parse_args()
    
    # 检查输入文件是否存在
    if not os.path.exists(args.input_file):
        print(f"错误: 输入文件不存在: {args.input_file}")
        return
    
    # 创建备份
    if args.backup:
        backup_file = args.input_file + '.backup'
        import shutil
        shutil.copy2(args.input_file, backup_file)
        print(f"已创建备份文件: {backup_file}")
    
    # 执行转换
    convert_id_to_int(args.input_file, args.output_file)

if __name__ == "__main__":
    main()
