#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import glob
import gzip
import argparse
from typing import List, Dict, Any, Iterable, Optional, Tuple

import requests
from tqdm import tqdm
from multiprocessing import Pool, Manager
from filelock import FileLock

# ============================ 数据读取 ============================

def _iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    open_fn = gzip.open if path.endswith(".gz") else open
    with open_fn(path, "rt", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    yield obj
                elif isinstance(obj, list):
                    for it in obj:
                        if isinstance(it, dict):
                            yield it
            except Exception:
                continue

def _detect_is_json_array(path: str) -> Optional[bool]:
    open_fn = gzip.open if path.endswith(".gz") else open
    with open_fn(path, "rt", encoding="utf-8") as f:
        while True:
            ch = f.read(1)
            if ch == "":
                return None
            if ch.isspace():
                continue
            if ch == "[":
                return True
            if ch == "{":
                return False
            return None

def _iter_json_array(path: str) -> Iterable[Dict[str, Any]]:
    open_fn = gzip.open if path.endswith(".gz") else open
    with open_fn(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        yield data
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                yield it

def stream_records_any(path: str) -> Iterable[Dict[str, Any]]:
    kind = _detect_is_json_array(path)
    if kind is True:
        yield from _iter_json_array(path)
    elif kind is False or kind is None:
        yield from _iter_jsonl(path)

def load_all_records(path: str) -> List[Dict[str, Any]]:
    files = []
    if os.path.isdir(path):
        for ext in ("*.jsonl", "*.jsonl.gz", "*.json", "*.json.gz"):
            files.extend(sorted(glob.glob(os.path.join(path, ext))))
    else:
        files = [path]

    recs = []
    for fp in files:
        recs.extend([x for x in stream_records_any(fp) if isinstance(x, dict)])
    return recs

# ============================ Prompt 组装（仅 special_token） ============================

def build_user_text_special(query: str, ratio: float, special_prefix: str = "COMP_") -> str:
    head = "Please reason step by step, and put your final answer within \\boxed{}."
    user = f"{head}\n{query}".rstrip()
    if ratio <= 1.0:
        tok = f"<{special_prefix}{int(round(ratio * 100))}>"
        user = f"{user} {tok}"
    return user

def build_prompt_local(user_text: str, model_type: str) -> str:
    mt = model_type.lower()
    if mt == "qwen":
        return (
            "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n"
            f"<|im_start|>user\n{user_text}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
    elif mt == "llama3":
        return (
            "<|start_header_id|>system<|end_header_id|>\n\nYou are a helpful assistant."
            "<|eot_id|>"
            "<|start_header_id|>user<|end_header_id|>\n\n"
            f"{user_text}"
            "<|eot_id|>"
            "<|start_header_id|>assistant<|end_header_id|>\n\n"
        )
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

def default_stops(model_type: str):
    return ["<|im_end|>"] if model_type.lower() == "qwen" else ["<|eot_id|>"]

# ============================ 评测辅助（选 best-of） ============================

BOXED_RE = re.compile(
    r"the\s*final\s*answer\s*is\s*:\s*\$?\\boxed\{(.+?)\}\$?",
    flags=re.IGNORECASE | re.DOTALL
)
GT_RE = re.compile(
    r"the\s*answer\s*is\s*:\s*(.+?)\s*\Z",
    flags=re.IGNORECASE | re.DOTALL
)
_LATEX_STRIP = re.compile(r"\\[a-zA-Z]+|[{}$^_]|\\")

def _last_match(regex: re.Pattern, s: str) -> Optional[re.Match]:
    last = None
    for m in regex.finditer(s or ""):
        last = m
    return last

def extract_boxed_answer(model_output: str) -> Optional[str]:
    m = _last_match(BOXED_RE, model_output or "")
    return m.group(1).strip() if m else None

def extract_gt_answer(response: str) -> Optional[str]:
    text = (response or "").rstrip()
    m = _last_match(GT_RE, text)
    if not m:
        return None
    ans = m.group(1).strip()
    return ans or None

def _normalize_ans(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = s.strip()
    s = _LATEX_STRIP.sub("", s)
    s = re.sub(r"\s+", "", s)
    s = s.strip(" .;,:")
    return s

def _to_float_maybe(s: str) -> Optional[float]:
    try:
        if "/" in s and not any(c.isalpha() for c in s):
            num, den = s.split("/", 1)
            return float(num) / float(den)
        return float(s)
    except Exception:
        return None

def answers_equal(pred: Optional[str], gt: Optional[str]) -> bool:
    if pred is None or gt is None:
        return False
    p_norm = _normalize_ans(pred)
    g_norm = _normalize_ans(gt)
    if not p_norm or not g_norm:
        return False
    p_val = _to_float_maybe(p_norm)
    g_val = _to_float_maybe(g_norm)
    if p_val is not None and g_val is not None:
        return abs(p_val - g_val) <= 1e-8
    return p_norm == g_norm

def choose_best_of(candidates: List[str], gt_text: Optional[str]) -> Tuple[int, str, str]:
    """
    返回 (best_idx, reason, chosen_text)
    规则：正确 > 有boxed > 最长
    """
    # 1) 正确
    for i, text in enumerate(candidates):
        if answers_equal(extract_boxed_answer(text), gt_text):
            return i, "correct", text
    # 2) 有boxed（格式正确）
    for i, text in enumerate(candidates):
        if extract_boxed_answer(text) is not None:
            return i, "boxed_only", text
    # 3) 最长
    best_idx = max(range(len(candidates)), key=lambda k: len(candidates[k] or ""))
    return best_idx, "longest", candidates[best_idx]

# ============================ vLLM 客户端（/v1/completions） ============================

class VLLMInferenceAligned:
    """
    通过 /v1/completions 发送与本地完全相同的字符串 prompt。
    一次请求用 n>1 返回多采样，便于做 best-of。
    """
    def __init__(self, base_url: str, model_name: str,
                 timeout: int = 300, temperature: float = 0.7, top_p: float = 0.95, top_k: int = 50,
                 max_tokens: int = 512, model_type: str = "qwen",
                 special_prefix: str = "COMP_", seed: Optional[int] = 42,
                 use_stop: bool = True, samples: int = 3):
        base = base_url.rstrip('/')
        self.base_url = base if base.endswith('/v1') else (base + '/v1')
        self.url = f"{self.base_url}/completions"

        self.model_name = model_name
        self.timeout = timeout
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.max_tokens = max_tokens
        self.model_type = model_type
        self.special_prefix = special_prefix
        self.seed = seed
        self.use_stop = use_stop
        self.samples = max(1, int(samples))

    def inference_multi(self, data: Dict[str, Any], ratio: float) -> Dict[str, Any]:
        try:
            q = (data.get("query") or data.get("original_question") or data.get("question") or "").strip()
            user_text = build_user_text_special(q, ratio, self.special_prefix)
            prompt = build_prompt_local(user_text, self.model_type)

            payload = {
                "model": self.model_name,
                "prompt": prompt,
                "temperature": float(self.temperature),
                "top_p": float(self.top_p),
                "max_tokens": int(self.max_tokens),
                "n": int(self.samples),
            }
            if self.top_k is not None and int(self.top_k) > 0:
                payload["top_k"] = int(self.top_k)
            if self.seed is not None:
                payload["seed"] = int(self.seed)
            if self.use_stop:
                payload["stop"] = default_stops(self.model_type)

            headers = {"Content-Type": "application/json"}
            resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                js = resp.json()
                choices = js.get("choices", []) or []
                texts = [c.get("text", "") for c in choices]
                finish_reasons = [c.get("finish_reason") for c in choices]
                return {
                    "id": data.get("id"),
                    "responses": texts,
                    "finish_reasons": finish_reasons,
                    "status": "success",
                    "raw_response": js,
                    "prompt": prompt
                }
            else:
                return {
                    "id": data.get("id"),
                    "responses": None,
                    "status": "error",
                    "error": f"HTTP {resp.status_code}: {resp.text}"
                }
        except Exception as e:
            return {
                "id": data.get("id"),
                "responses": None,
                "status": "error",
                "error": str(e)
            }

# ============================ 工具：vLLM 健康检查/写盘/断点 ============================

def check_vllm_server(base_url: str) -> bool:
    base = base_url.rstrip('/')
    url = base if base.endswith('/v1') else (base + '/v1')
    try:
        resp = requests.get(f"{url}/models", timeout=10)
        return resp.status_code == 200
    except Exception:
        return False

def save_result_to_file(result: Dict[str, Any], output_file: str, lock_file: str):
    try:
        with FileLock(lock_file):
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:
        print(f"Error saving result: {e}")

def get_completed_ids(output_file: str) -> set:
    """
    断点：若该 id 已成功写过（best-of 一行），则跳过。
    注意：若此前用不同 --samples 跑过，resume 仍会跳过；建议改输出路径或关闭 --resume。
    """
    done = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                        if obj.get("status") == "success" and obj.get("id"):
                            done.add(obj["id"])
                    except Exception:
                        pass
        except Exception as e:
            print(f"Warning: error reading output file: {e}")
    return done

def split_data(data: List[Dict[str, Any]], num_processes: int) -> List[List[Dict[str, Any]]]:
    n = len(data)
    if num_processes <= 1:
        return [data]
    size = (n + num_processes - 1) // num_processes
    return [data[i:i + size] for i in range(0, n, size)]

# ============================ Worker ============================

def worker_fn(args):
    (data_chunk, base_url, model_name, timeout, progress_dict, worker_id,
     out_best_file, out_raw_file, lock_best, lock_raw, model_type, special_prefix, ratio,
     temperature, top_p, top_k, max_tokens, seed, use_stop, samples, save_all_samples) = args

    client = VLLMInferenceAligned(
        base_url=base_url, model_name=model_name, timeout=timeout,
        temperature=temperature, top_p=top_p, top_k=top_k, max_tokens=max_tokens,
        model_type=model_type, special_prefix=special_prefix, seed=seed,
        use_stop=use_stop, samples=samples
    )

    succ = err = 0
    for i, data in enumerate(data_chunk):
        try:
            # id 必须存在（在主流程做过校验）
            assert data.get("id"), "record missing id (validated earlier)"

            res = client.inference_multi(data, ratio=ratio)

            # 元信息
            merged_base = dict(data)
            merged_base["compression_ratio"] = ratio
            lvl = int(round(max(0.0, min(1.0, float(ratio))) * 100))
            merged_base["special_token"] = f"<{special_prefix}{lvl}>"

            if res.get("status") == "success":
                # 读 GT：从数据字段 response 末行 "The answer is: ..."
                gt_text = extract_gt_answer(merged_base.get("response", ""))

                responses = res.get("responses") or []
                # 选择 best-of
                if responses:
                    best_idx, reason, chosen_text = choose_best_of(responses, gt_text)
                else:
                    best_idx, reason, chosen_text = (0, "empty", "")

                # BEST 行（与原脚本兼容）
                best_line = dict(merged_base)
                best_line["model_output"] = chosen_text
                best_line["status"] = "success"
                best_line["choice_index"] = best_idx
                best_line["choice_reason"] = reason
                best_line["num_samples"] = int(samples)
                if "prompt" in res:
                    best_line["prompt"] = res["prompt"]

                save_result_to_file(best_line, out_best_file, lock_best)

                # RAW 行（可选）
                if save_all_samples:
                    raw_line = dict(merged_base)
                    raw_line["status"] = "success"
                    raw_line["all_outputs"] = responses
                    raw_line["finish_reasons"] = res.get("finish_reasons")
                    raw_line["prompt"] = res.get("prompt")
                    save_result_to_file(raw_line, out_raw_file, lock_raw)

                succ += 1
            else:
                err_line = dict(merged_base)
                err_line["model_output"] = None
                err_line["status"] = "error"
                err_line["error"] = res.get("error")
                save_result_to_file(err_line, out_best_file, lock_best)
                if save_all_samples:
                    save_result_to_file(err_line, out_raw_file, lock_raw)
                err += 1

            progress_dict[f"{worker_id}_processed"] = i + 1
            progress_dict[f"{worker_id}_success"] = succ
            progress_dict[f"{worker_id}_error"] = err

        except Exception as e:
            lvl = int(round(max(0.0, min(1.0, float(ratio))) * 100))
            merged = dict(data)
            merged["compression_ratio"] = ratio
            merged["special_token"] = f"<{special_prefix}{lvl}>"
            merged["status"] = "error"
            merged["error"] = f"Worker exception: {e}"
            save_result_to_file(merged, out_best_file, lock_best)
            if save_all_samples:
                save_result_to_file(merged, out_raw_file, lock_raw)
            err += 1
            progress_dict[f"{worker_id}_processed"] = i + 1
            progress_dict[f"{worker_id}_success"] = succ
            progress_dict[f"{worker_id}_error"] = err

    return {"worker_id": worker_id, "total_processed": len(data_chunk),
            "success_count": succ, "error_count": err}

# ============================ 主流程 ============================

def parse_ratios(r: str) -> List[float]:
    vals = []
    for x in r.split(","):
        x = x.strip()
        if not x:
            continue
        vals.append(float(x))
    return sorted(set(vals))

def main():
    ap = argparse.ArgumentParser(description="vLLM evaluation with local-aligned prompt (special_token, multi-sampling).")
    ap.add_argument("--input", "-i", required=True, help="输入数据（jsonl/json/目录，支持 *.jsonl/.json(.gz)）")
    ap.add_argument("--output_dir", "-o", required=True, help="输出目录（每个压缩比两个文件：rXXX.best.jsonl / rXXX.raw.jsonl）")
    ap.add_argument("--base_url", "-u", required=True, help="vLLM 地址，如 http://localhost:8000 或已含 /v1")
    ap.add_argument("--model", "-m", required=True, help="vLLM 服务暴露的模型名（--served-model-name）")
    ap.add_argument("--model_type", choices=["qwen", "llama3"], default="qwen")

    ap.add_argument("--processes", "-p", type=int, default=8)
    ap.add_argument("--timeout", "-t", type=int, default=300)
    ap.add_argument("--resume", "-r", action="store_true", help="断点续跑：按 BEST 输出文件中已成功的 id 跳过")
    ap.add_argument("--ratios", default="0.2,0.4,0.6,0.8,1.0",
                    help="逗号分隔；如 0.2,0.4,0.6,0.8,1.0")
    ap.add_argument("--special_prefix", default="COMP_", help="special token 前缀（形如 <COMP_20>）")

    # 采样/解码参数（给出合理默认值）
    ap.add_argument("--samples", type=int, default=3, help="每条样本采样个数 (n)")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--top_p", type=float, default=0.95)
    ap.add_argument("--top_k", type=int, default=50)
    ap.add_argument("--max_tokens", type=int, default=4096)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no_stop", action="store_true", help="不传 stop 标记（默认会传）")
    ap.add_argument("--save_all_samples", action="store_true", help="额外保存全部候选到 *.raw.jsonl")

    args = ap.parse_args()

    # 1) 健康检查
    print(f"Checking vLLM server at {args.base_url} ...")
    if not check_vllm_server(args.base_url):
        print(f"Error: cannot connect to vLLM server at {args.base_url}")
        raise SystemExit(1)
    print("✓ vLLM server is accessible")

    # 2) 读取数据
    print(f"Loading dataset from {args.input} ...")
    data_all = load_all_records(args.input)
    if not data_all:
        print("No records found. Exit.")
        raise SystemExit(1)

    # 强校验：每条记录必须自带非空且全局唯一的 id
    missing = [i for i, rec in enumerate(data_all) if not rec.get("id")]
    if missing:
        raise ValueError(f"{len(missing)} record(s) missing 'id'. First indices: {missing[:10]}")
    ids = [rec["id"] for rec in data_all]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate 'id' detected in input. Ids must be unique.")
    print(f"Loaded {len(data_all)} records (ids checked)")

    os.makedirs(args.output_dir, exist_ok=True)
    ratios = parse_ratios(args.ratios)
    print(f"Ratios to run (SPECIAL token): {ratios}")

    # 3) 逐个压缩比运行
    for r in ratios:
        tag = f"r{int(round(r*100)):03d}" if r < 1.0 else "r100"
        best_file = os.path.join(args.output_dir, f"{tag}.best.jsonl")
        raw_file  = os.path.join(args.output_dir, f"{tag}.raw.jsonl")
        lock_best = best_file + ".lock"
        lock_raw  = raw_file + ".lock"

        if args.resume and os.path.exists(best_file):
            done_ids = get_completed_ids(best_file)
            remain = [x for x in data_all if x["id"] not in done_ids]
            print(f"[{tag}] resume: done={len(done_ids)}  remain={len(remain)}  -> {best_file}")
        else:
            # 清空旧文件
            for fp in (best_file, raw_file):
                if os.path.exists(fp):
                    open(fp, "w").close()
            remain = data_all
            print(f"[{tag}] fresh run: total={len(remain)} -> {best_file}")

        if not remain:
            print(f"[{tag}] nothing to do, skip.")
            continue

        num_proc = max(1, min(args.processes, len(remain)))
        chunks = split_data(remain, num_proc)
        print(f"[{tag}] starting with {num_proc} processes; items per process: {[len(c) for c in chunks]}")

        manager = Manager()
        progress = manager.dict()

        worker_args = []
        for i, chunk in enumerate(chunks):
            progress[f"{i}_processed"] = 0
            progress[f"{i}_success"] = 0
            progress[f"{i}_error"] = 0
            worker_args.append((
                chunk, args.base_url, args.model, args.timeout, progress, i,
                best_file, raw_file, lock_best, lock_raw,
                args.model_type, args.special_prefix, r,
                args.temperature, args.top_p, args.top_k, args.max_tokens, args.seed, (not args.no_stop),
                args.samples, args.save_all_samples
            ))

        start = time.time()
        with Pool(processes=num_proc) as pool:
            result_async = pool.map_async(worker_fn, worker_args)

            with tqdm(total=len(remain), desc=f"{tag} Processing") as pbar:
                last_total = 0
                while not result_async.ready():
                    cur_total = sum(progress.get(f"{i}_processed", 0) for i in range(num_proc))
                    cur_succ = sum(progress.get(f"{i}_success", 0) for i in range(num_proc))
                    cur_err  = sum(progress.get(f"{i}_error", 0) for i in range(num_proc))
                    pbar.update(cur_total - last_total)
                    last_total = cur_total
                    elapsed = max(1e-6, time.time() - start)
                    rate = cur_total / elapsed
                    pbar.set_description(f"{tag} (Success: {cur_succ}, Error: {cur_err}, Rate: {rate:.1f}/s)")
                    time.sleep(1)
                cur_total = sum(progress.get(f"{i}_processed", 0) for i in range(num_proc))
                pbar.update(cur_total - last_total)

            stats = result_async.get()

        elapsed = time.time() - start
        total_success = sum(s["success_count"] for s in stats)
        total_error   = sum(s["error_count"] for s in stats)
        total = total_success + total_error

        print("\n" + "="*60)
        print(f"[{tag}] Completed!")
        print(f"Total time: {elapsed:.2f}s")
        print(f"Processed: {total}  | Success: {total_success}  | Error: {total_error}")
        if total:
            print(f"Success rate: {100.0 * total_success / total:.2f}%")
            print(f"Avg time/item: {elapsed / total:.3f}s  | Throughput: {total / elapsed:.2f} it/s")
        print(f"BEST results saved to: {best_file}")
        if args.save_all_samples:
            print(f"RAW  results saved to: {raw_file}")

        try:
            for lf in (lock_best, lock_raw):
                if os.path.exists(lf):
                    os.remove(lf)
        except Exception:
            pass

        print("Worker stats:")
        for s in stats:
            print(f"  Worker {s['worker_id']}: {s['success_count']}/{s['total_processed']}")
        print("="*60 + "\n")

if __name__ == "__main__":
    main()
