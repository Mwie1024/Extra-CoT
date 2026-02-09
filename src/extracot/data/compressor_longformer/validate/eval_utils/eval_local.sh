CUDA_VISIBLE_DEVICES=4 python evaluate_local.py \
  --input_path /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/datasets/metamath_1k.jsonl \
  --output_dir ./outputs/Qwen2.5-7B-Instruct/metamath/longformer_none_mix \
  --dataset_format ansaug \
  --model_type qwen \
  --compression_ratio 1.0 \
  --ratio_injection special \
  --max_new_tokens 700 \
  --batch_size 32 --temperature 0.0 \
  --model_path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen2.5_7b_0.2_1.0_special_token \
  --tokenizer_path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen2.5_7b_0.2_1.0_special_token \
  # --model_path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen2.5_7b_metamath_0.2_1.0_spe0.2_special_token_mixture \
  # --tokenizer_path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/qwen2.5_7b_metamath_0.2_1.0_spe0.2_special_token_mixture \
  # --model_path /data/tyt/workspace/tyt/Models/Qwen2.5-7B-Instruct \
  # --tokenizer_path /data/tyt/workspace/tyt/Models/Qwen2.5-7B-Instruct \