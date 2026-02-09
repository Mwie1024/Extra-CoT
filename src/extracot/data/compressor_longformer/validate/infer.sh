python /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/inference_metamath.py \
  --input /data/tyt/workspace/tyt/CoT/CoT-Language-master/validata_longformer/datasets/MetaMathQA-395K.json \
  --output  ./outputs/qwen2.5_7b_infer_50k.jsonl \
  --base_url http://127.0.0.1:8000 \
  --model /data/tyt/workspace/tyt/Models/Qwen2.5-7B-Instruct \
  --processes 32 \
  --sample_k 50000 \
  --timeout 300 \
  --resume
