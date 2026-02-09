python preview_longformer.py \
  -i ./test.jsonl \
  -m /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/ckpt_longformer_large_full_data \
  -o ./preview_test.jsonl \
  --ratios 0.1,0.2,0.3,0.5,0.7,0.9 \
  --limit 7

