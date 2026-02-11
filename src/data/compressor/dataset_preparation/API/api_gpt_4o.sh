# 跑标注（输入既可以是 JSON 也可以是 JSONL）
python gpt4o_ranges_labeler.py \
  -i input.jsonl \
  -o output.jsonl \
  --model gpt-4o \
  --max-workers 32 \
  --min-interval 0.12 \
  --prompt-detailed \
  --min-zpad 3 \
  --index-basis one_based_closed \
  --max-samples 5 \
  # --force-json
