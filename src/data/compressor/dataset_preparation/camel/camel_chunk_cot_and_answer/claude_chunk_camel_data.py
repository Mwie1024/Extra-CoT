import re
from typing import List, Tuple, Dict, Any
from dataclasses import dataclass
try:
    from transformers import LongformerTokenizerFast
    TOKENIZER_CLASS = LongformerTokenizerFast
except ImportError:
    from transformers import LongformerTokenizer
    TOKENIZER_CLASS = LongformerTokenizer

# 导入用户的公式识别函数
from math_tokenizer_no_space import latex_spans, find_formula_spans_nolatex, merge_spans

@dataclass
class Sentence:
    content: str
    start: int
    end: int
    tokens: int = 0
    contains_math: bool = False

class SmartCoTChunker:
    def __init__(self, tokenizer_name: str = 'allenai/longformer-base-4096'):
        """初始化chunker"""
        self.tokenizer = TOKENIZER_CLASS.from_pretrained(tokenizer_name)
        self.target_tokens = 512
        self.safe_window_start = 450
        self.min_merge_tokens = 128

    def get_token_count(self, text: str) -> int:
        """获取文本的token数量"""
        if not text.strip():
            return 0
        return len(self.tokenizer.tokenize(text))

    def get_all_math_spans(self, text: str) -> List[Tuple[int, int]]:
        """获取所有数学公式的位置区间"""
        latex_math_spans = latex_spans(text)
        nolatex_math_spans = find_formula_spans_nolatex(text)
        return merge_spans(latex_math_spans + nolatex_math_spans)

    def is_position_in_math(self, position: int, math_spans: List[Tuple[int, int]]) -> bool:
        """检查某个位置是否在数学公式内"""
        for start, end in math_spans:
            if start <= position < end:
                return True
        return False

    def split_into_sentences(self, text: str) -> List[Sentence]:
        """将文本按句子分割，但避开数学公式内的标点符号"""
        math_spans = self.get_all_math_spans(text)
        
        # 句子结束标记的正则模式
        sentence_endings = [
            r'[。.!?！？]',  # 中英文句号、感叹号、问号
            r'✓',            # 验证符号
            r'[；;]',        # 分号
        ]
        
        sentences = []
        current_start = 0
        
        # 逐字符扫描，寻找句子边界
        i = 0
        while i < len(text):
            char = text[i]
            
            # 检查是否是潜在的句子结束符
            is_potential_ending = False
            for pattern in sentence_endings:
                if re.match(pattern, char):
                    is_potential_ending = True
                    break
            
            if is_potential_ending:
                # 检查这个位置是否在数学公式内
                if not self.is_position_in_math(i, math_spans):
                    # 这是一个真正的句子边界
                    # 句子在结束符处结束
                    sentence_content = text[current_start:i+1].strip()
                    if sentence_content:
                        # 检查这个句子是否包含数学公式
                        contains_math = any(
                            start < current_start + len(sentence_content) and end > current_start
                            for start, end in math_spans
                        )
                        
                        sentence = Sentence(
                            content=sentence_content,
                            start=current_start,
                            end=i+1,
                            tokens=self.get_token_count(sentence_content),
                            contains_math=contains_math
                        )
                        sentences.append(sentence)
                    
                    # 跳过句子结束符后的空白字符，开始下一个句子
                    next_start = i + 1
                    while next_start < len(text) and text[next_start].isspace():
                        next_start += 1
                    
                    current_start = next_start
                    i = next_start
                    continue
            
            i += 1
        
        # 处理最后的文本片段
        if current_start < len(text):
            remaining_content = text[current_start:].strip()
            if remaining_content:
                contains_math = any(
                    start < len(text) and end > current_start
                    for start, end in math_spans
                )
                
                sentence = Sentence(
                    content=remaining_content,
                    start=current_start,
                    end=len(text),
                    tokens=self.get_token_count(remaining_content),
                    contains_math=contains_math
                )
                sentences.append(sentence)
        
        return sentences

    def merge_short_sentences(self, sentences: List[Sentence]) -> List[Sentence]:
        """合并过短的句子片段"""
        if not sentences:
            return []
        
        merged = []
        current_group = [sentences[0]]
        current_tokens = sentences[0].tokens
        
        for sentence in sentences[1:]:
            # 如果当前组合的token数还很少，继续合并
            if current_tokens < 50 and len(current_group) < 3:
                current_group.append(sentence)
                current_tokens += sentence.tokens
            else:
                # 创建合并后的句子
                if len(current_group) == 1:
                    merged.append(current_group[0])
                else:
                    combined_content = ' '.join(s.content for s in current_group)
                    combined_sentence = Sentence(
                        content=combined_content,
                        start=current_group[0].start,
                        end=current_group[-1].end,
                        tokens=current_tokens,
                        contains_math=any(s.contains_math for s in current_group)
                    )
                    merged.append(combined_sentence)
                
                # 开始新的组
                current_group = [sentence]
                current_tokens = sentence.tokens
        
        # 处理最后一组
        if current_group:
            if len(current_group) == 1:
                merged.append(current_group[0])
            else:
                combined_content = ' '.join(s.content for s in current_group)
                combined_sentence = Sentence(
                    content=combined_content,
                    start=current_group[0].start,
                    end=current_group[-1].end,
                    tokens=sum(s.tokens for s in current_group),
                    contains_math=any(s.contains_math for s in current_group)
                )
                merged.append(combined_sentence)
        
        return merged

    def calculate_sentence_split_score(self, sentence: Sentence, current_tokens: int) -> float:
        """计算在某个句子后分割的评分"""
        # 距离目标token数的惩罚
        distance_penalty = 1.0 - abs(current_tokens - self.target_tokens) / 62.0
        distance_penalty = max(0.1, distance_penalty)
        
        # 根据句子特征确定优先级权重
        content = sentence.content.strip()
        
        # 逻辑结论句子（因此、所以等）
        if re.search(r'(因此|所以|综上|总之|Therefore|Thus|Hence)[^。]*[。.!?！？✓]\s*$', content):
            priority_weight = 10.0
        
        # 完整句子结尾（句号、感叹号、问号）
        elif re.search(r'[。.!?！？]\s*$', content):
            priority_weight = 9.0
        
        # 验证符号结尾
        elif re.search(r'✓\s*$', content):
            priority_weight = 9.0
        
        # 包含数学公式的句子
        elif sentence.contains_math:
            priority_weight = 8.0
        
        # 分号结尾
        elif re.search(r'[；;]\s*$', content):
            priority_weight = 6.0
        
        # 冒号结尾
        elif re.search(r'[：:]\s*$', content):
            priority_weight = 4.0
        
        # 普通句子
        else:
            priority_weight = 5.0
        
        return priority_weight * distance_penalty

    def smart_chunk_split(self, text: str) -> List[str]:
        """基于句子边界的智能分割"""
        # 1. 将文本分割为句子
        sentences = self.split_into_sentences(text)
        
        # 2. 合并过短的句子片段
        sentences = self.merge_short_sentences(sentences)
        
        if not sentences:
            return [text] if text.strip() else []
        
        # 3. 基于句子边界进行chunk分割
        chunks = []
        current_chunk_sentences = []
        current_tokens = 0
        
        i = 0
        while i < len(sentences):
            sentence = sentences[i]
            
            # 检查添加这个句子是否会超过限制
            if (current_tokens + sentence.tokens > self.target_tokens and 
                current_tokens >= self.safe_window_start and 
                current_chunk_sentences):
                
                # 需要分割，寻找最佳分割点
                best_split_idx = self.find_best_sentence_split(
                    current_chunk_sentences, sentences[i:i+5]  # 向前看5个句子
                )
                
                # 创建chunk
                chunk_content = ' '.join(s.content for s in current_chunk_sentences[:best_split_idx+1])
                chunks.append(chunk_content)
                
                # 更新状态
                remaining_sentences = current_chunk_sentences[best_split_idx+1:]
                current_chunk_sentences = remaining_sentences + [sentence]
                current_tokens = sum(s.tokens for s in current_chunk_sentences)
                i += 1
                
            else:
                # 直接添加句子
                current_chunk_sentences.append(sentence)
                current_tokens += sentence.tokens
                i += 1
        
        # 处理最后的句子
        if current_chunk_sentences:
            chunk_content = ' '.join(s.content for s in current_chunk_sentences)
            chunks.append(chunk_content)
        
        return self.merge_last_chunk_if_needed(chunks)

    def find_best_sentence_split(self, current_sentences: List[Sentence], 
                                upcoming_sentences: List[Sentence]) -> int:
        """在当前句子中找到最佳分割点"""
        if not current_sentences:
            return 0
        
        best_idx = len(current_sentences) - 1  # 默认在最后分割
        best_score = -1
        cumulative_tokens = 0
        
        # 计算到每个句子的累积token数，寻找最佳分割点
        for i, sentence in enumerate(current_sentences):
            cumulative_tokens += sentence.tokens
            
            if cumulative_tokens >= self.safe_window_start:
                score = self.calculate_sentence_split_score(sentence, cumulative_tokens)
                if score > best_score:
                    best_score = score
                    best_idx = i
        
        # 也考虑加上接下来的句子是否有更好的分割点
        for i, sentence in enumerate(upcoming_sentences):
            cumulative_tokens += sentence.tokens
            if cumulative_tokens > self.target_tokens + 100:  # 硬限制
                break
            
            score = self.calculate_sentence_split_score(sentence, cumulative_tokens)
            if score > best_score:
                best_score = score
                best_idx = len(current_sentences) + i
        
        # 确保不超出current_sentences的范围
        return min(best_idx, len(current_sentences) - 1)

    def merge_last_chunk_if_needed(self, chunks: List[str]) -> List[str]:
        """处理最后chunk的合并"""
        if len(chunks) <= 1:
            return chunks
        
        last_chunk_tokens = self.get_token_count(chunks[-1])
        
        if last_chunk_tokens < self.min_merge_tokens:
            merged_content = chunks[-2] + ' ' + chunks[-1]
            merged_tokens = self.get_token_count(merged_content)
            
            if merged_tokens <= self.target_tokens + 50:
                return chunks[:-2] + [merged_content]
        
        return chunks

    def analyze_sentence_structure(self, text: str) -> str:
        """分析句子结构（调试用）"""
        sentences = self.split_into_sentences(text)
        sentences = self.merge_short_sentences(sentences)
        
        output = ["=== SENTENCE ANALYSIS ==="]
        cumulative_tokens = 0
        
        for i, sentence in enumerate(sentences):
            cumulative_tokens += sentence.tokens
            math_indicator = "📐" if sentence.contains_math else "📝"
            
            # 计算分割评分
            score = self.calculate_sentence_split_score(sentence, cumulative_tokens)
            
            output.append(f"{i:2d}: {math_indicator} [{cumulative_tokens:3d}t] Score:{score:5.2f}")
            output.append(f"     Content: {sentence.content[:100]}")
            
            if cumulative_tokens >= self.safe_window_start:
                output.append(f"     >>> 🔄 In split consideration zone")
        
        return "\n".join(output)

    def validate_chunks(self, original_text: str, chunks: List[str]) -> Dict[str, Any]:
        """验证chunk结果"""
        validation_result = {
            'valid': True,
            'errors': [],
            'warnings': [],
            'stats': {
                'total_chunks': len(chunks),
                'token_distribution': [],
                'math_formula_integrity': True
            }
        }
        
        # Token数量统计
        for i, chunk in enumerate(chunks):
            tokens = self.get_token_count(chunk)
            validation_result['stats']['token_distribution'].append({
                'chunk_id': i,
                'tokens': tokens,
                'chars': len(chunk)
            })
            
            if tokens > self.target_tokens + 50:
                validation_result['warnings'].append(f'Chunk {i} exceeds target: {tokens} tokens')
        
        # 检查数学公式完整性
        try:
            original_math_spans = self.get_all_math_spans(original_text)
            
            for start, end in original_math_spans:
                formula = original_text[start:end]
                found_complete = False
                
                for chunk in chunks:
                    if formula in chunk:
                        found_complete = True
                        break
                
                if not found_complete:
                    validation_result['valid'] = False
                    validation_result['errors'].append(f'Math formula broken: {formula[:50]}...')
                    validation_result['stats']['math_formula_integrity'] = False
        except Exception as e:
            validation_result['warnings'].append(f'Math validation error: {str(e)}')
        
        return validation_result

    def chunk_text(self, text: str, validate: bool = True) -> Dict[str, Any]:
        """完整的chunking流程"""
        chunks = self.smart_chunk_split(text)
        
        result = {
            'chunks': chunks,
            'chunk_count': len(chunks),
        }
        
        if validate:
            result['validation'] = self.validate_chunks(text, chunks)
        
        return result

    def read_jsonl_file(self, file_path: str) -> List[Dict[str, Any]]:
        """读取JSONL文件"""
        import json
        samples = []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if line:
                        try:
                            sample = json.loads(line)
                            samples.append(sample)
                        except json.JSONDecodeError as e:
                            print(f"警告: 第{line_num}行JSON解析失败: {e}")
            
            print(f"成功读取 {len(samples)} 个样本")
            return samples
            
        except FileNotFoundError:
            print(f"错误: 文件 {file_path} 未找到")
            return []
        except Exception as e:
            print(f"读取文件时发生错误: {str(e)}")
            return []

    def process_all_samples(self, samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """处理所有样本的CoT文本"""
        results = []
        
        for i, sample in enumerate(samples):
            print(f"处理样本 {i+1}/{len(samples)}: {sample.get('id', f'sample_{i+1}')}")
            
            try:
                # 提取CoT文本
                cot_text = sample.get('answer', sample.get('cot', sample.get('reasoning', '')))
                
                if not cot_text:
                    print(f"  警告: 样本 {sample.get('id', f'sample_{i+1}')} 没有找到CoT文本")
                    results.append({
                        'id': sample.get('id', f'sample_{i+1}'),
                        'question': sample.get('question', ''),
                        'original_cot': '',
                        'error': 'No CoT text found',
                        'chunks': [],
                        'chunk_count': 0
                    })
                    continue
                
                # 执行分块处理
                result = self.chunk_text(cot_text, validate=True)
                
                # 构建结果对象
                processed_result = {
                    'id': sample.get('id', f'sample_{i+1}'),
                    'question': sample.get('question', ''),
                    'original_cot': cot_text,
                    'chunks': result['chunks'],
                    'chunk_count': result['chunk_count'],
                    'validation': result.get('validation', {}),
                    'token_distribution': result['validation']['stats']['token_distribution'] if 'validation' in result else []
                }
                
                results.append(processed_result)
                print(f"  ✅ 成功处理，生成 {result['chunk_count']} 个chunks")
                
            except Exception as e:
                print(f"  ❌ 处理失败: {str(e)}")
                results.append({
                    'id': sample.get('id', f'sample_{i+1}'),
                    'question': sample.get('question', ''),
                    'original_cot': sample.get('answer', sample.get('cot', sample.get('reasoning', ''))),
                    'error': str(e),
                    'chunks': [],
                    'chunk_count': 0
                })
        
        return results

    def save_processed_results(self, processed_results: List[Dict[str, Any]], output_path: str):
        """保存处理后的结果到JSONL文件"""
        import json
        
        with open(output_path, 'w', encoding='utf-8') as f:
            for result in processed_results:
                # 创建输出格式
                output_item = {
                    'id': result['id'],
                    'question': result['question'],
                    'original_cot': result['original_cot'],
                    'chunks': result['chunks'],
                    'chunk_count': result['chunk_count'],
                    'processing_status': 'success' if 'error' not in result else 'failed',
                }
                
                if 'error' in result:
                    output_item['error'] = result['error']
                
                if 'validation' in result:
                    output_item['validation_info'] = {
                        'valid': result['validation']['valid'],
                        'token_distribution': result['token_distribution'],
                        'math_formula_integrity': result['validation']['stats']['math_formula_integrity']
                    }
                
                f.write(json.dumps(output_item, ensure_ascii=False) + '\n')

    def generate_token_statistics(self, processed_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成token统计信息"""
        successful_results = [r for r in processed_results if 'error' not in r]
        
        if not successful_results:
            return {"error": "No successful results to analyze"}
        
        # 收集所有chunk的token数
        all_chunk_tokens = []
        for result in successful_results:
            for chunk_info in result.get('token_distribution', []):
                all_chunk_tokens.append(chunk_info['tokens'])
        
        if not all_chunk_tokens:
            return {"error": "No token data available"}
        
        # 计算统计信息
        import statistics
        stats = {
            "total_chunks": len(all_chunk_tokens),
            "mean_tokens": statistics.mean(all_chunk_tokens),
            "median_tokens": statistics.median(all_chunk_tokens),
            "min_tokens": min(all_chunk_tokens),
            "max_tokens": max(all_chunk_tokens),
            "std_tokens": statistics.stdev(all_chunk_tokens) if len(all_chunk_tokens) > 1 else 0,
        }
        
        # token分布区间
        stats["token_ranges"] = {
            "under_400": sum(1 for t in all_chunk_tokens if t < 400),
            "400_to_450": sum(1 for t in all_chunk_tokens if 400 <= t < 450),
            "450_to_512": sum(1 for t in all_chunk_tokens if 450 <= t <= 512),
            "over_512": sum(1 for t in all_chunk_tokens if t > 512),
        }
        
        return stats

    def analyze_chunk_distribution(self, processed_results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分析chunk分布情况"""
        successful_results = [r for r in processed_results if 'error' not in r]
        
        chunk_counts = [r['chunk_count'] for r in successful_results]
        
        if not chunk_counts:
            return {"error": "No successful results to analyze"}
        
        import statistics
        distribution = {
            "samples_with_1_chunk": sum(1 for c in chunk_counts if c == 1),
            "samples_with_2_chunks": sum(1 for c in chunk_counts if c == 2),
            "samples_with_3_chunks": sum(1 for c in chunk_counts if c == 3),
            "samples_with_4_plus_chunks": sum(1 for c in chunk_counts if c >= 4),
            "max_chunks_in_sample": max(chunk_counts),
            "average_chunks_per_sample": statistics.mean(chunk_counts),
        }
        
        return distribution

    def save_statistics(self, stats: Dict[str, Any], stats_path: str):
        """保存统计信息到JSON文件"""
        import json
        
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    def get_current_timestamp(self) -> str:
        """获取当前时间戳"""
        from datetime import datetime
        return datetime.now().isoformat()

    def visualize_chunks(self, text: str, chunks: List[str]) -> str:
        """可视化chunk分割结果"""
        output = []
        for i, chunk in enumerate(chunks):
            tokens = self.get_token_count(chunk)
            
            # 显示chunk的前后边界
            start_text = chunk[:60].replace('\n', '\\n')
            end_text = chunk[-60:].replace('\n', '\\n')
            
            output.append(f"\n{'='*15} CHUNK {i+1} ({tokens} tokens) {'='*15}")
            output.append(f"开始: {start_text}...")
            output.append(f"结束: ...{end_text}")
            output.append("="*60)
        
        return "\n".join(output)
        """可视化chunk分割结果"""
        output = []
        for i, chunk in enumerate(chunks):
            tokens = self.get_token_count(chunk)
            
            # 显示chunk的前后边界
            start_text = chunk[:60].replace('\n', '\\n')
            end_text = chunk[-60:].replace('\n', '\\n')
            
            output.append(f"\n{'='*15} CHUNK {i+1} ({tokens} tokens) {'='*15}")
            output.append(f"开始: {start_text}...")
            output.append(f"结束: ...{end_text}")
            output.append("="*60)
        
        return "\n".join(output)


# 使用示例
def example_usage():
    # 指定JSONL文件路径
    jsonl_file_path = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/test_chunk.jsonl"
    
    try:
        chunker = SmartCoTChunker()
        
        # 读取JSONL文件
        print("=== 开始读取JSONL文件 ===")
        samples = chunker.read_jsonl_file(jsonl_file_path)
        
        if not samples:
            print("没有读取到任何样本，程序退出")
            return
        
        # 处理所有样本
        print("\n=== 开始处理所有样本的CoT文本 ===")
        original_cot_list = chunker.process_all_samples(samples)
        
        # 显示处理结果统计
        print("\n=== 处理结果统计 ===")
        total_samples = len(original_cot_list)
        successful_samples = sum(1 for item in original_cot_list if 'error' not in item)
        failed_samples = total_samples - successful_samples
        total_chunks = sum(item.get('chunk_count', 0) for item in original_cot_list if 'error' not in item)
        
        print(f"总样本数: {total_samples}")
        print(f"成功处理: {successful_samples}")
        print(f"处理失败: {failed_samples}")
        print(f"总chunk数: {total_chunks}")
        
        # 显示每个样本的详细信息
        print("\n=== 样本详细信息 ===")
        for i, item in enumerate(original_cot_list):
            print(f"\n样本 {i+1}: {item['id']}")
            print(f"问题: {item['question'][:100]}...")
            
            if 'error' in item:
                print(f"状态: 处理失败 - {item['error']}")
            else:
                print(f"状态: 处理成功")
                print(f"Chunk数量: {item['chunk_count']}")
                print(f"Token分布: {[chunk['tokens'] for chunk in item['token_distribution']]}")
        
        # 显示前几个chunk的预览
        print("\n=== Chunk预览 ===")
        for i, item in enumerate(original_cot_list[:3]):  # 只显示前3个样本
            if 'error' not in item and item['chunks']:
                print(f"\n--- 样本 {item['id']} 的Chunks ---")
                for j, chunk in enumerate(item['chunks']):
                    tokens = chunker.get_token_count(chunk)
                    preview = chunk[:100].replace('\n', '\\n')
                    print(f"  Chunk {j+1} ({tokens} tokens): {preview}...")
        
        # 保存处理结果
        print("\n=== 开始保存结果 ===")
        output_file_path = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/chunked_cot_output.jsonl"
        stats_file_path = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/chunking_stats.json"
        
        try:
            # 保存处理后的数据
            chunker.save_processed_results(original_cot_list, output_file_path)
            
            # 生成并保存统计信息
            stats = {
                "processing_summary": {
                    "total_samples": total_samples,
                    "successful_samples": successful_samples,
                    "failed_samples": failed_samples,
                    "total_chunks": total_chunks,
                    "average_chunks_per_sample": total_chunks / successful_samples if successful_samples > 0 else 0
                },
                "token_statistics": chunker.generate_token_statistics(original_cot_list),
                "chunk_distribution": chunker.analyze_chunk_distribution(original_cot_list),
                "processing_timestamp": chunker.get_current_timestamp()
            }
            
            chunker.save_statistics(stats, stats_file_path)
            
            print(f"✅ 处理结果已保存到: {output_file_path}")
            print(f"✅ 统计信息已保存到: {stats_file_path}")
            
        except Exception as save_error:
            print(f"❌ 保存过程中发生错误: {str(save_error)}")
            import traceback
            traceback.print_exc()
        
        print(f"\n=== 处理完成 ===")
        print(f"所有结果已保存在 original_cot_list 变量中")
        print(f"original_cot_list 包含 {len(original_cot_list)} 个样本的处理结果")
        
    except Exception as e:
        print(f"程序执行时发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    example_usage()