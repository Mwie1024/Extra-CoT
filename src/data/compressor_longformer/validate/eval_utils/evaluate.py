#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Evaluate multiple models served by vLLM (OpenAI-compatible HTTP) over a dataset,
with per-model × per-ratio outputs. Ratio injection strictly follows your dataset
builder's rules:

  ratio_encoding:
    - numeric:
        * qwen   -> append "<|eot_id|>{ratio:.1f}<|eot_id|>" to user (only if ratio<1.0)
        * llama3 -> append a new line "compression_ratio: {ratio:.1f}" to user (only if ratio<1.0)
    - special: prepend "<{prefix}{int(ratio*100)}>" as a standalone line (always add, including 1.0)
    - none   : do not inject

Features
- Model-level parallelism (via ProcessPoolExecutor) + data-level parallelism (via multiprocessing.Pool)
- One output directory per (model_tag, ratio): <output_root>/<model_tag>/<ratio>/{predictions.jsonl, metrics.json}
- Robust progress display:
    * Default tqdm with assignment updates (pbar.n = cur; pbar.refresh())
    * Optionally disable tqdm (--no_tqdm) and use single-line logs (recommended when parallel_models > 1)
"""

import os
import re
import json
import argparse
import time
import hashlib
import sys
from typing import List, Dict, Any, Optional, Tuple
from multiprocessing import Pool, Manager
from concurrent.futures import ProcessPoolExecutor, as_completed

import requests
from tqdm import tqdm
from filelock import FileLock

# ============================ Read data ============================

def read_data(path: str) -> List[Dict[str, Any]]:
    data = []
    if path.endswith(".jsonl"):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        data.append(obj)
                except Exception:
                    pass
    elif path.endswith(".json"):
        data = json.load(open(path, "r", encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("JSON 文件应为数组")
    else:
        raise ValueError("仅支持 .jsonl / .json")
    return data

# ============================ Answer extraction & matching ============================

BOXED_RE = re.compile(r"\\boxed\s*\{([^}]*)\}", flags=re.IGNORECASE)
ANS_RE   = re.compile(r"the\s+answer\s+is:\s*(.+)\s*$", flags=re.IGNORECASE | re.DOTALL)
NUM_RE   = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
FRAC_RE  = re.compile(r"^[+-]?\d+\s*/\s*[+-]?\d+$")
PCT_RE   = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")

def extract_pred_from_output(text: str) -> str:
    if not text:
        return ""
    ms = BOXED_RE.findall(text)
    return ms[-1].strip() if ms else ""

def extract_gold_from_response(text: str) -> str:
    if not text:
        return ""
    last = None
    for m in ANS_RE.finditer(text):
        last = m.group(1)
    return last.strip() if last else ""

def _latex_to_plain(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u2212","-").replace("−","-").replace("–","-").replace("$","")
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", s)
    s = re.sub(r"\\(left|right|,|;|!|:)", "", s)
    s = re.sub(r"\\(?:text|mathrm|operatorname)\s*\{([^{}]*)\}", r"\1", s)
    if "=" in s:
        s = s.split("=")[-1]
    s = s.strip().strip("()[]")
    s = re.sub(r"[。．\.，,;；:：!！?？]+$", "", s)
    return s.strip()

def _to_numeric(s: str):
    t = s.strip()
    if not t:
        return ("string","")
    if PCT_RE.match(t):
        return ("percent", float(t[:-1]))
    if FRAC_RE.match(t.replace(" ", "")):
        a, b = t.replace(" ", "").split("/")
        try:
            return ("fraction", (int(a), int(b)))
        except Exception:
            return ("string", t.lower())
    if NUM_RE.match(t):
        try:
            return ("number", float(t))
        except Exception:
            return ("string", t.lower())
    return ("string", t.lower())

def answers_equal(gold_raw: str, pred_raw: str, tol: float = 1e-9) -> bool:
    g = _latex_to_plain(gold_raw)
    p = _latex_to_plain(pred_raw)
    kg, vg = _to_numeric(g)
    kp, vp = _to_numeric(p)
    try:
        if kg=="fraction" and kp=="fraction": return vg[0]*vp[1] == vp[0]*vg[1]
        if kg=="fraction" and kp=="number":   return abs(vg[0]/vg[1] - vp) <= tol
        if kg=="number" and kp=="fraction":   return abs(vp[0]/vp[1] - vg) <= tol
        if kg=="number" and kp=="number":     return abs(vg - vp) <= tol
        if kg=="percent" and kp=="percent":   return abs(vg - vp) <= 100*tol
        if kg=="percent" and kp=="number":    return abs(vg/100.0 - vp) <= tol
        if kg=="number" and kp=="percent":    return abs(vg - vp/100.0) <= tol
    except Exception:
        pass
    def s(x: str) -> str:
        x = x.strip()
        x = re.sub(r"\s+", "", x).replace(",", "")
        x = re.sub(r"[。．\.]+$", "", x)
        return x.lower()
    return s(g) == s(p)

# ============================ vLLM HTTP client ============================

class VLLMClient:
    """
    ratio_encoding: "numeric" | "special" | "none"
      - numeric:
          * qwen   -> append "<|eot_id|>{ratio:.1f}<|eot_id|>" to user (only ratio<1.0)
          * llama3 -> append a new line "compression_ratio: {ratio:.1f}" (only ratio<1.0)
      - special: prepend "<{prefix}{int(ratio*100)}>" as a standalone line (always add)
      - none   : no injection
    """
    def __init__(self, base_url: str, served_name: str, timeout: int = 300,
                 model_type: str = "qwen", ratio_encoding: str = "numeric",
                 ratio: float = 1.0, special_token_prefix: str = "comp",
                 max_new_tokens: int = 512):
        base = base_url.rstrip('/')
        self.url = (base if base.endswith('/v1') else base + '/v1') + "/chat/completions"
        self.model = served_name
        self.timeout = timeout
        self.mt = model_type.lower()      # qwen | llama3
        self.enc = ratio_encoding.lower() # numeric | special | none
        self.ratio = float(ratio)
        self.special_prefix = special_token_prefix
        self.max_new = max_new_tokens

    def _build_user_text(self, query: str) -> str:
        header = "Please reason step by step, and put your final answer within \\boxed{}."
        user = f"{header}\n{query}"
        r = self.ratio
        if self.enc == "numeric" and r < 1.0:
            if self.mt == "qwen":
                user = f"{user}<|eot_id|>{r:.1f}<|eot_id|>"
            else:  # llama3
                user = f"{user}\ncompression_ratio: {r:.1f}"
        elif self.enc == "special":
            tok = f"<{self.special_prefix}{int(round(r*100))}>"
            user = f"{tok}\n{user}"
        return user

    def _build_messages(self, rec: Dict[str, Any]) -> List[Dict[str,str]]:
        q = (rec.get("query") or rec.get("original_question") or "").strip()
        user = self._build_user_text(q)
        return [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user",   "content": user}
        ]

    def infer_one(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        try:
            payload = {
                "model": self.model,
                "messages": self._build_messages(rec),
                "temperature": 0.0,
                "top_p": 0.95,
                "max_tokens": self.max_new,
                "stream": False,
            }
            r = requests.post(self.url, json=payload, timeout=self.timeout,
                              headers={"Content-Type":"application/json"})
            if r.status_code == 200:
                js = r.json()
                text = js.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {"status":"success", "response":text, "raw":js}
            else:
                return {"status":"error", "error": f"HTTP {r.status_code}: {r.text}"}
        except Exception as e:
            return {"status":"error", "error": str(e)}

# ============================ Safe append ============================

def save_jsonl_line_safe(obj: Dict[str, Any], out_path: str, lock_path: str):
    try:
        with FileLock(lock_path):
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                f.flush(); os.fsync(f.fileno())
    except Exception as e:
        print(f"[save] {e}")

# ============================ Worker (data-level parallel) ============================

def _stable_id(ex: Dict[str,Any]) -> str:
    key = json.dumps({
        "query": ex.get("query"),
        "original_question": ex.get("original_question"),
        "type": ex.get("type"),
        "response": ex.get("response")
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

def worker(args):
    (chunk, base_url, served_name, timeout, progress, wid, out_pred, lock_file,
     model_type, ratio_encoding, ratio, special_token_prefix, max_new_tokens,
     tokenizer_path) = args

    tok = None
    if tokenizer_path:
        try:
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=True, use_fast=True)
        except Exception:
            tok = None

    client = VLLMClient(base_url, served_name, timeout, model_type,
                        ratio_encoding, ratio, special_token_prefix, max_new_tokens)

    succ = err = 0
    for i, ex in enumerate(chunk):
        try:
            if not ex.get("id"):
                ex["id"] = _stable_id(ex)

            res = client.infer_one(ex)
            out_text = res.get("response") if res.get("status")=="success" else ""
            gold = extract_gold_from_response(ex.get("response",""))
            pred = extract_pred_from_output(out_text)

            cutoff = out_text.split("\n\nThe final answer is:")[0]
            if tok is not None:
                cot_len = len(tok(cutoff, add_special_tokens=False).input_ids)
            else:
                cot_len = len(cutoff)

            row = {
                "id": ex["id"],
                "query": ex.get("query"),
                "original_question": ex.get("original_question"),
                "response": ex.get("response"),
                "type": ex.get("type"),
                "model_output": out_text,
                "gold_extracted": gold,
                "pred_extracted": pred,
                "accuracy": bool(gold) and bool(pred) and answers_equal(gold, pred),
                "cot_length": cot_len,
            }
            save_jsonl_line_safe(row, out_pred, lock_file)

            if row["accuracy"]:
                succ += 1
            if res.get("status") != "success":
                err += 1

            progress[f"{wid}_processed"] = i+1
            progress[f"{wid}_succ"] = succ
            progress[f"{wid}_err"] = err

        except Exception as e:
            rr = {"id": ex.get("id") or _stable_id(ex), "status":"error", "error": f"Worker exception: {e}"}
            save_jsonl_line_safe(rr, out_pred, lock_file)
            err += 1
            progress[f"{wid}_processed"] = i+1
            progress[f"{wid}_succ"] = succ
            progress[f"{wid}_err"] = err

    return {"worker_id": wid, "total": len(chunk), "succ": succ, "err": err}

# ============================ Utils ============================

def check_vllm_server(base_url: str) -> bool:
    base = base_url.rstrip('/')
    url = base if base.endswith('/v1') else base + '/v1'
    try:
        r = requests.get(f"{url}/models", timeout=10)
        return r.status_code == 200
    except Exception:
        return False

def split_list(xs: List[Any], nproc: int) -> List[List[Any]]:
    n = len(xs)
    if nproc <= 1:
        return [xs]
    size = (n + nproc - 1) // nproc
    return [xs[i:i+size] for i in range(0, n, size)]

def write_metrics(pred_path: str, out_path: str, total_time: float, engine_desc: str, ratio_desc: str):
    n = acc = cot_sum = 0
    with open(pred_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            n += 1
            acc += int(bool(obj.get("accuracy")))
            cot_sum += int(obj.get("cot_length") or 0)
    mets = {
        "n_samples": n,
        "accuracy": (acc / n) if n else 0.0,
        "avg_cot_length": (cot_sum / n) if n else 0.0,
        "sample_latency": (total_time / n) if n else 0.0,
        "total_time_sec": total_time,
        "engine": engine_desc,
        "ratio": ratio_desc,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mets, f, ensure_ascii=False, indent=2)
    return mets

# ============================ Per-model evaluation (for parallel run) ============================

def eval_one_model(
    input_path: str, output_root: str,
    tag: str, base_url: str, served_name: str,
    original_model: str, ratios: List[float],
    max_new_tokens: int, processes: int, timeout: int,
    model_type: str, ratio_encoding: str, special_token_prefix: str,
    tokenizer_path: str, no_tqdm: bool, progress_refresh: float
) -> str:
    is_original = (tag == original_model)
    work_ratios = [1.0] if is_original else ratios

    # Read once inside subproc (avoid pickling a large dataset)
    records = read_data(input_path)
    if not records:
        raise RuntimeError("输入为空")

    if not check_vllm_server(base_url):
        raise RuntimeError(f"vLLM 不可达: {base_url} (model={served_name})")

    print(f"\n=== Model [{tag}] @ {base_url} served_name={served_name} ===")
    print(f"Ratios: {', '.join(f'{r:.1f}' for r in work_ratios)}")

    for r in work_ratios:
        subdir = os.path.join(output_root, tag, f"{r:.1f}")
        os.makedirs(subdir, exist_ok=True)
        pred_path = os.path.join(subdir, "predictions.jsonl")
        mets_path = os.path.join(subdir, "metrics.json")
        lock_file = pred_path + ".lock"

        # Clear prediction file
        open(pred_path, "w", encoding="utf-8").close()

        # Split work
        nproc = max(1, min(processes, len(records)))
        chunks = split_list(records, nproc)

        mgr = Manager()
        prog = mgr.dict()
        worker_args = []
        # Force no injection for original model
        ratio_enc = "none" if is_original else ratio_encoding
        for i, ch in enumerate(chunks):
            prog[f"{i}_processed"] = 0
            prog[f"{i}_succ"] = 0
            prog[f"{i}_err"] = 0
            worker_args.append((
                ch, base_url, served_name, timeout, prog, i,
                pred_path, lock_file, model_type, ratio_enc, r,
                special_token_prefix, max_new_tokens, tokenizer_path
            ))

        t0 = time.time()
        with Pool(processes=nproc) as pool:
            r_async = pool.map_async(worker, worker_args)

            total = len(records)
            if no_tqdm:
                last_logged = -1
                while not r_async.ready():
                    cur = sum(prog.get(f"{i}_processed", 0) for i in range(len(chunks)))
                    cur = min(cur, total)
                    if cur != last_logged:
                        succ = sum(prog.get(f"{i}_succ", 0) for i in range(len(chunks)))
                        err  = sum(prog.get(f"{i}_err", 0) for i in range(len(chunks)))
                        elapsed = time.time() - t0
                        rate = cur / max(1e-6, elapsed)
                        print(f"\r[{tag} r={r:.1f}] {cur}/{total} (Succ:{succ} Err:{err} {rate:.1f}/s)", end="", flush=True)
                        last_logged = cur
                    time.sleep(progress_refresh)
                print()
                stats = r_async.get()
            else:
                with tqdm(total=total, desc=f"{tag} r={r:.1f}", dynamic_ncols=True, smoothing=0.01) as pbar:
                    while not r_async.ready():
                        cur = sum(prog.get(f"{i}_processed", 0) for i in range(len(chunks)))
                        cur = min(cur, total)
                        pbar.n = cur
                        succ = sum(prog.get(f"{i}_succ", 0) for i in range(len(chunks)))
                        err  = sum(prog.get(f"{i}_err", 0) for i in range(len(chunks)))
                        elapsed = time.time() - t0
                        rate = cur / max(1e-6, elapsed)
                        pbar.set_description(f"{tag} r={r:.1f} (Succ:{succ} Err:{err} {rate:.1f}/s)")
                        pbar.refresh()
                        time.sleep(progress_refresh)
                    # final align
                    cur = sum(prog.get(f"{i}_processed", 0) for i in range(len(chunks)))
                    pbar.n = min(cur, total)
                    pbar.refresh()
                stats = r_async.get()

        total_time = time.time() - t0
        mets = write_metrics(pred_path, mets_path, total_time,
                             engine_desc=f"vllm_http:{base_url}",
                             ratio_desc=f"{'orig' if is_original else f'{r:.1f}'}")
        print(f"[{tag} r={r:.1f}]  n={mets['n_samples']}  acc={mets['accuracy']*100:.2f}%  "
              f"avg_cot_len={mets['avg_cot_length']:.1f}  latency={mets['sample_latency']:.3f}s  -> {subdir}")

    return tag

# ============================ Main ============================

def main():
    ap = argparse.ArgumentParser(
        description="Evaluate multiple models via vLLM with per-ratio outputs (dataset-style ratio injection)."
    )
    ap.add_argument("--input_path", required=True, help="测试集 .jsonl/.json")
    ap.add_argument("--output_root", required=True, help="输出根目录；写入 <root>/<model_tag>/<ratio>/...")

    # Models & ports
    ap.add_argument("--models", required=True, help="逗号分隔模型标签，如: original,longformer,llmlingua2")
    ap.add_argument("--base_urls", required=True, help="逗号分隔 vLLM base_url，与 models 对齐，如 http://127.0.0.1:8000,http://127.0.0.1:8001")
    ap.add_argument("--served_names", default="", help="逗号分隔 served model names，对齐 models；留空则用标签本身")

    # Original model (no injection)
    ap.add_argument("--original_model", required=True, help="models 中的一个标签，作为原始模型，不注入压缩比")

    # Ratios for non-original models
    ap.add_argument("--ratios", default="0.9,0.8,0.7,0.6,0.5", help="非原始模型的压缩比列表")

    # Generation & parallelism
    ap.add_argument("--max_new_tokens", type=int, default=512)
    ap.add_argument("--processes", type=int, default=8, help="每个模型内部的数据并发进程数")
    ap.add_argument("--parallel_models", type=int, default=1, help="同时并发评测的模型个数（建议与你的 GPU 数相同）")
    ap.add_argument("--timeout", type=int, default=300)

    # Ratio injection (for non-original models)
    ap.add_argument("--model_type", choices=["qwen","llama3"], default="qwen")
    ap.add_argument("--ratio_encoding", choices=["numeric","special","none"], default="numeric",
                    help="numeric(Qwen用<|eot_id|>；Llama3用文本行)、special(如<comp80>)、none(不注入)")
    ap.add_argument("--special_token_prefix", default="comp", help="special 方案的前缀，形成 <comp90>")

    # Tokenizer path (only for counting COT tokens)
    ap.add_argument("--tokenizer_path", default="", help="可选；若提供，将用其统计 cot_length（tokens）")

    # Progress control
    ap.add_argument("--no_tqdm", action="store_true", help="禁用进度条（并发多个模型时建议开启以免覆盖）")
    ap.add_argument("--progress_refresh", type=float, default=0.5, help="进度刷新间隔（秒）")

    args = ap.parse_args()

    # Parse model lists
    model_tags = [x.strip() for x in args.models.split(",") if x.strip()]
    base_urls  = [x.strip() for x in args.base_urls.split(",") if x.strip()]
    served     = [x.strip() for x in args.served_names.split(",")] if args.served_names else []
    if served and len(served) != len(model_tags):
        print("[ERR] served_names 数量需与 models 对齐（或留空）"); sys.exit(2)
    if len(base_urls) != len(model_tags):
        print("[ERR] base_urls 数量需与 models 对齐"); sys.exit(2)
    served_names = served if served else model_tags

    # Ratios
    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]

    # Model-level parallel or serial
    tasks = list(zip(model_tags, base_urls, served_names))
    pm = max(1, min(args.parallel_models, len(tasks)))

    if pm == 1:
        for tag, base_url, served_name in tasks:
            eval_one_model(args.input_path, args.output_root,
                           tag, base_url, served_name,
                           args.original_model, ratios,
                           args.max_new_tokens, args.processes, args.timeout,
                           args.model_type, args.ratio_encoding, args.special_token_prefix,
                           args.tokenizer_path, args.no_tqdm, args.progress_refresh)
    else:
        with ProcessPoolExecutor(max_workers=pm) as ex:
            futs = []
            for tag, base_url, served_name in tasks:
                fut = ex.submit(
                    eval_one_model, args.input_path, args.output_root,
                    tag, base_url, served_name,
                    args.original_model, ratios,
                    args.max_new_tokens, args.processes, args.timeout,
                    args.model_type, args.ratio_encoding, args.special_token_prefix,
                    args.tokenizer_path, args.no_tqdm, args.progress_refresh
                )
                futs.append(fut)
            for fu in as_completed(futs):
                try:
                    print(f"[MODEL DONE] {fu.result()}")
                except Exception as e:
                    print(f"[MODEL FAILED] {e}")

    print("\nALL DONE.")

if __name__ == "__main__":
    main()
