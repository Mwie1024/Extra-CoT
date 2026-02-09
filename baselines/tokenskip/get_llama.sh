python get_llamafactory.py \
  --original /data/tyt/workspace/tyt/Code/efficient_reasoning/TokenSkip-main/outputs/Qwen2.5-7B-Instruct/gsm8k/7b/Original/train/samples/predictions_formatted.jsonl \
  --comp-dir ./Compression \
  --ratios 1.0,0.9,0.8,0.7,0.6,0.5 \
  --out ./outputs/test.json