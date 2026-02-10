python vllm_eval.py \
  --input /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/eval/dataset/math-500/test.jsonl \
  --dataset_format ansaug \
  --out ./eval_result_ablation/no_comp/200/math \
  --vllm_base_url http://localhost:8003/v1 \
  --vllm_model  /data/jbh/verl_ckpt/ablation/no_comp/step_200 \
  --tokenizer_path  /data/jbh/verl_ckpt/ablation/no_comp/step_200 \
  --max_new_tokens 4096 \
  --ratio 2.0
