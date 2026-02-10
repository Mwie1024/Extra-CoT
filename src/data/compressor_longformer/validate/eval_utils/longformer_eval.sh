#!/usr/bin/env bash
set -euo pipefail

# ====== 参数区（按需修改） ======
INPUT=/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/datasets/metamath_1k.jsonl
OUTROOT=/data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/eval_utils

# 三个 vLLM 实例分别监听 8000/8001/8002，对应三张卡
# MODELS="original,longformer,llmlingua2"
# URLS="http://127.0.0.1:8000,http://127.0.0.1:8001,http://127.0.0.1:8002"
# SERVED="/data/tyt/workspace/tyt/Models/Qwen3-8B,longformer_numeric,llmlingua2_numeric"
MODELS="longformer,llmlingua2"
URLS="http://127.0.0.1:8001,http://127.0.0.1:8002"
SERVED="longformer_numeric,llmlingua2_numeric"

# 原始模型标签
ORIG="original"

# 非原始模型跑这些压缩比（原始模型自动只跑 1.0 且不注入）
RATIOS="0.5"

# 每个模型内部的数据并发进程数（HTTP 请求并发）
PROC=32

# 模型级并发（想三模型同时跑就设 3；担心 CPU 过载可酌情调小）
PMODELS=2

# Qwen/llama3 用于选择 numeric 注入风格
MODEL_TYPE=qwen

# ====== 正式执行 ======
python evaluate.py \
  --input_path  "${INPUT}" \
  --output_root "${OUTROOT}" \
  --models       "${MODELS}" \
  --base_urls    "${URLS}" \
  --served_names "${SERVED}" \
  --original_model "${ORIG}" \
  --ratios "${RATIOS}" \
  --model_type "${MODEL_TYPE}" \
  --ratio_encoding numeric \
  --max_new_tokens 4096 \
  --processes ${PROC} \
  --parallel_models ${PMODELS} \
  --tokenizer_path /data/tyt/workspace/tyt/Models/Qwen3-8B  # 仅用于统计cot token长度，可不填
