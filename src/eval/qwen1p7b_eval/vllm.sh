CUDA_VISIBLE_DEVICES=5 python -m vllm.entrypoints.openai.api_server \
    --model model_path \
    --tensor-parallel-size 1 \
    --host 0.0.0.0 \
    --port 8003 \
    # --enable-thinking \
    
