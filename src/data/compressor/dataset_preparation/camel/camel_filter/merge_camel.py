import json
import re

def merge_and_sort_jsonl(file1_path, file2_path, output_path):
    """
    合并两个JSONL文件并按照id属性升序排列
    
    Args:
        file1_path: 第一个JSONL文件路径
        file2_path: 第二个JSONL文件路径
        output_path: 输出文件路径
    """
    
    all_data = []
    
    # 读取第一个文件
    print(f"正在读取文件: {file1_path}")
    try:
        with open(file1_path, 'r', encoding='utf-8') as f:
            count1 = 0
            for line in f:
                if line.strip():  # 跳过空行
                    data = json.loads(line)
                    all_data.append(data)
                    count1 += 1
        print(f"文件1读取完成，共 {count1} 条记录")
    except FileNotFoundError:
        print(f"错误: 文件 {file1_path} 不存在")
        return
    except Exception as e:
        print(f"读取文件1时出错: {e}")
        return
    
    # 读取第二个文件
    print(f"正在读取文件: {file2_path}")
    try:
        with open(file2_path, 'r', encoding='utf-8') as f:
            count2 = 0
            for line in f:
                if line.strip():  # 跳过空行
                    data = json.loads(line)
                    all_data.append(data)
                    count2 += 1
        print(f"文件2读取完成，共 {count2} 条记录")
    except FileNotFoundError:
        print(f"错误: 文件 {file2_path} 不存在")
        return
    except Exception as e:
        print(f"读取文件2时出错: {e}")
        return
    
    print(f"合并后总记录数: {len(all_data)}")
    
    # 定义排序键函数
    def get_sort_key(item):
        """提取id中的数字部分用于排序"""
        id_str = item.get('id', '')
        # 使用正则表达式提取数字部分
        match = re.search(r'item_(\d+)', id_str)
        if match:
            return int(match.group(1))
        else:
            # 如果格式不匹配，使用字符串排序
            return float('inf')  # 将无法解析的id排到最后
    
    # 按id排序
    print("正在按ID排序...")
    all_data.sort(key=get_sort_key)
    
    # 保存到输出文件
    print(f"正在保存到文件: {output_path}")
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            for item in all_data:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
        print(f"文件保存成功!")
    except Exception as e:
        print(f"保存文件时出错: {e}")
        return
    
    # 显示排序结果的前几个和后几个id
    print("\n排序结果预览:")
    print("前5个ID:")
    for i, item in enumerate(all_data[:5]):
        print(f"  {i+1}. {item.get('id', 'N/A')}")
    
    if len(all_data) > 5:
        print("后5个ID:")
        for i, item in enumerate(all_data[-5:], len(all_data)-4):
            print(f"  {i}. {item.get('id', 'N/A')}")
    
    return all_data

def check_for_duplicates(data):
    """检查是否有重复的ID"""
    ids = [item.get('id', '') for item in data]
    unique_ids = set(ids)
    
    if len(ids) != len(unique_ids):
        duplicate_count = len(ids) - len(unique_ids)
        print(f"\n警告: 发现 {duplicate_count} 个重复的ID")
        
        # 找出重复的ID
        id_counts = {}
        for id_str in ids:
            id_counts[id_str] = id_counts.get(id_str, 0) + 1
        
        duplicates = [id_str for id_str, count in id_counts.items() if count > 1]
        print("重复的ID:")
        for dup_id in duplicates[:10]:  # 只显示前10个
            print(f"  {dup_id} (出现 {id_counts[dup_id]} 次)")
        if len(duplicates) > 10:
            print(f"  ... 还有 {len(duplicates) - 10} 个重复ID")
    else:
        print("\n✓ 没有发现重复的ID")

# 使用示例
if __name__ == "__main__":
    # 修改为您的文件路径
    file1 = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_3k_correct_results.jsonl"
    file2 = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/camel_26k_0714_correct.jsonl"
    output = "/Users/mwie/User/Data/Code/CoT Language/CoT_Language/dataset_preparation/camel/Final_CAMEL_DATA.jsonl"
    
    # 执行合并和排序
    merged_data = merge_and_sort_jsonl(file1, file2, output)
    
    # 检查重复ID
    if merged_data:
        check_for_duplicates(merged_data)