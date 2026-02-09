python eval_5_ctrl.py \
  --input_path /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/amc23_str.jsonl --dataset_format ansaug \
  --output_dir ./eval_result_amc/basemodel_5_ctrl/amc \
  --vllm_base_url http://localhost:8008/v1 --vllm_api_key EMPTY \
  --vllm_model /data/tyt/workspace/tyt/Models/qwen3-1.7B/qwen3_1.7b \
  --tokenizer_path /data/tyt/workspace/tyt/Models/qwen3-1.7B/qwen3_1.7b \
  --model_type qwen \
  --ratios 0.6 \
  --ctrls lc-prompt,truncation \
  --processes 32 --max_new_tokens 4096
