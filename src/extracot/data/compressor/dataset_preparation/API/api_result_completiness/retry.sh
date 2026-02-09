#!/usr/bin/env bash
set -e
export NUWA_BASE_URL="https://api.nuwaapi.com/v1"
export NUWA_API_KEY="sk-IjumAnpWkZ4pwleMSJesj3DghaPp6l8YeLZ6WkBCj8XjWmgV"

# python retry_empty_chunks.py \
#   -d /Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/camel/camel_chunk_only_cot/chunked_result.jsonl\
#   -m ./merged_empty_ranges.jsonl \
#   -o ./retry_result.jsonl \
#   --model gpt-4o \
#   --max-workers 32 \
#   --min-interval 0.15 \
#   --max-tokens 256 \
#   --temperature 0.0 \
#   --force-json \
#   --prompt-detailed \
#   --min-zpad 3 \
#   --index-basis one_based_closed

python retry_empty_chunks.py \
  -d /Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/camel/camel_chunk_only_cot/chunked_result.jsonl\
  -m ./empty.jsonl \
  -o ./retry_result.jsonl \
  --rounds 10 --temperature 0.3 --temp-step 0.15 \
  --min-spans 3 --min-ratio 0.2 \
  --model gpt-4o --max-workers 32 --min-interval 0.1
