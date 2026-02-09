CUDA_VISIBLE_DEVICES=3 python -m vllm.entrypoints.openai.api_server \
    --model /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/tokenskip_qwen3_1.7b/checkpoint-6000 \
    --tensor-parallel-size 1 \
    --host 0.0.0.0 \
    --port 8009 \
    # --enable-thinking \
    