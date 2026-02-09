CUDA_VISIBLE_DEVICES=5 python -m vllm.entrypoints.openai.api_server \
    --model /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen3_1.7b_full_ratio_72k_cleaned \
    --tensor-parallel-size 1 \
    --host 0.0.0.0 \
    --port 8000 \
    # --enable-thinking \
    