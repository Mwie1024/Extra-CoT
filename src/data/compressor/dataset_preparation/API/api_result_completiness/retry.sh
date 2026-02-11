#!/usr/bin/env bash
set -e
python retry_empty_chunks.py \
  -d input.jsonl\
  -m ./empty.jsonl \
  -o ./retry_result.jsonl \
  --rounds 10 --temperature 0.3 --temp-step 0.15 \
  --min-spans 3 --min-ratio 0.2 \
  --model gpt-4o --max-workers 32 --min-interval 0.1
