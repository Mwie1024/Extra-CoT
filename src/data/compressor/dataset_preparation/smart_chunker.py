import re
import nltk
from typing import List, Tuple

class SmartChunker:
    def __init__(self, max_tokens=512, min_chunk_size=128, overlap_size=50):
        self.max_tokens = max_tokens
        self.min_chunk_size = min_chunk_size
        self.overlap_size = overlap_size
        
    def extract_math_formulas(self, text: str) -> Tuple[str, dict]:
        """提取并替换数学公式"""
        math_patterns = [
            r'\$\$.*?\$\$',  # 块级公式
            r'\$.*?\$',      # 行内公式
            r'\\begin\{.*?\}.*?\\end\{.*?\}',  # LaTeX环境
            r'\\[a-zA-Z]+\{[^}]*\}',  # LaTeX命令
        ]
        
        formulas = {}
        processed_text = text
        formula_id = 0
        
        for pattern in math_patterns:
            matches = re.findall(pattern, processed_text, re.DOTALL)
            for match in matches:
                placeholder = f"[MATH_{formula_id}]"
                formulas[placeholder] = match
                processed_text = processed_text.replace(match, placeholder, 1)
                formula_id += 1
                
        return processed_text, formulas
    
    def identify_reasoning_steps(self, text: str) -> List[Tuple[int, int]]:
        """识别推理步骤边界"""
        step_patterns = [
            r'Step \d+:',
            r'\d+\.',
            r'Therefore,',
            r'So,',
            r'Thus,',
            r'Hence,',
            r'Now,',
            r'Next,',
            r'Finally,',
        ]
        
        boundaries = [0]
        for pattern in step_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                boundaries.append(match.start())
        
        boundaries.append(len(text))
        boundaries = sorted(list(set(boundaries)))
        
        # 转换为(start, end)对
        step_ranges = []
        for i in range(len(boundaries) - 1):
            step_ranges.append((boundaries[i], boundaries[i + 1]))
            
        return step_ranges
    
    def smart_chunk(self, question: str, cot: str) -> List[dict]:
        """智能分块CoT"""
        # 1. 提取数学公式
        processed_cot, formulas = self.extract_math_formulas(cot)
        
        # 2. 识别推理步骤
        reasoning_steps = self.identify_reasoning_steps(processed_cot)
        
        # 3. 基于推理步骤进行分块
        chunks = []
        current_chunk_start = 0
        current_token_count = len(question.split())  # 每个chunk都包含问题
        
        for step_start, step_end in reasoning_steps:
            step_text = processed_cot[step_start:step_end]
            step_tokens = len(step_text.split())
            
            # 如果加入这个步骤会超过最大长度，先保存当前chunk
            if current_token_count + step_tokens > self.max_tokens:
                if current_chunk_start < step_start:  # 确保有内容
                    chunk_text = processed_cot[current_chunk_start:step_start].strip()
                    if chunk_text:
                        chunks.append({
                            'question': question,
                            'cot_chunk': chunk_text,
                            'chunk_id': len(chunks),
                            'formulas': formulas
                        })
                
                # 开始新的chunk
                current_chunk_start = max(0, step_start - self.overlap_size)
                current_token_count = len(question.split())
            
            current_token_count += step_tokens
        
        # 处理最后一个chunk
        if current_chunk_start < len(processed_cot):
            chunk_text = processed_cot[current_chunk_start:].strip()
            if len(chunk_text.split()) >= self.min_chunk_size or len(chunks) == 0:
                chunks.append({
                    'question': question,
                    'cot_chunk': chunk_text,
                    'chunk_id': len(chunks),
                    'formulas': formulas
                })
            else:
                # 合并到前一个chunk
                if chunks:
                    chunks[-1]['cot_chunk'] += ' ' + chunk_text
        
        return chunks

# 使用示例
def chunk_cot_data(question: str, cot: str) -> List[dict]:
    """对单个问题-CoT对进行分块"""
    chunker = SmartChunker(max_tokens=512, min_chunk_size=128)
    return chunker.smart_chunk(question, cot)

# 批量处理函数
def process_dataset(data_list: List[dict]) -> List[dict]:
    """批量处理数据集"""
    chunked_data = []
    
    for item in data_list:
        question = item['question']
        cot = item['cot']
        
        chunks = chunk_cot_data(question, cot)
        chunked_data.extend(chunks)
    
    return chunked_data