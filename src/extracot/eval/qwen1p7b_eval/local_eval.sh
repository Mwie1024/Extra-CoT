parser = argparse.ArgumentParser(description="Parallel vLLM inference on 10k subset with Qwen template via /v1/completions")
    parser.add_argument("--input", "-i", required=True, help="输入数据（JSONL 或 JSON 数组）")
    parser.add_argument("--output", "-o", required=True, help="输出 JSONL")
    parser.add_argument("--base_url", "-u", required=True, help="vLLM 地址，如 http://localhost:8000 或已含 /v1")
    parser.add_argument("--model", "-m", default="qwen3-8b-instruct", help="vLLM --served-model-name")
    parser.add_argument("--processes", "-p", type=int, default=8, help="并行进程数")
    parser.add_argument("--timeout", "-t", type=int, default=300, help="HTTP 超时（秒）")
    parser.add_argument("--resume", "-r", action="store_true", help="断点续跑")
    parser.add_argument("--sample_k", type=int, default=10000, help="抽样条数（默认10k）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--model_type", choices=["qwen", "llama3"], default="qwen", help="决定提问风格（此处仅用于兼容描述）")
    parser.add_argument("--compression_ratio", type=float, default=1.0, help="<1.0 时在 user 提示中附带 ratio 提示（自然语言）")

python val_one_ratio.py \
    --input /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/datasets/metamath_1k.jsonl \
    --output ./eval_result/qwen1.7b_75k_sft/0.2 \
    --vllm_base_url http://127.0.0.1:8000/v1 --vllm_api_key EMPTY \
    --model /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen3_1.7b_mixture_75k \
    --process 128 \
    --model_type qwen \
    --max_new_tokens 2048 --len_ctrl none \
    --sample_k 1000 \
    --compression_ratio 0.4