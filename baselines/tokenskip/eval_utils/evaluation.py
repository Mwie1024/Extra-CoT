# -*- coding: utf-8 -*-
import os
import json
import torch
import random
import argparse
import numpy as np
from tqdm import tqdm
from time import time
from copy import deepcopy

from peft import PeftModel, PeftConfig, get_peft_model
from transformers import AutoTokenizer, AutoModelForCausalLM

# 你工程里的工具保持不变
from eval.utils import generate_completions
from data_processing.process_utils import *
from data_processing.answer_extraction import *
from eval.eval_script import *

try:
    from safetensors.torch import load_file as safe_load_file
except Exception:
    safe_load_file = None


# --------------------------
# Utilities
# --------------------------
def set_random_seed(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def read_data(path):
    if path.endswith("json"):
        data = json.load(open(path, "r"))
    elif path.endswith("jsonl"):
        data = []
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                data.append(json.loads(line))
    else:
        raise NotImplementedError(f"Unsupported file type: {path}")
    return data


def _maybe_add_missing_special_tokens(tokenizer, needed_specials):
    """
    只在确实缺失时补齐，避免改变训练时已存在 token 的 id 映射。
    """
    missing = []
    for tok in needed_specials:
        tid = tokenizer.convert_tokens_to_ids(tok)
        if tid is None or (getattr(tokenizer, "unk_token_id", None) is not None and tid == tokenizer.unk_token_id):
            missing.append(tok)
    if missing:
        tokenizer.add_special_tokens({"additional_special_tokens": missing})
    return missing


def _print_vocab_info(prefix, tokenizer, model=None):
    v = len(tokenizer)
    msg = f"[{prefix}] tokenizer_vocab_size={v}"
    if model is not None:
        msg += f", model_embed_num={model.get_input_embeddings().num_embeddings}"
        if hasattr(model, "config"):
            msg += f", config.vocab_size={getattr(model.config, 'vocab_size', 'NA')}"
    print(msg, flush=True)


def _load_adapter_filtered(model, adapter_path):
    """
    只加载 LoRA 层参数，过滤掉 adapter 中 modules_to_save 里
    的 embed_tokens / lm_head 全量权重。用于临时绕过 tokenizer 不一致问题。
    """
    if safe_load_file is None:
        raise RuntimeError("safetensors 未安装，无法使用 --drop_modules_to_save，"
                           "请先 pip install safetensors，或去掉该开关。")

    peft_cfg = PeftConfig.from_pretrained(adapter_path)
    peft_model = get_peft_model(model, peft_cfg)

    # 推测权重文件名；优先 safetensors
    st_path = os.path.join(adapter_path, "adapter_model.safetensors")
    pt_path = os.path.join(adapter_path, "adapter_model.bin")

    if os.path.exists(st_path):
        state_dict = safe_load_file(st_path)
    elif os.path.exists(pt_path):
        state_dict = torch.load(pt_path, map_location="cpu")
    else:
        raise FileNotFoundError(f"未在 {adapter_path} 找到 adapter_model.safetensors/bin")

    keys_to_drop = [k for k in state_dict.keys()
                    if "modules_to_save" in k and ("embed_tokens" in k or "lm_head" in k)]
    for k in keys_to_drop:
        state_dict.pop(k)

    missing, unexpected = peft_model.load_state_dict(state_dict, strict=False)
    print(f"[adapter] dropped {len(keys_to_drop)} keys (modules_to_save for embed/lm_head)", flush=True)
    if missing:
        print(f"[adapter] missing keys: {len(missing)} (showing first 10)\n{missing[:10]}", flush=True)
    if unexpected:
        print(f"[adapter] unexpected keys: {len(unexpected)} (showing first 10)\n{unexpected[:10]}", flush=True)

    return peft_model


# --------------------------
# Inference Core
# --------------------------
def infer(args, test_data, answer_extraction_fn):
    # ---------- Prompt 构造（与原逻辑一致） ----------
    # 先临时 tokenizer（仅用于拼 prompt 中的 bos/eos），随后会重新加载“训练时 tokenizer”
    tmp_tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path, trust_remote_code=True)

    prompts = []
    for example in test_data:
        prompt = ""
        for mess in example['messages']:
            if mess['role'] == 'user':
                if args.model_type == 'llama3':
                    if args.compression_ratio < 1.0:
                        prompt += (
                            f"{tmp_tokenizer.bos_token}"
                            "<|start_header_id|>user<|end_header_id|>\n\n"
                            "Please reason step by step, and put your final answer within \\boxed{}.\n"
                            f"{mess['content']}\n{tmp_tokenizer.eos_token}"
                            f"{args.compression_ratio}"
                            f"{tmp_tokenizer.eos_token}{tmp_tokenizer.eos_token}"
                            "<|start_header_id|>assistant<|end_header_id|>\n\n"
                        )
                    else:
                        prompt += (
                            f"{tmp_tokenizer.bos_token}"
                            "<|start_header_id|>user<|end_header_id|>\n\n"
                            "Please reason step by step, and put your final answer within \\boxed{}.\n"
                            f"{mess['content']}\n{tmp_tokenizer.eos_token}"
                            "<|start_header_id|>assistant<|end_header_id|>\n\n"
                        )
                elif args.model_type == 'qwen':
                    if args.compression_ratio < 1.0:
                        prompt += (
                            "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
                            "<|im_start|>user\nPlease reason step by step, and put your final answer within \\boxed{}.\n"
                            f"{mess['content']}<COMP_{int(args.compression_ratio * 100)}><|im_end|>\n"
                            "<|im_start|>assistant\n"
                        )
                    else:
                        prompt += (
                            "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
                            "<|im_start|>user\nPlease reason step by step, and put your final answer within \\boxed{}.\n"
                            f"{mess['content']}<|im_end|>\n"
                            "<|im_start|>assistant\n"
                        )
                else:
                    raise NotImplementedError(f"Unknown model_type: {args.model_type}")
            elif mess['role'] == 'assistant':
                prompt += mess['content'].rstrip()
            prompt = prompt.lstrip()
        example['prompt'] = prompt
        prompts.append(prompt)

    print("Loading model and tokenizer...", flush=True)

    # ---------- 使用“训练时的 tokenizer” ----------
    # 强烈建议：args.tokenizer_path 指向训练时保存的 tokenizer 目录
    tok_dir = args.tokenizer_path
    tokenizer = AutoTokenizer.from_pretrained(tok_dir, use_fast=True, trust_remote_code=True)

    # 仅在缺失时补齐（避免改变 token-id 映射）
    qwen_needed = [f"<COMP_{p}>" for p in (50, 60, 70, 80, 90)]
    missing = _maybe_add_missing_special_tokens(tokenizer, qwen_needed)
    if missing:
        print(f"[tokenizer] added {len(missing)} missing special tokens: {missing}", flush=True)
    _print_vocab_info("after_tokenizer_load", tokenizer)

    # ---------- 加载基座模型 ----------
    model = AutoModelForCausalLM.from_pretrained(
        args.model_path,
        torch_dtype=torch.float16,
        trust_remote_code=True,
        device_map="auto",
    )

    # ---------- 词表尺寸对齐 ----------
    if model.get_input_embeddings().num_embeddings != len(tokenizer):
        print(f"[resize] resizing embeddings from "
              f"{model.get_input_embeddings().num_embeddings} -> {len(tokenizer)}", flush=True)
        model.resize_token_embeddings(len(tokenizer))
        # 同步 config（有些模型不会自动更新）
        if hasattr(model, "config"):
            model.config.vocab_size = len(tokenizer)

    _print_vocab_info("before_adapter_load", tokenizer, model)

    # ---------- 挂载 LoRA 适配器 ----------
    if args.use_adapter:
        if args.drop_modules_to_save:
            print("[adapter] loading with filtered modules_to_save (embed/lm_head dropped)", flush=True)
            model = _load_adapter_filtered(model, args.adapter_path)
        else:
            print("[adapter] loading with PeftModel.from_pretrained (expect vocab EXACT match)", flush=True)
            model = PeftModel.from_pretrained(model, args.adapter_path)  # 不 merge

    # ---------- 生成前设置 ----------
    tokenizer.padding_side = "left"
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        tokenizer.pad_token_id = tokenizer.eos_token_id

    stop_id_sequences = []
    if tokenizer.eos_token_id is not None:
        stop_id_sequences = [[tokenizer.eos_token_id]]

    torch.cuda.synchronize()
    start_time = time()
    outputs, finish_completion = generate_completions(
        model=model,
        tokenizer=tokenizer,
        prompts=prompts,
        max_new_tokens=args.max_new_tokens,
        do_sample=False,
        temperature=args.temperature,
        top_p=1.0,
        batch_size=args.eval_batch_size,
        stop_id_sequences=stop_id_sequences if stop_id_sequences else None,
        end_of_generation_id_sequence=[tokenizer.eos_token_id] if tokenizer.eos_token_id is not None else None
    )
    torch.cuda.synchronize()
    total_time = time() - start_time

    # ---------- 统计 COT 长度 ----------
    model_outputs = outputs
    cot_lengths = []
    for model_completion in model_outputs:
        cot = model_completion.split('\n\nThe final answer is:')[0]
        cot_length = tokenizer(cot, return_tensors="pt")['input_ids'].shape[1]
        cot_lengths.append(cot_length)

    # ---------- 抽取答案 ----------
    predictions = [eval(answer_extraction_fn)(
        item['messages'][-2]['content'], output, task='cot'
    ) for item, output in tqdm(zip(test_data, model_outputs), desc="extract answer", total=len(model_outputs))]

    assert len(model_outputs) > 0, f"{len(model_outputs)}"

    # ---------- 汇总结果 ----------
    results = []
    for example, output, pred, cot_length in zip(test_data, model_outputs, predictions, cot_lengths):
        item = deepcopy(example)
        item.update({
            'model_output': output,
            'prediction': pred,
            'cot_length': cot_length,
        })
        results.append(item)
    return results, total_time


