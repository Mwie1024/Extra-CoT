python longformer_compressor.py \
  --input_path /data/tyt/workspace/tyt/CoT/CoT-Language-master/Qwen3-1.7B/dataset/metamath_145k_query/selected_sft_seed_15k.jsonl \
  --model_dir  /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/ckpt_longformer_large_full_data \
  --output_dir ./Compression/SFT_15k \
  --ratios 0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1