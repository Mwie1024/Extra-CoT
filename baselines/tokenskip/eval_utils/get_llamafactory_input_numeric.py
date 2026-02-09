import os
import json
import random
import numpy as np

def load_json(file, encoding='utf-8'):
    data = []
    with open(file, 'r', encoding=encoding) as f:
        for j in f.readlines():
            j = json.loads(j)
            data.append(j)
    return data

def write_list_to_json(list, file_path):
    with open(file_path, 'w') as  f:
        json.dump(list, f, ensure_ascii=False, indent=1)

def seed_everything(seed: int):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)

def load_all_data(input_dir="outputs/Qwen2.5-7B-Instruct/gsm8k/7b/"):
    original_data = load_json(os.path.join(input_dir, "Original/train/samples/predictions_formatted.jsonl"))
    compressed_data_0 = load_json(os.path.join(input_dir, "Compression/train_outputs_compressed_ratio_0.9.jsonl"))
    compressed_data_1 = load_json(os.path.join(input_dir, "Compression/train_outputs_compressed_ratio_0.8.jsonl"))
    compressed_data_2 = load_json(os.path.join(input_dir, "Compression/train_outputs_compressed_ratio_0.7.jsonl"))
    compressed_data_3 = load_json(os.path.join(input_dir, "Compression/train_outputs_compressed_ratio_0.6.jsonl"))
    compressed_data_4 = load_json(os.path.join(input_dir, "Compression/train_outputs_compressed_ratio_0.5.jsonl"))
    return [original_data, compressed_data_0, compressed_data_1, compressed_data_2, compressed_data_3, compressed_data_4]

def get_llamafactory_input():
    compressed_data_list = load_all_data()
    original_data = compressed_data_list[0]
    compression_ratio_list = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]
    datalines = []
    for i in range(len(original_data)):
        data_index = random.choice([0,1,2,3,4,5])
        if data_index == 0:
            input_data = original_data[i]['messages'][0]['content']
            answer = original_data[i]['prediction']
            cot = original_data[i]['model_output']
            output_data = f"{cot}\n\nThe final answer is: " + "$\\boxed{" + answer + "}$"
        else:
            compression_ratio = compression_ratio_list[data_index]
            compressed_data = compressed_data_list[data_index]
            input_data = f"{compressed_data[i]['question']}<|eot_id|>{compression_ratio}<|eot_id|>"
            answer = compressed_data[i]['model_answer']
            cot = compressed_data[i]['compressed_cot']
            output_data = f"{cot}\n\nThe final answer is: " + "$\\boxed{" + answer + "}$"

        data = {
            "instruction": "Please reason step by step, and put your final answer within \\boxed{}.",
            "input": input_data,
            "output": output_data
        }
        datalines.append(data)
    print(len(datalines))
    random.shuffle(datalines)
    write_list_to_json(datalines, './outputs/mydataset_compressed_gsm8k_llmlingua2_qwen_7B.json')


if __name__ == '__main__':
    seed_everything(42)
    get_llamafactory_input()

