export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
# torchrun --standalone --nproc_per_node=8 train_longformer.py \
#   --data_pt /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/4k_dataset.pt \
#   --model_name /data/tyt/workspace/tyt/Models/longformer-large-4096 \
#   --save_dir ./ckpt/longformer_large_qrel \
#   --epochs 8 --batch_size 4 --grad_accum 16 \
#   --loss_type focal --alpha 1.0 --gamma 2.0 --use_class_weight \
#   --eval_keep_ratios 0.10,0.20,0.30 \
#   --fp16 --gradient_checkpointing \


torchrun --nproc_per_node=8 train_longformer.py \
  --data-path /data/tyt/workspace/tyt/CoT/CoT-Language-master/train_longformer/4k_dataset.pt \
  --model-name /data/tyt/workspace/tyt/Models/longformer-large-4096 \
  --save-dir ./ckpt_longformer_large \
  --epochs 6 --batch-size 1 --grad-accum 16 \
  --lr 2e-5 --weight-decay 0.01 --warmup-ratio 0.06 \
  --fp16 --grad-chkpt --use-focal --use-global-attn \
  --num-workers 4 --early-stop 3
