python longformer_compressor.py \
  --input_path input.jsonl \
  --model_dir  model_path \
  --output_dir ./Compression/SFT_15k \
  --ratios 0.9,0.8,0.7,0.6,0.5,0.4,0.3,0.2,0.1
