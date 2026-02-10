export NUWA_BASE_URL="https://api.nuwaapi.com/v1"
export NUWA_API_KEY="sk-IjumAnpWkZ4pwleMSJesj3DghaPp6l8YeLZ6WkBCj8XjWmgV"

# 跑标注（输入既可以是 JSON 也可以是 JSONL）
python gpt4o_ranges_labeler.py \
  -i /Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/API/dataset_split/4k_sampled_data.jsonl \
  -o /Users/mwie/User/Data/Code/CoT-Language/CoT_Language/dataset_preparation/API/test.jsonl \
  --model gpt-4o \
  --max-workers 32 \
  --min-interval 0.12 \
  --prompt-detailed \
  --min-zpad 3 \
  --index-basis one_based_closed \
  --max-samples 5 \
  # --force-json
