CUDA_VISIBLE_DEVICES=1 python evaluation.py \
  --model-path /data/tyt/workspace/tyt/Models/Qwen2.5-7B-Instruct \
  --tokenizer-path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/longformer_tokenizer_0.5_1.0_special_token/checkpoint-588 \
  --use_adapter \
  --adapter-path /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/saves/longformer_tokenizer_0.5_1.0_special_token/checkpoint-588 \
  --benchmark gsm8k --data-type test \
  --model-type qwen --model-size 7b \
  --eval_batch_size 128 \
  --compression_ratio 0.5
