python special_token_vllm.py \
  --input dataset \
  --out output_dir \
  --vllm_base_url http://localhost:8003/v1 \
  --vllm_model model_path \
  --tokenizer_path model_path \
  --ratios 3.0
