CUDA_VISIBLE_DEVICES=5 python -m vllm.entrypoints.openai.api_server \
    --model /data/jbh/verl_ckpt/ablation/no_comp/step_200 \
    --tensor-parallel-size 1 \
    --host 0.0.0.0 \
    --port 8003 \
    # --enable-thinking \
    