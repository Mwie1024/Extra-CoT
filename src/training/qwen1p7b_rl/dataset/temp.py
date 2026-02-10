import json

def find_jsonl_id_intersection(file1, file2, output_file=None):
    # 读取第一个文件中的所有 id
    ids1 = set()
    with open(file1, 'r', encoding='utf-8') as f1:
        for line in f1:
            if line.strip():
                obj = json.loads(line)
                if 'id' in obj:
                    ids1.add(obj['id'])
    
    # 查找第二个文件中与第一个文件相同的 id
    intersection = []
    with open(file2, 'r', encoding='utf-8') as f2:
        for line in f2:
            if line.strip():
                obj = json.loads(line)
                if 'id' in obj and obj['id'] in ids1:
                    intersection.append(obj)

    # 如果指定输出文件，就保存交集内容
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as out:
            for item in intersection:
                out.write(json.dumps(item, ensure_ascii=False) + '\n')

    print(f"共有 {len(intersection)} 条记录的 id 在两个文件中都有。")
    return intersection


# 示例用法：
if __name__ == "__main__":
    file1 = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/RL/dataset/metamath_60k.jsonl"
    file2 = "/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/datasets/metamath_1k.jsonl"
    output = "intersection.jsonl"
    intersection = find_jsonl_id_intersection(file1, file2, output)
