torchrun --nproc_per_node=8 train_longformer_v2.py \
  --data-path dataset.pt \
  --model-name longformer_path \
  --save-dir ./ckpt_longformer_large_full_data \
  --epochs 40 --lr 2e-5 --batch-size 2 \
  --grad-accum 16 --fp16 --use-global-attn \
  --eval-keep-ratios 0.1,0.2,0.3,0.4 --log-interval 50
