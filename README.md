# Extra-CoT

This repository contains the **research code release** for **Extra-CoT**, a framework for **extreme-ratio Chain-of-Thought (CoT) compression** in large reasoning models.

The code focuses on **data preparation pipelines**, **SFT training configuration**, and **evaluation utilities** used in our experiments, primarily conducted on **Qwen1.7B**.

---

## Scope of this release

This repository provides:

- Data construction pipelines for:
  - CoT compressor training
  - Ratio-controlled SFT data
  - RL dataset preparation
- Evaluation scripts used in the experiments (vLLM-based)
- SFT training configuration based on **LLaMA-Factory**

---

## Supervised fine-tuning (SFT)

Supervised fine-tuning is performed using the included **LLaMA-Factory** codebase.

The configuration used in our experiments is:

```
cd llamafactory
llamafactory-cli train examples/train_full/qwen3_1.7b_extra_cot.yaml
```

------

## RL data preparation

The repository includes utilities for **RL dataset construction and conversion**, including:

- query-based filtering
- ratio-window sampling
- conversion to common RL data formats (e.g., verl-style)

The RL training loop itself is **intentionally not released** in this research code drop.

------

## Evaluation

Evaluation scripts are provided for reproducing the reported results, including:

- vLLM-based inference and evaluation
- special-token evaluation settings
- out-of-distribution evaluation utilities (e.g., MMLU-STEM)

------

## Notes on external APIs

Some data construction scripts may rely on external LLM APIs for labeling or formatting.
Users must configure API keys via environment variables.
No credentials or keys are included in this repository.

------

## License

**Research Only.**

This code is released strictly for research and academic use.
Commercial use is not permitted without explicit authorization.

------

