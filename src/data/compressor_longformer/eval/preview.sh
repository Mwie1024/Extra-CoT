python preview_longformer.py \
  -i input.jsonl \
  -m model_path \
  -o ./preview_test.jsonl \
  --ratios 0.1,0.2,0.3,0.5,0.7,0.9 \
  --limit 7

