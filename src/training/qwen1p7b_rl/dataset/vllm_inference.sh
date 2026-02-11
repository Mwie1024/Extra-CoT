python vllm_inference.py 
  --input data_path \
  --output_dir ratio_output_60k_0.8_1.0 \
  --base_url http://localhost:8001/v1 \
  --model model_path \
  --model_type qwen --processes 128 --samples 1 --ratios 0.8,1.0
