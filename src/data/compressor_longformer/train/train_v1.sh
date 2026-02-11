export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7

torchrun --nproc_per_node=8 train_longformer.py \
  --data-path dataset.pt \
  --model-name longformer_path \
  --save-dir ./ckpt_longformer_large \
  --epochs 6 --batch-size 1 --grad-accum 16 \
  --lr 2e-5 --weight-decay 0.01 --warmup-ratio 0.06 \
  --fp16 --grad-chkpt --use-focal --use-global-attn \
  --num-workers 4 --early-stop 3
