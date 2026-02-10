#!/usr/bin/env python3
"""
检查CHUNKED_RESULT.jsonl文件中每个样本的validation_info元素中的token_distribution中tokens的分布情况
"""

import json
import statistics
from collections import Counter
import matplotlib.pyplot as plt
import numpy as np

def analyze_token_distribution(file_path):
    """
    分析JSONL文件中每个样本的token分布情况
    """
    token_counts = []
    chunk_counts = []
    sample_stats = []
    high_token_samples = []  # 存储token数超过1000的样本信息
    min_token_samples = []
    
    print("正在读取文件...")
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            try:
                data = json.loads(line.strip())
                
                # 检查是否有validation_info和token_distribution
                if 'validation_info' in data and 'token_distribution' in data['validation_info']:
                    token_dist = data['validation_info']['token_distribution']
                    
                    # 提取每个chunk的token数量
                    sample_tokens = [chunk['tokens'] for chunk in token_dist]
                    token_counts.extend(sample_tokens)
                    chunk_counts.append(len(sample_tokens))
                    
                    # 检查是否有token数超过1000的chunk
                    max_tokens_in_sample = max(sample_tokens) if sample_tokens else 0
                    if max_tokens_in_sample > 1000:
                        high_token_samples.append({
                            'sample_id': data.get('id', f'line_{line_num}'),
                            'max_tokens': max_tokens_in_sample,
                            'token_distribution': sample_tokens,
                            'chunk_count': len(sample_tokens)
                        })
                    
                    min_tokens_in_sample = min(sample_tokens) if sample_tokens else 999999
                    if min_tokens_in_sample < 200:
                        min_token_samples.append({
                            'sample_id': data.get('id', f'line_{line_num}'),
                            'min_tokens': min_tokens_in_sample,
                            'token_distribution': sample_tokens,
                            'chunk_count': len(sample_tokens)
                        })
                    
                    # 记录每个样本的统计信息
                    sample_stats.append({
                        'sample_id': data.get('id', f'line_{line_num}'),
                        'chunk_count': len(sample_tokens),
                        'total_tokens': sum(sample_tokens),
                        'min_tokens': min(sample_tokens) if sample_tokens else 0,
                        'max_tokens': max_tokens_in_sample,
                        'avg_tokens': statistics.mean(sample_tokens) if sample_tokens else 0,
                        'token_distribution': sample_tokens
                    })
                else:
                    print(f"警告: 第{line_num}行缺少validation_info或token_distribution")
                    
            except json.JSONDecodeError as e:
                print(f"错误: 第{line_num}行JSON解析失败: {e}")
            except Exception as e:
                print(f"错误: 第{line_num}行处理失败: {e}")
    
    return token_counts, chunk_counts, sample_stats, high_token_samples, min_token_samples

def print_statistics(token_counts, chunk_counts, sample_stats, high_token_samples, min_token_samples):
    """
    打印统计信息
    """
    print("\n" + "="*60)
    print("TOKEN DISTRIBUTION ANALYSIS REPORT")
    print("="*60)
    
    # 总体token统计
    print(f"\n📊 Overall Token Statistics:")
    print(f"  Total samples: {len(sample_stats)}")
    print(f"  Total chunks: {len(token_counts)}")
    print(f"  Total tokens: {sum(token_counts):,}")
    print(f"  Average tokens per chunk: {statistics.mean(token_counts):.2f}")
    print(f"  Median tokens: {statistics.median(token_counts):.2f}")
    print(f"  Token standard deviation: {statistics.stdev(token_counts):.2f}")
    print(f"  Minimum tokens: {min(token_counts)}")
    print(f"  Maximum tokens: {max(token_counts)}")
    
    # Chunk数量统计
    print(f"\n📦 Chunk Count Statistics:")
    chunk_counter = Counter(chunk_counts)
    print(f"  Average chunks per sample: {statistics.mean(chunk_counts):.2f}")
    print(f"  Median chunks: {statistics.median(chunk_counts):.2f}")
    print(f"  Minimum chunks: {min(chunk_counts)}")
    print(f"  Maximum chunks: {max(chunk_counts)}")
    print(f"  Chunk count distribution:")
    for chunk_num in sorted(chunk_counter.keys()):
        print(f"    {chunk_num} chunks: {chunk_counter[chunk_num]} samples")
    
    # Token数量区间统计
    print(f"\n🎯 Token Range Statistics:")
    token_ranges = [
        (0, 100, "0-100"),
        (100, 200, "100-200"),
        (200, 300, "200-300"),
        (300, 400, "300-400"),
        (400, 500, "400-500"),
        (500, 600, "500-600"),
        (600, 700, "600-700"),
        (700, 800, "700-800"),
        (800, 900, "800-900"),
        (900, 1000, "900-1000"),
        (1000, float('inf'), "1000+")
    ]
    
    for min_val, max_val, label in token_ranges:
        count = sum(1 for tokens in token_counts if min_val <= tokens < max_val)
        percentage = (count / len(token_counts)) * 100
        print(f"  {label:>8}: {count:>6} chunks ({percentage:>5.1f}%)")
    
    # 样本统计
    print(f"\n📈 Sample-level Statistics:")
    total_tokens_per_sample = [s['total_tokens'] for s in sample_stats]
    print(f"  Average total tokens per sample: {statistics.mean(total_tokens_per_sample):.2f}")
    print(f"  Median total tokens per sample: {statistics.median(total_tokens_per_sample):.2f}")
    print(f"  Minimum total tokens per sample: {min(total_tokens_per_sample)}")
    print(f"  Maximum total tokens per sample: {max(total_tokens_per_sample)}")
    
    # 高token数样本信息
    if high_token_samples:
        print(f"\n🚨 Samples with tokens > 1000:")
        print(f"  Found {len(high_token_samples)} samples with chunks containing > 1000 tokens:")
        for sample in high_token_samples:
            print(f"    Sample ID: {sample['sample_id']}")
            print(f"    Max tokens: {sample['max_tokens']}")
            print(f"    Chunk count: {sample['chunk_count']}")
            print(f"    Token distribution: {sample['token_distribution']}")
            print()
    else:
        print(f"\n✅ No samples found with tokens > 1000")

    # 低token数样本信息
    if min_token_samples:
        print(f"\n🚨 Samples with tokens < 1000:")
        print(f"  Found {len(min_token_samples)} samples with chunks containing < 1000 tokens:")
        for sample in min_token_samples:
            print(f"    Sample ID: {sample['sample_id']}")
            print(f"    Min tokens: {sample['min_tokens']}")
            print(f"    Chunk count: {sample['chunk_count']}")
            print(f"    Token distribution: {sample['token_distribution']}")
            print()
    else:
        print(f"\n✅ No samples found with tokens < 1000")

