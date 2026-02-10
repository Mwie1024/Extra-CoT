#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
可视化压缩比率分布的脚本
基于analyze_compression_ratio.py的分析结果，生成多种图表展示保留比例分布
"""

import json
import argparse
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import List, Dict, Any
from analyze_compression_ratio import analyze_compression_ratio

# Set font for better display
plt.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

def create_visualizations(stats: Dict[str, Any], output_dir: str = "compression_visualization"):
    """
    创建多种可视化图表
    
    Args:
        stats: 分析统计结果
        output_dir: 输出目录
    """
    import os
    os.makedirs(output_dir, exist_ok=True)
    
    if 'error' in stats:
        print(f"Error: {stats['error']}")
        return
    
    ratios = stats['retention_ratios']
    sample_ratios = [s['retention_ratio'] for s in stats['sample_details']]
    
    if not ratios:
        print("No valid retention ratio data")
        return
    
    # Set chart style
    plt.style.use('default')
    sns.set_palette("husl")
    
    # 1. Retention ratio histogram (Chunk-level)
    plt.figure(figsize=(12, 8))
    plt.hist(ratios, bins=50, alpha=0.7, color='skyblue', edgecolor='black', linewidth=0.5)
    plt.axvline(np.mean(ratios), color='red', linestyle='--', linewidth=2, label=f'平均值: {np.mean(ratios):.3f}')
    plt.axvline(np.median(ratios), color='green', linestyle='--', linewidth=2, label=f'中位数: {np.median(ratios):.3f}')
    plt.xlabel('Retention Ratio')
    plt.ylabel('Frequency')
    plt.title('Chunk-level Retention Ratio Distribution Histogram')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/chunk_retention_ratio_histogram.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    # 2. Retention ratio boxplot (Chunk-level)
    plt.figure(figsize=(10, 6))
    plt.boxplot(ratios, vert=True, patch_artist=True, 
                boxprops=dict(facecolor='lightblue', alpha=0.7),
                medianprops=dict(color='red', linewidth=2))
    plt.ylabel('Retention Ratio')
    plt.title('Chunk-level Retention Ratio Boxplot')
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/chunk_retention_ratio_boxplot.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    # 3. Sample-level retention ratio distribution
    if sample_ratios:
        plt.figure(figsize=(12, 8))
        plt.hist(sample_ratios, bins=30, alpha=0.7, color='lightcoral', edgecolor='black', linewidth=0.5)
        plt.axvline(np.mean(sample_ratios), color='red', linestyle='--', linewidth=2, label=f'平均值: {np.mean(sample_ratios):.3f}')
        plt.axvline(np.median(sample_ratios), color='green', linestyle='--', linewidth=2, label=f'中位数: {np.median(sample_ratios):.3f}')
        plt.xlabel('Retention Ratio')
        plt.ylabel('Frequency')
        plt.title('Sample-level Retention Ratio Distribution Histogram')
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(f'{output_dir}/sample_retention_ratio_histogram.png', dpi=300, bbox_inches='tight')
        plt.close()
    
    # 4. Cumulative distribution function (CDF)
    plt.figure(figsize=(10, 6))
    sorted_ratios = np.sort(ratios)
    y = np.arange(1, len(sorted_ratios) + 1) / len(sorted_ratios)
    plt.plot(sorted_ratios, y, linewidth=2, color='blue', label='Chunk-level')
    
    if sample_ratios:
        sorted_sample_ratios = np.sort(sample_ratios)
        y_sample = np.arange(1, len(sorted_sample_ratios) + 1) / len(sorted_sample_ratios)
        plt.plot(sorted_sample_ratios, y_sample, linewidth=2, color='red', label='Sample-level')
    
    plt.xlabel('Retention Ratio')
    plt.ylabel('Cumulative Probability')
    plt.title('Retention Ratio Cumulative Distribution Function')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/retention_ratio_cdf.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    # 5. Percentile comparison chart
    plt.figure(figsize=(12, 6))
    percentiles = [10, 25, 50, 75, 90, 95, 99]
    chunk_percentiles = [np.percentile(ratios, p) for p in percentiles]
    
    x = np.arange(len(percentiles))
    width = 0.35
    
    plt.bar(x - width/2, chunk_percentiles, width, label='Chunk-level', alpha=0.7, color='skyblue')
    
    if sample_ratios:
        sample_percentiles = [np.percentile(sample_ratios, p) for p in percentiles]
        plt.bar(x + width/2, sample_percentiles, width, label='Sample-level', alpha=0.7, color='lightcoral')
    
    plt.xlabel('Percentile')
    plt.ylabel('Retention Ratio')
    plt.title('Retention Ratio Percentile Comparison')
    plt.xticks(x, [f'{p}%' for p in percentiles])
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/retention_ratio_percentiles.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    # 6. Density plot (KDE)
    plt.figure(figsize=(12, 6))
    sns.kdeplot(ratios, label='Chunk-level', linewidth=2, alpha=0.7)
    if sample_ratios:
        sns.kdeplot(sample_ratios, label='Sample-level', linewidth=2, alpha=0.7)
    plt.xlabel('Retention Ratio')
    plt.ylabel('Density')
    plt.title('Retention Ratio Density Distribution')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(f'{output_dir}/retention_ratio_density.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    # 7. Statistical summary chart
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 10))
    
    # Basic statistical information
    stats_text = f"""
    Total Samples: {stats['total_samples']:,}
    Total Chunks: {stats['total_chunks']:,}
    Total Tokens: {stats['total_tokens']:,}
    Total Kept Tokens: {stats['total_kept_tokens']:,}
    
    Overall Retention Ratio: {stats['overall_retention_ratio']:.4f} ({stats['overall_retention_ratio']*100:.2f}%)
    Avg Retention Ratio (chunk): {stats['avg_retention_ratio_by_chunk']:.4f} ({stats['avg_retention_ratio_by_chunk']*100:.2f}%)
    Avg Retention Ratio (sample): {stats['avg_retention_ratio_by_sample']:.4f} ({stats['avg_retention_ratio_by_sample']*100:.2f}%)
    
    Valid Chunks: {stats['valid_chunks']:,}
    Zero Retention Chunks: {stats['zero_ratio_chunks']:,}
    Valid Samples: {stats['valid_samples']:,}
    Zero Retention Samples: {stats['zero_ratio_samples']:,}
    """
    ax1.text(0.1, 0.9, stats_text, transform=ax1.transAxes, fontsize=10, 
             verticalalignment='top', fontfamily='monospace')
    ax1.set_title('Statistical Summary')
    ax1.axis('off')
    
    # Retention ratio range distribution
    ranges = ['0-0.1', '0.1-0.2', '0.2-0.3', '0.3-0.4', '0.4-0.5', 
              '0.5-0.6', '0.6-0.7', '0.7-0.8', '0.8-0.9', '0.9-1.0']
    counts = []
    for i in range(10):
        start, end = i * 0.1, (i + 1) * 0.1
        count = sum(1 for r in ratios if start <= r < end)
        counts.append(count)
    
    ax2.bar(ranges, counts, alpha=0.7, color='lightgreen')
    ax2.set_title('Retention Ratio Range Distribution')
    ax2.set_xlabel('Retention Ratio Range')
    ax2.set_ylabel('Number of Chunks')
    ax2.tick_params(axis='x', rotation=45)
    
    # Compression effect distribution
    compression_effects = ['High Compression (0-0.3)', 'Medium Compression (0.3-0.7)', 'Low Compression (0.7-1.0)']
    compression_counts = [
        sum(1 for r in ratios if 0 <= r < 0.3),
        sum(1 for r in ratios if 0.3 <= r < 0.7),
        sum(1 for r in ratios if 0.7 <= r <= 1.0)
    ]
    colors = ['red', 'orange', 'green']
    ax3.pie(compression_counts, labels=compression_effects, autopct='%1.1f%%', 
            colors=colors)
    ax3.set_title('Compression Effect Distribution')
    
    # Sample chunks count distribution
    chunk_counts = [s['chunks'] for s in stats['sample_details']]
    if chunk_counts:
        ax4.hist(chunk_counts, bins=20, alpha=0.7, color='purple', edgecolor='black')
        ax4.set_title('Sample Chunks Count Distribution')
        ax4.set_xlabel('Number of Chunks')
        ax4.set_ylabel('Number of Samples')
    
    plt.tight_layout()
    plt.savefig(f'{output_dir}/compression_summary.png', dpi=300, bbox_inches='tight')
    plt.close()
    
    print(f"Visualization charts saved to directory: {output_dir}/")
    print("Generated chart files:")
    print("  - chunk_retention_ratio_histogram.png: Chunk-level retention ratio histogram")
    print("  - chunk_retention_ratio_boxplot.png: Chunk-level retention ratio boxplot")
    print("  - sample_retention_ratio_histogram.png: Sample-level retention ratio histogram")
    print("  - retention_ratio_cdf.png: Retention ratio cumulative distribution function")
    print("  - retention_ratio_percentiles.png: Retention ratio percentile comparison")
    print("  - retention_ratio_density.png: Retention ratio density distribution")
    print("  - compression_summary.png: Statistical summary")

def main():
    parser = argparse.ArgumentParser(description="Visualize CoT retention ratio distribution")
    parser.add_argument("--input_file", required=True, help="Input JSONL file path")
    parser.add_argument("--output_dir", default="compression_visualization", 
                       help="Output directory (default: compression_visualization)")
    parser.add_argument("--show_plots", action="store_true", help="Show plots (requires GUI environment)")
    
    args = parser.parse_args()
    
    print("Analyzing compression ratio...")
    # Analyze retention ratio
    stats = analyze_compression_ratio(args.input_file)
    
    print("Generating visualization charts...")
    # Create visualization charts
    create_visualizations(stats, args.output_dir)
    
    if args.show_plots:
        plt.show()

if __name__ == "__main__":
    main()
