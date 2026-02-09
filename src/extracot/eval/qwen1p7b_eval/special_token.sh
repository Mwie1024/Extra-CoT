python special_token_vllm.py \
  --input /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/gsm8k/test.jsonl \
  --dataset_format ansaug \
  --out ./eval_result_thinkless/300/gsm8k \
  --vllm_base_url http://localhost:8003/v1 \
  --vllm_model /data/jbh/verl_ckpt/DeGRPO/step_300 \
  --tokenizer_path /data/jbh/verl_ckpt/DeGRPO/step_300 \
  --ratios 3.0
