# 设置环境（单机4卡示例）
export MASTER_ADDR=127.0.0.1
export MASTER_PORT=29500
export CUDA_VISIBLE_DEVICES=0,1,2,3

# NCCL / network debug & safe settings for single-node
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=ALL
export NCCL_SOCKET_IFNAME=lo       # 强制使用 loopback（最稳妥）
export NCCL_IB_DISABLE=1          # 禁用 Infiniband（如果你不使用 IB）
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1

# 运行并保存完整输出
torchrun --nproc_per_node=4 --nnodes=1 --node_rank=0 \
  /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/src/llamafactory/launcher.py \
  /data/tyt/workspace/tyt/CoT/LLaMA-Factory-main/examples/train_full/qwen2.5_7b_ratio_auto_output_2468.yaml \
  2> ~/llf_torchrun_debug.log