# --------------------------
# Main
# --------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=str, default="outputs/Qwen2.5-7B-Instruct/gsm8k/",
                        help="default to `model_path`_predictions")
    parser.add_argument("--model-path", type=str, default="/your_model_path/Qwen2.5-7B-Instruct")
    parser.add_argument("--tokenizer-path", type=str, default="/your_model_path/Qwen2.5-7B-Instruct",  # 指向训练时保存的 tokenizer
                        help="MUST be the tokenizer used during training (with added specials).")
    parser.add_argument("--adapter-path", type=str, default="/your_model_path/TokenSkip-Qwen2.5-7B-Instruct-GSM8K")
    parser.add_argument("--model-size", type=str, choices=['3b', '7b', '13b', '33b', '34b', '70b'], default="7b")
    parser.add_argument("--model-type", type=str, choices=['llama3', 'qwen'], default="qwen")

    # LoRA/Adapter
    parser.add_argument("--use_adapter", action="store_true", help="whether to use LoRA")
    parser.add_argument("--drop_modules_to_save", action="store_true",
                        help="TEMP workaround: drop modules_to_save weights (embed/lm_head) when loading adapter")

    # 数据与评测
    parser.add_argument("--compression_ratio", type=float, default=1.0, help="compression ratio for CoT.")
    parser.add_argument("--benchmark", type=str, choices=['gsm8k', 'math'], default="gsm8k")
    parser.add_argument("--data-type", type=str, choices=['train', 'test'], default="test")
    parser.add_argument("--max_num_examples", type=int, default=10**15)
    parser.add_argument("--max_new_tokens", type=int, default=512)
    parser.add_argument("--eval_batch_size", type=int, default=16)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=42)

    args, unparsed_args = parser.parse_known_args()

    if args.benchmark == 'math' and args.use_adapter:
        args.max_new_tokens = int(args.max_new_tokens * args.compression_ratio)

    print(f"Evaluating {args.model_path}", flush=True)
    print(f"Max new tokens: {args.max_new_tokens}, eval batch size: {args.eval_batch_size}, "
          f"temperature: {args.temperature}, seed: {args.seed}", flush=True)
    if args.use_adapter:
        print(f"Adapter path {args.adapter_path}, compression ratio: {args.compression_ratio}", flush=True)
        if args.drop_modules_to_save:
            print("[WARN] Using --drop_modules_to_save; prefer fixing tokenizer to match training vocab.", flush=True)

    # 输出目录组织
    if args.use_adapter:
        args.output_dir = os.path.join(args.output_dir, f"{args.model_size}/", f"TokenSkip/", f"{args.compression_ratio}/")
    else:
        args.output_dir = os.path.join(args.output_dir, f"{args.model_size}/", f"Original/{args.data_type}/")

    # 读取评测配置
    test_conf = read_data(f"configs/{args.benchmark}_{args.data_type}.json")

    for src, info in test_conf.items():
        fname = os.path.join(args.output_dir, "test_data", "test.jsonl")
        input_dir = os.path.dirname(fname)
        os.makedirs(input_dir, exist_ok=True)
        metric_path = os.path.join(args.output_dir, "samples", "metrics.json")
        if os.path.exists(metric_path) and read_data(metric_path)['n_samples'] > 0:
            continue

        # 预处理数据 -> test.jsonl
        with open(fname, "w", encoding="utf-8") as file:
            data = read_data(info['test_path'])
            for i, sample in enumerate(tqdm(data, desc=f'processing {src}')):
                fn = eval(info['process_fn'])
                sample['id'] = sample.get('id', f"{src}-{i}")
                for j, item in enumerate(fn(sample)):
                    item['dataset'] = src
                    item['id'] = f"{src}-test-{i}-{j}"
                    assert 'answer' in item
                    print(json.dumps(item, ensure_ascii=False), file=file, flush=True)

        output_dir = os.path.join(args.output_dir, "samples")
        os.makedirs(output_dir, exist_ok=True)

        set_random_seed(args.seed)

        print("Loading data...", flush=True)
        test_data = []
        with open(os.path.join(input_dir, f"test.jsonl"), "r", encoding="utf-8") as fin:
            for line in fin:
                example = json.loads(line)
                messages = example['messages']
                assert messages[-1]['role'] == 'assistant'
                example['reference'] = example.get('reference', '') or [
                    mess['content'] for mess in messages if mess['role'] == 'assistant'
                ]
                for mess in messages:
                    if mess['role'] == 'assistant':
                        mess['content'] = ''
                example['messages'] = messages
                test_data.append(example)

        if args.max_num_examples and len(test_data) > args.max_num_examples:
            test_data = random.sample(test_data, args.max_num_examples)

        results, total_time = infer(args, test_data, info['answer_extraction_fn'])

        print("Finished inference...", flush=True)
        os.environ['TOKENIZERS_PARALLELISM'] = "false"

        invalid_outputs = []
        labels = []
        for item in results:
            if len(item['prediction']) == 0:
                invalid_outputs.append({'prompt': item['prompt'],
                                        'output': item['model_output'],
                                        'answer': item['prediction']})
                res = False
                extract_ans = None
            else:
                extract_ans = item['prediction']
                res = eval_math(item)
            labels.append(res)

        for item, label in zip(results, labels):
            item['accuracy'] = label

        print("Calculating accuracy...", flush=True)
        acc = sum(1 for x in results if x['accuracy']) / max(1, len(results))
        print("output acc = {:.5f}".format(acc * 100), flush=True)

        avg_cot_length = sum(item['cot_length'] for item in results) / max(1, len(results))
        print("output avg_cot_length = {:.5f}".format(avg_cot_length), flush=True)

        print("number of invalid outputs: {}".format(len(invalid_outputs)), flush=True)

        pred_fname = "predictions.jsonl"
        with open(os.path.join(output_dir, pred_fname), 'a+', encoding='utf-8') as fout:
            for item in results:
                fout.write(json.dumps(item, ensure_ascii=False) + '\n')

        metric_fname = "metrics.json"
        with open(os.path.join(output_dir, metric_fname), "w", encoding="utf-8") as fout:
            json.dump({
                "n_samples": len(results),
                "accuracy": sum(item['accuracy'] for item in results) / max(1, len(results)),
                "avg_cot_length": avg_cot_length,
                'sample_latency': total_time / max(1, len(test_data)),
            }, fout, indent=4, ensure_ascii=False)
