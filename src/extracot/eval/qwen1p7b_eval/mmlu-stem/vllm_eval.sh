python eval_vllm.py \
  --input /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/evaluation/mmlu/data_jsonl/all_stem.jsonl \
  --dataset_format ansaug \
  --out ./eval_result_single_gpu/sft-tokenskip/mmlu \
  --vllm_base_url http://localhost:8009/v1 \
  --vllm_model  /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/tokenskip_qwen3_1.7b/checkpoint-6000 \
  --tokenizer_path  /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/tokenskip_qwen3_1.7b/checkpoint-6000  \
  --max_new_tokens 4096 \
  --ratio 0.8,1.0,2.0
