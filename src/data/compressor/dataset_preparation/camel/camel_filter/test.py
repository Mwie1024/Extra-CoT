import json
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from transformers import LongformerTokenizer
from collections import Counter
import pandas as pd

def analyze_token_distribution(input_file, output_prefix=None):
    """
    分析数据集中question+response的token长度分布
    
    Args:
        input_file: 输入的jsonl文件路径
        output_prefix: 输出文件前缀（可选，用于保存图片）
    """
    
    print("Initializing Longformer tokenizer...")
    try:
        tokenizer = LongformerTokenizer.from_pretrained('allenai/longformer-base-4096')
        print("Longformer tokenizer initialized successfully")
    except Exception as e:
        print(f"Failed to initialize tokenizer: {e}")
        return
    
    print(f"Processing file: {input_file}")
    
    token_lengths = []
    sample_data = []
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                
                try:
                    data = json.loads(line)
                    item_id = data.get('id', f'line_{line_num}')
                    question = data.get('question', '')
                    cot = data.get('cot', '')
                    
                    # Combine question and cot
                    combined_text = question + " " + cot
                    
                    try:
                        # Tokenize and count
                        tokens = tokenizer.tokenize(combined_text)
                        token_count = len(tokens)
                        token_lengths.append(token_count)
                        
                        sample_data.append({
                            'id': item_id,
                            'token_count': token_count,
                            'question_length': len(question),
                            'response_length': len(cot),
                            'char_length': len(combined_text)
                        })
                        
                    except Exception as e:
                        print(f"Tokenization failed for ID {item_id}: {e}")
                        continue
                    
                    # Progress indicator
                    if line_num % 1000 == 0:
                        print(f"Processed {line_num} samples...")
                
                except json.JSONDecodeError:
                    print(f"JSON decode error at line {line_num}, skipping")
                    continue
                except Exception as e:
                    print(f"Error processing line {line_num}: {e}")
                    continue
                    
    except FileNotFoundError:
        print(f"Error: File {input_file} not found")
        return
    except Exception as e:
        print(f"Error reading file: {e}")
        return
    
    if not token_lengths:
        print("No valid samples found")
        return
    
    print(f"Successfully processed {len(token_lengths)} samples")
    
    # Convert to numpy array for easier computation
    token_lengths = np.array(token_lengths)
    
    # Basic statistics
    token_stats = {
        'count': len(token_lengths),
        'mean': np.mean(token_lengths),
        'median': np.median(token_lengths),
        'std': np.std(token_lengths),
        'min': np.min(token_lengths),
        'max': np.max(token_lengths),
        'q25': np.percentile(token_lengths, 25),
        'q75': np.percentile(token_lengths, 75)
    }
    
    # Print statistics
    print("\n=== Token Length Distribution Statistics ===")
    print(f"Total Samples: {token_stats['count']:,}")
    print(f"Mean: {token_stats['mean']:.1f} tokens")
    print(f"Median: {token_stats['median']:.1f} tokens")
    print(f"Standard Deviation: {token_stats['std']:.1f} tokens")
    print(f"Minimum: {token_stats['min']:,} tokens")
    print(f"Maximum: {token_stats['max']:,} tokens")
    print(f"25th Percentile: {token_stats['q25']:.1f} tokens")
    print(f"75th Percentile: {token_stats['q75']:.1f} tokens")
    
    # Range distribution
    ranges = [
        (0, 500), (500, 1000), (1000, 1500), (1500, 2000),
        (2000, 2500), (2500, 3000), (3000, 3500), (3500, 4000), (4000, float('inf'))
    ]
    
    print(f"\n=== Token Length Range Distribution ===")
    for start, end in ranges:
        if end == float('inf'):
            count = np.sum(token_lengths >= start)
            range_str = f"{start}+ tokens"
        else:
            count = np.sum((token_lengths >= start) & (token_lengths < end))
            range_str = f"{start}-{end-1} tokens"
        
        percentage = count / len(token_lengths) * 100
        print(f"{range_str:15}: {count:6,} samples ({percentage:5.1f}%)")
    
    # Create visualizations
    plt.style.use('default')
    fig = plt.figure(figsize=(20, 12))
    
    # 1. Histogram
    plt.subplot(2, 3, 1)
    plt.hist(token_lengths, bins=50, alpha=0.7, color='skyblue', edgecolor='black')
    plt.xlabel('Token Length')
    plt.ylabel('Frequency')
    plt.title('Token Length Distribution (Histogram)')
    plt.grid(True, alpha=0.3)
    
    # Add statistics text
    stats_text = f'Mean: {token_stats["mean"]:.0f}\nMedian: {token_stats["median"]:.0f}\nStd: {token_stats["std"]:.0f}'
    plt.text(0.02, 0.98, stats_text, transform=plt.gca().transAxes, 
             verticalalignment='top', bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
    
    # 2. Box plot
    plt.subplot(2, 3, 2)
    plt.boxplot(token_lengths, vert=True)
    plt.ylabel('Token Length')
    plt.title('Token Length Distribution (Box Plot)')
    plt.grid(True, alpha=0.3)
    
    # 3. Cumulative distribution
    plt.subplot(2, 3, 3)
    sorted_lengths = np.sort(token_lengths)
    cumulative_prob = np.arange(1, len(sorted_lengths) + 1) / len(sorted_lengths)
    plt.plot(sorted_lengths, cumulative_prob * 100, color='red', linewidth=2)
    plt.xlabel('Token Length')
    plt.ylabel('Cumulative Percentage (%)')
    plt.title('Cumulative Distribution Function')
    plt.grid(True, alpha=0.3)
    
    # Add percentile lines
    for percentile in [25, 50, 75, 90, 95]:
        value = np.percentile(token_lengths, percentile)
        plt.axvline(x=value, color='gray', linestyle='--', alpha=0.7)
        plt.text(value, percentile + 2, f'P{percentile}={value:.0f}', 
                rotation=90, fontsize=8, ha='right')
    
    # 4. Range distribution bar chart
    plt.subplot(2, 3, 4)
    range_labels = []
    range_counts = []
    
    for start, end in ranges:
        if end == float('inf'):
            count = np.sum(token_lengths >= start)
            range_labels.append(f'{start}+')
        else:
            count = np.sum((token_lengths >= start) & (token_lengths < end))
            range_labels.append(f'{start}-{end-1}')
        range_counts.append(count)
    
    bars = plt.bar(range_labels, range_counts, color='lightcoral', alpha=0.7)
    plt.xlabel('Token Length Range')
    plt.ylabel('Number of Samples')
    plt.title('Sample Count by Token Length Range')
    plt.xticks(rotation=45)
    plt.grid(True, alpha=0.3)
    
    # Add value labels on bars
    for bar, count in zip(bars, range_counts):
        plt.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(range_counts)*0.01, 
                str(count), ha='center', va='bottom', fontsize=9)
    
    # 5. Density plot
    plt.subplot(2, 3, 5)
    plt.hist(token_lengths, bins=100, density=True, alpha=0.7, color='lightgreen')
    
    # Add kernel density estimation
    from scipy import stats
    density = stats.gaussian_kde(token_lengths)
    x_range = np.linspace(token_lengths.min(), token_lengths.max(), 200)
    plt.plot(x_range, density(x_range), 'r-', linewidth=2, label='KDE')
    
    plt.xlabel('Token Length')
    plt.ylabel('Density')
    plt.title('Token Length Density Distribution')
    plt.legend()
    plt.grid(True, alpha=0.3)
    
    # 6. Percentile analysis
    plt.subplot(2, 3, 6)
    percentiles = np.arange(1, 101)
    percentile_values = [np.percentile(token_lengths, p) for p in percentiles]
    
    plt.plot(percentiles, percentile_values, 'b-', linewidth=2)
    plt.xlabel('Percentile')
    plt.ylabel('Token Length')
    plt.title('Token Length by Percentile')
    plt.grid(True, alpha=0.3)
    
    # Highlight key percentiles
    key_percentiles = [25, 50, 75, 90, 95, 99]
    for p in key_percentiles:
        value = np.percentile(token_lengths, p)
        plt.plot(p, value, 'ro', markersize=8)
        plt.text(p, value + token_stats['max']*0.02, f'{value:.0f}', 
                ha='center', va='bottom', fontweight='bold')
    
    plt.tight_layout()
    
    # Save the plot if output prefix is provided
    if output_prefix:
        plt.savefig(f'{output_prefix}_token_distribution.png', dpi=300, bbox_inches='tight')
        print(f"Visualization saved as {output_prefix}_token_distribution.png")
    
    plt.show()
    
    # Additional detailed analysis
    print(f"\n=== Additional Analysis ===")
    
    # Most common token lengths
    length_counter = Counter(token_lengths)
    most_common = length_counter.most_common(10)
    print("Top 10 Most Common Token Lengths:")
    for length, count in most_common:
        percentage = count / len(token_lengths) * 100
        print(f"  {length:4d} tokens: {count:5d} samples ({percentage:4.1f}%)")
    
    # Samples beyond certain thresholds
    thresholds = [1000, 2000, 3000, 4000]
    print(f"\nSamples Beyond Thresholds:")
    for threshold in thresholds:
        count = np.sum(token_lengths > threshold)
        percentage = count / len(token_lengths) * 100
        print(f"  > {threshold:4d} tokens: {count:5d} samples ({percentage:5.1f}%)")
    
    # Create DataFrame for further analysis if needed
    df = pd.DataFrame(sample_data)
    
    return {
        'statistics': token_stats,
        'token_lengths': token_lengths,
        'sample_data': df,
        'range_distribution': dict(zip([f"{s}-{e-1}" if e != float('inf') else f"{s}+" 
                                      for s, e in ranges], range_counts))
    }

def main():
    """Main function"""
    # File path
    input_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_filter/LONGFORMER_DATA.jsonl"
    output_prefix = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/token_analysis"
    
    # Run analysis
    results = analyze_token_distribution(input_file, output_prefix)
    
    if results:
        print("\nAnalysis completed successfully!")
        print("Results include:")
        print("- Comprehensive statistics")
        print("- Multiple visualization plots")
        print("- Detailed distribution analysis")

if __name__ == "__main__":
    main()