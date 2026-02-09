python build_llamafactory_input.py \
  --original_path /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/outputs/qwen2.5_correct.jsonl \
  --ratio_encoding numeric \
  --model_type qwen \
  --output_path outputs/retry_new_compressor_formatted.json \
  --longformer_dir /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/longformer_pipeline/qwen2.5-7b-metamath \
  # --lingua_dir /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/Tokenskip_pipeline/compression_llmlingua2 \
