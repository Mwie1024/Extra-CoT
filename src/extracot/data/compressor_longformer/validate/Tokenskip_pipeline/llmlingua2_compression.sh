python llmlingua2_compression.py \
  --input_path /data/tyt/workspace/tyt/Code/efficient_reasoning/TokenSkip-main/outputs/Qwen2.5-7B-Instruct/gsm8k/3b/Original/train/samples/predictions_formatted.jsonl \
  --output_dir  ./gsm8k_llama3.2_3b \
  --llmlingua_path /data/tyt/workspace/tyt/Models/llm_lingua_2_xlm-roberta-large \
  --ratios 0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1 \
  --chunk_tokens 450 \
