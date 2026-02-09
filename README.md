# Extra-CoT (Release Skeleton)

This repo is a mechanically extracted + reorganized skeleton for open-sourcing.
It copies only source-like files (py/sh/yaml/md/...) and excludes data/artifacts.

## Layout
- src/extracot/data/compressor: compressor data building pipeline (legacy kept)
- src/extracot/data/sft: SFT data building pipeline (legacy kept)
- src/extracot/data/compressor_longformer: longformer pipeline scripts for compressor train/val/eval (legacy kept)
- src/extracot/training/qwen1p7b_rl: main RL training code (legacy kept)
- src/extracot/eval/qwen1p7b_eval: evaluation code (legacy kept)
- baselines/tokenskip: TokenSkip baseline scripts (only)

## Notes
- Large artifacts (parquet/ckpt/weights/logs) are intentionally excluded.
