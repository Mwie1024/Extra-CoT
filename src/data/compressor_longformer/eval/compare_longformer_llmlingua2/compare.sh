python compare.py \
  --llmlingua_model /data/tyt/workspace/tyt/Models/llm_lingua_2_xlm-roberta-large \
  --longformer_model /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/ckpt_longformer_large_full_data \
  --ratios 0.2,0.4,0.6,0.8 \
  --target_tokens 2500 \
  --output_dir outputs \
  --amp none