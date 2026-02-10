import json
import re
from typing import List, Dict, Any, Tuple
from dataclasses import dataclass

@dataclass
class LaTeXIssue:
    issue_type: str
    description: str
    location: int = -1
    content_preview: str = ""

class LaTeXIntegrityChecker:
    def __init__(self):
        # 用户提供的LaTeX模式
        self.LATEX_PATS = [
            re.compile(r'\$\$(?:(?:\\.|[^$\\])*)\$\$', re.S),
            re.compile(r'\\\[(?:(?:\\.|[^\\])*)\\\]', re.S),
            re.compile(r'\\\((?:(?:\\.|[^\\])*)\\\)', re.S),
            re.compile(r'\$(?:(?:\\.|[^$\\])*)\$', re.S),
        ]
        
        # 用于检查配对的简化模式
        self.PAIR_PATTERNS = [
            (r'\$\$', r'\$\$', '$$...$$'),
            (r'\\\[', r'\\\]', r'\[...\]'),
            (r'\\\(', r'\\\)', r'\(...\)'),
        ]

    def check_latex_integrity(self, text: str) -> List[LaTeXIssue]:
        """检查文本中LaTeX公式的完整性"""
        issues = []
        
        # 1. 检查各种LaTeX格式的配对
        issues.extend(self._check_latex_pairs(text))
        
        # 2. 检查单$符号配对
        issues.extend(self._check_single_dollar_pairs(text))
        
        # 3. 检查异常长的公式
        issues.extend(self._check_abnormally_long_formulas(text))
        
        # 4. 检查嵌套问题
        issues.extend(self._check_nested_formulas(text))
        
        # 5. 检查孤立符号
        issues.extend(self._check_orphaned_symbols(text))
        
        return issues

    def _check_latex_pairs(self, text: str) -> List[LaTeXIssue]:
        """检查LaTeX配对符号"""
        issues = []
        
        for start_pattern, end_pattern, formula_name in self.PAIR_PATTERNS:
            start_matches = list(re.finditer(start_pattern, text))
            end_matches = list(re.finditer(end_pattern, text))
            
            if len(start_matches) != len(end_matches):
                issues.append(LaTeXIssue(
                    issue_type="unmatched_pairs",
                    description=f"{formula_name} 配对不匹配: {len(start_matches)} 个开始符号, {len(end_matches)} 个结束符号"
                ))
        
        return issues

    def _check_single_dollar_pairs(self, text: str) -> List[LaTeXIssue]:
        """检查单$符号配对"""
        issues = []
        
        # 先排除$$的影响
        text_without_double = re.sub(r'\$\$.*?\$\$', '', text, flags=re.DOTALL)
        
        # 计算剩余的单$符号
        single_dollars = re.findall(r'(?<!\$)\$(?!\$)', text_without_double)
        
        if len(single_dollars) % 2 != 0:
            issues.append(LaTeXIssue(
                issue_type="unmatched_single_dollar",
                description=f"单$符号不配对: 找到 {len(single_dollars)} 个单$符号"
            ))
            
            # 找到所有单$的位置
            positions = []
            for match in re.finditer(r'(?<!\$)\$(?!\$)', text):
                # 检查这个位置是否在$$块内
                in_double_dollar = False
                for double_match in re.finditer(r'\$\$.*?\$\$', text, flags=re.DOTALL):
                    if double_match.start() <= match.start() <= double_match.end():
                        in_double_dollar = True
                        break
                
                if not in_double_dollar:
                    positions.append(match.start())
            
            # 报告未配对的$位置
            if positions:
                for i, pos in enumerate(positions):
                    context = text[max(0, pos-20):pos+21]
                    issues.append(LaTeXIssue(
                        issue_type="single_dollar_position",
                        description=f"第 {i+1} 个单$符号位置",
                        location=pos,
                        content_preview=context
                    ))
        
        return issues

    def _check_abnormally_long_formulas(self, text: str) -> List[LaTeXIssue]:
        """检查异常长的公式"""
        issues = []
        
        for i, pattern in enumerate(self.LATEX_PATS):
            matches = pattern.finditer(text)
            for match in matches:
                formula_length = match.end() - match.start()
                
                # 设置不同类型公式的长度阈值
                if i == 0:  # $$...$$
                    threshold = 1000
                elif i in [1, 2]:  # \[...\], \(...\)
                    threshold = 500
                else:  # $...$
                    threshold = 300
                
                if formula_length > threshold:
                    issues.append(LaTeXIssue(
                        issue_type="abnormally_long_formula",
                        description=f"异常长的公式 ({formula_length} 字符)",
                        location=match.start(),
                        content_preview=match.group()[:100] + "..." if len(match.group()) > 100 else match.group()
                    ))
        
        return issues

    def _check_nested_formulas(self, text: str) -> List[LaTeXIssue]:
        """检查嵌套公式问题"""
        issues = []
        
        # 检查$嵌套在$$内
        for match in re.finditer(r'\$\$.*?\$\$', text, flags=re.DOTALL):
            formula_content = match.group()
            inner_dollars = re.findall(r'(?<!\$)\$(?!\$)', formula_content)
            if inner_dollars:
                issues.append(LaTeXIssue(
                    issue_type="nested_formulas",
                    description=f"$$块内发现 {len(inner_dollars)} 个单$符号",
                    location=match.start(),
                    content_preview=formula_content[:100] + "..." if len(formula_content) > 100 else formula_content
                ))
        
        return issues

    def _check_orphaned_symbols(self, text: str) -> List[LaTeXIssue]:
        """检查孤立的LaTeX符号"""
        issues = []
        
        # 检查孤立的\]、\)等
        orphaned_patterns = [
            (r'(?<!\\)\\\]', '孤立的\\]'),
            (r'(?<!\\)\\\)', '孤立的\\)'),
            (r'^\\$', '行首的单独\\')
        ]
        
        for pattern, description in orphaned_patterns:
            matches = list(re.finditer(pattern, text, re.MULTILINE))
            if matches:
                issues.append(LaTeXIssue(
                    issue_type="orphaned_symbols",
                    description=f"{description}: 找到 {len(matches)} 处"
                ))
        
        return issues

    def check_jsonl_file(self, file_path: str) -> Dict[str, Any]:
        """检查JSONL文件中所有样本的LaTeX完整性"""
        results = {
            'total_samples': 0,
            'problematic_samples': 0,
            'successful_samples': 0,
            'issue_summary': {},
            'detailed_issues': [],
            'successful_samples_data': []
        }
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        sample = json.loads(line)
                        results['total_samples'] += 1
                        
                        # 提取CoT文本
                        cot_text = sample.get('cot', '')
                        if not cot_text:
                            continue
                        
                        # 检查LaTeX完整性
                        issues = self.check_latex_integrity(cot_text)
                        
                        if issues:
                            results['problematic_samples'] += 1
                            
                            # 统计问题类型
                            for issue in issues:
                                issue_type = issue.issue_type
                                if issue_type not in results['issue_summary']:
                                    results['issue_summary'][issue_type] = 0
                                results['issue_summary'][issue_type] += 1
                            
                            # 记录详细信息
                            results['detailed_issues'].append({
                                'sample_id': sample.get('id', f'line_{line_num}'),
                                'line_number': line_num,
                                'issues': [
                                    {
                                        'type': issue.issue_type,
                                        'description': issue.description,
                                        'location': issue.location,
                                        'preview': issue.content_preview
                                    }
                                    for issue in issues
                                ]
                            })
                        else:
                            # LaTeX公式匹配成功，保存样本
                            results['successful_samples'] += 1
                            results['successful_samples_data'].append({
                                'sample_id': sample.get('id', f'line_{line_num}'),
                                'line_number': line_num,
                                'sample_data': sample
                            })
                        
                    except json.JSONDecodeError as e:
                        print(f"警告: 第{line_num}行JSON解析失败: {e}")
                        continue
            
        except FileNotFoundError:
            print(f"错误: 文件 {file_path} 未找到")
            return results
        except Exception as e:
            print(f"读取文件时发生错误: {str(e)}")
            return results
        
        return results

    def generate_report(self, results: Dict[str, Any]) -> str:
        """生成检查报告"""
        report_lines = []
        
        # 总体统计
        report_lines.append("=" * 60)
        report_lines.append("LaTeX 公式完整性检查报告")
        report_lines.append("=" * 60)
        
        total = results['total_samples']
        problematic = results['problematic_samples']
        successful = results['successful_samples']
        
        report_lines.append(f"总样本数: {total}")
        report_lines.append(f"有问题的样本: {problematic}")
        report_lines.append(f"成功的样本: {successful}")
        report_lines.append(f"问题比例: {problematic/total*100:.1f}%")
        report_lines.append(f"成功比例: {successful/total*100:.1f}%")
        
        # 问题类型统计
        if results['issue_summary']:
            report_lines.append("\n问题类型统计:")
            report_lines.append("-" * 40)
            for issue_type, count in results['issue_summary'].items():
                report_lines.append(f"  {issue_type}: {count} 次")
        
        # 详细问题列表
        if results['detailed_issues']:
            report_lines.append(f"\n详细问题列表 (显示前10个):")
            report_lines.append("-" * 40)
            
            for i, issue_detail in enumerate(results['detailed_issues'][:10]):
                report_lines.append(f"\n样本 {i+1}: {issue_detail['sample_id']} (行号: {issue_detail['line_number']})")
                
                for issue in issue_detail['issues']:
                    report_lines.append(f"  - {issue['type']}: {issue['description']}")
                    if issue['location'] >= 0:
                        report_lines.append(f"    位置: {issue['location']}")
                    if issue['preview']:
                        preview = issue['preview'].replace('\n', '\\n')
                        report_lines.append(f"    预览: {preview}")
        
        return "\n".join(report_lines)

    def save_report(self, results: Dict[str, Any], output_path: str):
        """保存详细的检查结果到JSON文件"""
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    def save_successful_samples(self, results: Dict[str, Any], output_path: str):
        """保存所有LaTeX公式匹配成功的样本到JSONL文件"""
        successful_count = 0
        with open(output_path, 'w', encoding='utf-8') as f:
            for sample_info in results['successful_samples_data']:
                sample_data = sample_info['sample_data']
                json.dump(sample_data, f, ensure_ascii=False)
                f.write('\n')
                successful_count += 1
        
        print(f"成功保存 {successful_count} 个LaTeX公式匹配成功的样本到: {output_path}")

def main():
    # 配置文件路径
    input_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_filter/LONGFORMER_DATA.jsonl"
    output_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/latex_check_results.json"
    successful_samples_file = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_chunk/successful_latex_samples.jsonl"
    
    # 创建检查器
    checker = LaTeXIntegrityChecker()
    
    print("开始检查LaTeX公式完整性...")
    
    # 执行检查
    results = checker.check_jsonl_file(input_file)
    
    # 生成和显示报告
    report = checker.generate_report(results)
    print(report)
    
    # 保存详细结果
    checker.save_report(results, output_file)
    print(f"\n详细结果已保存到: {output_file}")
    
    # 保存成功样本
    if results['successful_samples'] > 0:
        checker.save_successful_samples(results, successful_samples_file)
    else:
        print("\n没有找到LaTeX公式匹配成功的样本")
    
    # 如果有问题样本，询问是否查看具体内容
    if results['problematic_samples'] > 0:
        print(f"\n发现 {results['problematic_samples']} 个有问题的样本")
        print("建议检查详细结果文件来确定修复策略")

if __name__ == "__main__":
    main()