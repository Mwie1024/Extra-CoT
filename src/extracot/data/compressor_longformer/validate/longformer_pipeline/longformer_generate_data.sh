python longformer_tokenizer_compression.py \
  --input_path /data/tyt/workspace/tyt/Code/efficient_reasoning/TokenSkip-main/outputs/Qwen2.5-7B-Instruct/gsm8k/3b/Original/train/samples/predictions_formatted.jsonl \
  --model_dir  /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/ckpt_longformer_large_full_data \
  --output_dir ./llama3.2_3b_instruct_v2 \
  --ratios 0.9,0.8,0.7,0.6,0.5