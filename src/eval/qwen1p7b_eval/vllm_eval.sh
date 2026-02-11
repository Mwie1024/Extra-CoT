python vllm_eval.py \
  --input dataset \
  --dataset_format ansaug \
  --out output_Dir \
  --vllm_base_url http://localhost:8003/v1 \
  --vllm_model  model_path \
  --tokenizer_path  model_path \
  --max_new_tokens 4096 \
  --ratio 2.0
