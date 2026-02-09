#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
分析压缩比率的脚本
计算每个样本的chunks中masked_text和kept_preview的字符数，并计算平均比值
"""

import json
import argparse
from typing import List, Dict, Any

def analyze_compression_ratio(jsonl_file: str) -> Dict[str, Any]:
    """
    分析JSONL文件中每个样本的保留索引比例
    
    Args:
        jsonl_file: JSONL文件路径
        
    Returns:
        包含统计信息的字典
    """
    total_samples = 0
    total_chunks = 0
    total_tokens = 0
    total_kept_tokens = 0
    compression_ratios = []
    zero_ratio_chunks = 0  # 保留比例为0的chunks数量
    zero_ratio_samples = 0  # 保留比例为0的样本数量
    
    # 存储每个样本的详细信息
    sample_details = []
    
    with open(jsonl_file, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
                
            try:
                sample = json.loads(line)
                total_samples += 1
                
                sample_total_tokens = 0
                sample_kept_tokens = 0
                sample_chunks = 0
                
                chunks = sample.get('chunks', [])
                for chunk in chunks:
                    if isinstance(chunk, dict):
                        index_text = chunk.get('index_text', '')
                        ranges = chunk.get('ranges', [])
                        raw_response = chunk.get('raw_response', '')
                        
                        # 检查raw_response中的ranges是否为空
                        ranges_from_raw = []
                        if raw_response:
                            try:
                                raw_data = json.loads(raw_response)
                                ranges_from_raw = raw_data.get('ranges', [])
                            except (json.JSONDecodeError, TypeError):
                                ranges_from_raw = []
                        
                        # 跳过ranges为空的chunks（包括ranges字段和raw_response中的ranges）
                        if not ranges and not ranges_from_raw:
                            continue
                        
                        # 计算总token数量（从index_text中提取最后一个索引号）
                        total_tokens_in_chunk = 0
                        if index_text:
                            lines = index_text.strip().split('\n')
                            if lines:
                                # 找到最后一个索引号
                                last_line = lines[-1]
                                if '\t' in last_line:
                                    last_index = last_line.split('\t')[0]
                                    try:
                                        total_tokens_in_chunk = int(last_index)
                                    except ValueError:
                                        continue
                        
                        # 计算保留的token数量（从ranges中计算）
                        kept_tokens_in_chunk = 0
                        for range_str in ranges:
                            if '-' in range_str:
                                try:
                                    start, end = map(int, range_str.split('-'))
                                    kept_tokens_in_chunk += (end - start + 1)
                                except ValueError:
                                    continue
                        
                        sample_total_tokens += total_tokens_in_chunk
                        sample_kept_tokens += kept_tokens_in_chunk
                        sample_chunks += 1
                        
                        total_tokens += total_tokens_in_chunk
                        total_kept_tokens += kept_tokens_in_chunk
                        total_chunks += 1
                        
                        # 计算这个chunk的保留比例
                        if total_tokens_in_chunk > 0:
                            ratio = kept_tokens_in_chunk / total_tokens_in_chunk
                            # 只考虑保留比例大于0的情况
                            if ratio > 0:
                                compression_ratios.append(ratio)
                            else:
                                zero_ratio_chunks += 1
                
                # 计算这个样本的保留比例
                if sample_total_tokens > 0:
                    sample_ratio = sample_kept_tokens / sample_total_tokens
                    # 只考虑保留比例大于0的情况
                    if sample_ratio > 0:
                        sample_details.append({
                            'idx': sample.get('idx', line_num),
                            'chunks': sample_chunks,
                            'total_tokens': sample_total_tokens,
                            'kept_tokens': sample_kept_tokens,
                            'retention_ratio': sample_ratio
                        })
                    else:
                        zero_ratio_samples += 1
                
            except json.JSONDecodeError as e:
                print(f"警告: 第{line_num}行JSON解析错误: {e}")
                continue
            except Exception as e:
                print(f"警告: 第{line_num}行处理错误: {e}")
                continue
    
    # 计算统计信息
    if total_chunks == 0:
        return {
            'error': '没有找到有效的chunks数据'
        }
    
    # 总体保留比例
    overall_retention_ratio = total_kept_tokens / total_tokens if total_tokens > 0 else 0
    
    # 平均保留比例（基于每个chunk的比率）
    avg_retention_ratio = sum(compression_ratios) / len(compression_ratios) if compression_ratios else 0
    
    # 样本级别的平均保留比例
    sample_ratios = [s['retention_ratio'] for s in sample_details]
    avg_sample_retention_ratio = sum(sample_ratios) / len(sample_ratios) if sample_ratios else 0
    
    return {
        'total_samples': total_samples,
        'total_chunks': total_chunks,
        'total_tokens': total_tokens,
        'total_kept_tokens': total_kept_tokens,
        'overall_retention_ratio': overall_retention_ratio,
        'avg_retention_ratio_by_chunk': avg_retention_ratio,
        'avg_retention_ratio_by_sample': avg_sample_retention_ratio,
        'retention_ratios': compression_ratios,
        'sample_details': sample_details,
        'zero_ratio_chunks': zero_ratio_chunks,
        'zero_ratio_samples': zero_ratio_samples,
        'valid_chunks': len(compression_ratios),
        'valid_samples': len(sample_details)
    }

def print_statistics(stats: Dict[str, Any], show_details: bool = False):
    """打印统计信息"""
    print("=" * 60)
    print("保留索引比例分析结果")
    print("=" * 60)
    
    if 'error' in stats:
        print(f"错误: {stats['error']}")
        return
    
    print(f"总样本数: {stats['total_samples']}")
    print(f"总chunks数: {stats['total_chunks']}")
    print(f"总token数: {stats['total_tokens']:,}")
    print(f"总保留token数: {stats['total_kept_tokens']:,}")
    print()
    
    print("数据过滤情况:")
    print(f"  有效chunks (保留比例>0): {stats['valid_chunks']}")
    print(f"  零保留比例chunks (已排除): {stats['zero_ratio_chunks']}")
    print(f"  有效样本 (保留比例>0): {stats['valid_samples']}")
    print(f"  零保留比例样本 (已排除): {stats['zero_ratio_samples']}")
    print()
    
    print("保留比例统计:")
    print(f"  总体保留比例: {stats['overall_retention_ratio']:.4f} ({stats['overall_retention_ratio']*100:.2f}%)")
    print(f"  平均保留比例 (按chunk): {stats['avg_retention_ratio_by_chunk']:.4f} ({stats['avg_retention_ratio_by_chunk']*100:.2f}%)")
    print(f"  平均保留比例 (按样本): {stats['avg_retention_ratio_by_sample']:.4f} ({stats['avg_retention_ratio_by_sample']*100:.2f}%)")
    print()
    
    # 保留比例分布
    ratios = stats['retention_ratios']
    if ratios:
        ratios.sort()
        print("保留比例分布:")
        print(f"  最小值: {min(ratios):.4f} ({min(ratios)*100:.2f}%)")
        print(f"  最大值: {max(ratios):.4f} ({max(ratios)*100:.2f}%)")
        print(f"  中位数: {ratios[len(ratios)//2]:.4f} ({ratios[len(ratios)//2]*100:.2f}%)")
        
        # 分位数
        q25_idx = len(ratios) // 4
        q75_idx = 3 * len(ratios) // 4
        print(f"  25%分位数: {ratios[q25_idx]:.4f} ({ratios[q25_idx]*100:.2f}%)")
        print(f"  75%分位数: {ratios[q75_idx]:.4f} ({ratios[q75_idx]*100:.2f}%)")
        print()
    
    if show_details and stats['sample_details']:
        print("样本详细信息:")
        print("-" * 60)
        for detail in stats['sample_details']:
            print(f"样本 {detail['idx']}: {detail['chunks']} chunks, "
                  f"总 {detail['total_tokens']} tokens, "
                  f"保留 {detail['kept_tokens']} tokens, "
                  f"保留比例 {detail['retention_ratio']:.4f} ({detail['retention_ratio']*100:.2f}%)")

def main():
    parser = argparse.ArgumentParser(description="分析CoT保留索引比例")
    parser.add_argument("--input_file", help="输入的JSONL文件路径")
    parser.add_argument("--details", action="store_true", help="显示每个样本的详细信息")
    parser.add_argument("--output", help="输出统计结果到JSON文件")
    
    args = parser.parse_args()
    
    # 分析保留索引比例
    stats = analyze_compression_ratio(args.input_file)
    
    # 打印统计信息
    print_statistics(stats, show_details=args.details)
    
    # 保存到文件
    if args.output:
        # 移除sample_details以减小文件大小（除非用户明确要求）
        output_stats = {k: v for k, v in stats.items() if k != 'sample_details'}
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output_stats, f, ensure_ascii=False, indent=2)
        print(f"\n统计结果已保存到: {args.output}")

if __name__ == "__main__":
    main()