def print_sample_details(sample_stats, num_samples=5):
    """
    打印前几个样本的详细信息
    """
    print(f"\n🔍 Details of first {num_samples} samples:")
    print("-" * 80)
    
    for i, sample in enumerate(sample_stats[:num_samples]):
        print(f"\nSample {i+1}: {sample['sample_id']}")
        print(f"  Chunk count: {sample['chunk_count']}")
        print(f"  Total tokens: {sample['total_tokens']}")
        print(f"  Average tokens: {sample['avg_tokens']:.2f}")
        print(f"  Token range: {sample['min_tokens']} - {sample['max_tokens']}")
        print(f"  Token distribution: {sample['token_distribution']}")

def create_visualizations(token_counts, chunk_counts, sample_stats):
    """
    创建可视化图表
    """
    print(f"\n📊 Generating visualization charts...")
    
    # 创建子图
    fig, axes = plt.subplots(2, 2, figsize=(15, 12))
    fig.suptitle('Token Distribution Analysis', fontsize=16, fontweight='bold')
    
    # 1. Token数量直方图
    axes[0, 0].hist(token_counts, bins=50, alpha=0.7, color='skyblue', edgecolor='black')
    axes[0, 0].set_title('Chunk Token Count Distribution')
    axes[0, 0].set_xlabel('Token Count')
    axes[0, 0].set_ylabel('Frequency')
    axes[0, 0].grid(True, alpha=0.3)
    
    # 2. Chunk数量分布
    chunk_counter = Counter(chunk_counts)
    chunk_nums = sorted(chunk_counter.keys())
    chunk_freqs = [chunk_counter[num] for num in chunk_nums]
    axes[0, 1].bar(chunk_nums, chunk_freqs, alpha=0.7, color='lightgreen', edgecolor='black')
    axes[0, 1].set_title('Chunk Count Distribution per Sample')
    axes[0, 1].set_xlabel('Number of Chunks')
    axes[0, 1].set_ylabel('Number of Samples')
    axes[0, 1].grid(True, alpha=0.3)
    
    # 3. 样本总token数分布
    total_tokens_per_sample = [s['total_tokens'] for s in sample_stats]
    axes[1, 0].hist(total_tokens_per_sample, bins=30, alpha=0.7, color='lightcoral', edgecolor='black')
    axes[1, 0].set_title('Total Token Count Distribution per Sample')
    axes[1, 0].set_xlabel('Total Token Count')
    axes[1, 0].set_ylabel('Number of Samples')
    axes[1, 0].grid(True, alpha=0.3)
    
    # 4. Token数量箱线图
    axes[1, 1].boxplot(token_counts, vert=True, patch_artist=True, 
                       boxprops=dict(facecolor='lightyellow', alpha=0.7))
    axes[1, 1].set_title('Token Count Box Plot')
    axes[1, 1].set_ylabel('Token Count')
    axes[1, 1].grid(True, alpha=0.3)
    
    plt.tight_layout()
    
    # 保存图表
    output_file = '/Users/mwie/User/Data/Code/CoT Language/CoT_Language/token_distribution_analysis.png'
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    print(f"Chart saved to: {output_file}")
    
    plt.show()

def main():
    """
    主函数
    """
    file_path = '/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/CHUNKED_RESULT.jsonl'
    
    print("Starting token distribution analysis...")
    print(f"File path: {file_path}")
    
    # 分析数据
    token_counts, chunk_counts, sample_stats, high_token_samples, min_token_samples = analyze_token_distribution(file_path)
    
    if not token_counts:
        print("Error: No valid token data found")
        return
    
    # 打印统计信息
    print_statistics(token_counts, chunk_counts, sample_stats, high_token_samples, min_token_samples)
    
    # 打印样本详情
    print_sample_details(sample_stats, num_samples=5)
    
    # 创建可视化图表
    try:
        create_visualizations(token_counts, chunk_counts, sample_stats)
    except ImportError:
        print("\nNote: matplotlib not installed, skipping visualization chart generation")
    except Exception as e:
        print(f"\nVisualization chart generation failed: {e}")
    
    print(f"\n✅ Analysis completed!")

if __name__ == "__main__":
    main()
