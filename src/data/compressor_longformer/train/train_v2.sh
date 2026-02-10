torchrun --nproc_per_node=8 train_longformer_v2.py \
  --data-path /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/LONGFORMER_DATA.pt \
  --model-name /data/tyt/workspace/tyt/Models/longformer-large-4096 \
  --save-dir ./ckpt_longformer_large_full_data \
  --epochs 40 --lr 2e-5 --batch-size 2 \
  --grad-accum 16 --fp16 --use-global-attn \
  --eval-keep-ratios 0.1,0.2,0.3,0.4 --log-interval 50
