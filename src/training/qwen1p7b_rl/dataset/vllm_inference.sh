python vllm_inference.py --input /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/RL/dataset/metamath_60k.jsonl \
  --output_dir ratio_output_60k_0.8_1.0 \
  --base_url http://localhost:8001/v1 --model /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen3_1.7b_full_ratio_72k_cleaned \
  --model_type qwen --processes 128 --samples 1 --ratios 0.8,1.0