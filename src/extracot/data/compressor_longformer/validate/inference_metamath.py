#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys
import os
import time
import argparse
import requests
from multiprocessing import Pool, Manager
from tqdm import tqdm
from typing import List, Dict, Any, Iterable, Optional
from filelock import FileLock
import hashlib
import random

# ============================ 数据加载 + 抽样 ============================

def _iter_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, 'r', encoding='utf-8') as f:
        for ln, line in enumerate(f, 1):
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
    with open(path, 'r', encoding='utf-8') as f:
        while True:
            ch = f.read(1)
            if ch == '':
                return None
            if ch.isspace():
                continue
            if ch == '[':
                return True
            if ch == '{':
                return False
            return None

def _iter_json_array_slow(path: str) -> Iterable[Dict[str, Any]]:
    with open(path, 'r', encoding='utf-8') as f:
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
        yield from _iter_json_array_slow(path)
    elif kind is False or kind is None:
        yield from _iter_jsonl(path)

def reservoir_sample(iterable, k: int, seed: int = 42) -> List[Dict[str, Any]]:
    random.seed(seed)
    sample: List[Dict[str, Any]] = []
    n = 0
    for obj in iterable:
        if not isinstance(obj, dict):
            continue
        if len(sample) < k:
            sample.append(obj)
        else:
            j = random.randint(0, n)
            if j < k:
                sample[j] = obj
        n += 1
    return sample

# ============================ vLLM 推理客户端 ============================

class VLLMInference:
    def __init__(self, base_url: str, model_name: str = "qwen3-8b-instruct", timeout: int = 300,
                 model_type: str = "qwen", compression_ratio: float = 1.0):
        """
        base_url: e.g. http://localhost:8000 或已带 /v1
        """
        base = base_url.rstrip('/')
        self.base_url = base if base.endswith('/v1') else (base + '/v1')
        self.model_name = model_name
        self.timeout = timeout
        self.chat_url = f"{self.base_url}/chat/completions"
        self.model_type = model_type
        self.compression_ratio = float(compression_ratio)

    def _build_messages_from_record(self, rec: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        参考你的“提问方式”：
        - system: You are a helpful assistant.
        - user: "Please reason step by step, and put your final answer within \\boxed{}.\n{query}\n[原始问题/题目类型/参考答案]"
        - 若 compression_ratio < 1.0，则把 ratio 作为额外提示行一并放入（不注入模型私有控制 token）
        """
        q = (rec.get("query") or "").strip()
        oq = (rec.get("original_question") or "").strip()
        typ = (rec.get("type") or "").strip()
        ans = rec.get("answer")

        header = "Please reason step by step, and put your final answer within \\boxed{}."
        user_lines = [header, oq]

        # if oq:
        #     user_lines.append(f"原始问题：{oq}")
        # if typ:
        #     user_lines.append(f"题目类型：{typ}")
        # if ans not in (None, ""):
        #     user_lines.append(f"参考答案（供校对，不一定正确）：{ans}")
        # 压缩比提示（仅作为自然语言提示，保持和 OpenAI chat 兼容）
        if self.compression_ratio < 1.0:
            user_lines.append(f"compression_ratio: {self.compression_ratio:.2f}")

        user_content = "\n".join([s for s in user_lines if s])

        # system 固定为你的模板
        system_msg = "You are a helpful assistant."
        # llama3 / qwen 在 OpenAI 兼容接口下都按 role-based messages 交给服务端模板去包裹
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_content}
        ]
        return messages

    def inference_single(self, data: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if "request_body" in data:
                request_body = data["request_body"]
                messages = request_body.get("messages") or self._build_messages_from_record(data)
                system_message = request_body.get("system")
                if system_message:
                    messages = [{"role": "system", "content": system_message}] + messages
                temperature = request_body.get("temperature", 0.0)
                top_p = request_body.get("top_p", 1.0)
                max_tokens = request_body.get("max_tokens", 4096)
            else:
                messages = self._build_messages_from_record(data)
                temperature = 0.0
                top_p = 1.0
                max_tokens = 4096

            payload = {
                "model": self.model_name,
                "messages": messages,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
                "stream": False
            }
            headers = {"Content-Type": "application/json"}

            resp = requests.post(self.chat_url, json=payload, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                resp_json = resp.json()
                text = resp_json.get("choices", [{}])[0].get("message", {}).get("content", "")
                return {
                    "id": data.get("id"),
                    "response": text,
                    "status": "success",
                    "raw_response": resp_json,
                    "messages": messages
                }
            else:
                return {
                    "id": data.get("id"),
                    "response": None,
                    "status": "error",
                    "error": f"HTTP {resp.status_code}: {resp.text}"
                }
        except Exception as e:
            return {
                "id": data.get("id"),
                "response": None,
                "status": "error",
                "error": str(e)
            }

# ============================ 写盘（加锁） ============================

def save_result_to_file(result: Dict[str, Any], output_file: str, lock_file: str):
    try:
        with FileLock(lock_file):
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
                f.flush()
                os.fsync(f.fileno())
    except Exception as e:
        print(f"Error saving result: {e}")

# ============================ Worker 进程 ============================

def _stable_id_from_fields(rec: Dict[str, Any]) -> str:
    key = json.dumps({
        "type": rec.get("type"),
        "query": rec.get("query"),
        "original_question": rec.get("original_question"),
        "answer": rec.get("answer")
    }, ensure_ascii=False, sort_keys=True)
    import hashlib
    return hashlib.sha1(key.encode('utf-8')).hexdigest()

def worker_function(args):
    data_chunk, base_url, model_name, timeout, progress_dict, worker_id, output_file, lock_file, model_type, compression_ratio = args
    client = VLLMInference(base_url, model_name, timeout, model_type=model_type, compression_ratio=compression_ratio)
    succ, err = 0, 0

    for i, data in enumerate(data_chunk):
        try:
            if "id" not in data or not data["id"]:
                data["id"] = _stable_id_from_fields(data)

            res = client.inference_single(data)

            merged = dict(data)
            if res.get("status") == "success":
                merged["model_output"] = res.get("response")
                merged["status"] = "success"
            else:
                merged["model_output"] = None
                merged["status"] = "error"
                merged["error"] = res.get("error")

            # 也把本次 messages（提问内容）落盘，便于回溯
            if "messages" not in merged and "messages" in res:
                merged["messages"] = res["messages"]

            save_result_to_file(merged, output_file, lock_file)

            if merged["status"] == "success":
                succ += 1
            else:
                err += 1

            progress_dict[f'{worker_id}_processed'] = i + 1
            progress_dict[f'{worker_id}_success'] = succ
            progress_dict[f'{worker_id}_error'] = err

        except Exception as e:
            merged = dict(data)
            merged["status"] = "error"
            merged["error"] = f"Worker exception: {e}"
            save_result_to_file(merged, output_file, lock_file)
            err += 1
            progress_dict[f'{worker_id}_processed'] = i + 1
            progress_dict[f'{worker_id}_success'] = succ
            progress_dict[f'{worker_id}_error'] = err

    return {"worker_id": worker_id, "total_processed": len(data_chunk), "success_count": succ, "error_count": err}

# ============================ 其他工具 ============================

def check_vllm_server(base_url: str) -> bool:
    base = base_url.rstrip('/')
    url = base if base.endswith('/v1') else (base + '/v1')
    try:
        resp = requests.get(f"{url}/models", timeout=10)
        return resp.status_code == 200
    except:
        return False

def get_completed_ids(output_file: str) -> set:
    done = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                        if obj.get("status") == "success" and obj.get("id"):
                            done.add(obj["id"])
                    except:
                        pass
        except Exception as e:
            print(f"Warning: error reading output file: {e}")
    return done

def split_data(data: List[Dict[str, Any]], num_processes: int) -> List[List[Dict[str, Any]]]:
    n = len(data)
    if num_processes <= 1:
        return [data]
    size = (n + num_processes - 1) // num_processes
    return [data[i:i+size] for i in range(0, n, size)]

# ============================ 主流程 ============================

def main():
    parser = argparse.ArgumentParser(description="Parallel vLLM inference on 10k subset with Qwen/Llama3 prompt style")
    parser.add_argument("--input", "-i", required=True, help="输入数据（JSONL 或 JSON 数组）")
    parser.add_argument("--output", "-o", required=True, help="输出 JSONL")
    parser.add_argument("--base_url", "-u", required=True, help="vLLM 地址，如 http://localhost:8000 或已含 /v1")
    parser.add_argument("--model", "-m", default="qwen3-8b-instruct", help="vLLM --served-model-name")
    parser.add_argument("--processes", "-p", type=int, default=8, help="并行进程数")
    parser.add_argument("--timeout", "-t", type=int, default=300, help="HTTP 超时（秒）")
    parser.add_argument("--resume", "-r", action="store_true", help="断点续跑")
    parser.add_argument("--sample_k", type=int, default=10000, help="抽样条数（默认10k）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--model_type", choices=["qwen", "llama3"], default="qwen", help="决定提问风格（文本提示相同，role-based messages）")
    parser.add_argument("--compression_ratio", type=float, default=1.0, help="<1.0 时在 user 提示中附带 ratio 提示")
    args = parser.parse_args()

    print(f"Checking vLLM server at {args.base_url} ...")
    if not check_vllm_server(args.base_url):
        print(f"Error: cannot connect to vLLM server at {args.base_url}")
        sys.exit(1)
    print("✓ vLLM server is accessible")

    print(f"Streaming & sampling {args.sample_k} from {args.input} ...")
    sampled = reservoir_sample(stream_records_any(args.input), k=args.sample_k, seed=args.seed)
    print(f"Sampled {len(sampled)} records")

    if not sampled:
        print("No records sampled. Exit.")
        sys.exit(1)

    if args.resume and os.path.exists(args.output):
        done = get_completed_ids(args.output)
        before = len(sampled)
        sampled = [x for x in sampled if (x.get("id") or _stable_id_from_fields(x)) not in done]
        print(f"Resume: filtered {before - len(sampled)} done, remain {len(sampled)}")

    if not args.resume and os.path.exists(args.output):
        open(args.output, 'w').close()

    # 确保每条有 id
    for rec in sampled:
        if "id" not in rec or not rec["id"]:
            rec["id"] = _stable_id_from_fields(rec)

    num_proc = max(1, min(args.processes, len(sampled)))
    chunks = split_data(sampled, num_proc)
    print(f"Starting with {num_proc} processes; items per process: {[len(c) for c in chunks]}")

    manager = Manager()
    progress = manager.dict()
    lock_file = args.output + ".lock"

    worker_args = []
    for i, chunk in enumerate(chunks):
        progress[f'{i}_processed'] = 0
        progress[f'{i}_success'] = 0
        progress[f'{i}_error'] = 0
        worker_args.append((chunk, args.base_url, args.model, args.timeout, progress, i, args.output, lock_file, args.model_type, args.compression_ratio))

    start = time.time()
    with Pool(processes=num_proc) as pool:
        result_async = pool.map_async(worker_function, worker_args)

        with tqdm(total=len(sampled), desc="Processing") as pbar:
            last_total = 0
            while not result_async.ready():
                cur_total = sum(progress.get(f'{i}_processed', 0) for i in range(num_proc))
                cur_succ = sum(progress.get(f'{i}_success', 0) for i in range(num_proc))
                cur_err  = sum(progress.get(f'{i}_error', 0) for i in range(num_proc))
                pbar.update(cur_total - last_total)
                last_total = cur_total
                elapsed = max(1e-6, time.time() - start)
                rate = cur_total / elapsed
                pbar.set_description(f"Processing (Success: {cur_succ}, Error: {cur_err}, Rate: {rate:.1f}/s)")
                time.sleep(1)
            cur_total = sum(progress.get(f'{i}_processed', 0) for i in range(num_proc))
            pbar.update(cur_total - last_total)

        stats = result_async.get()

    elapsed = time.time() - start
    total_success = sum(s['success_count'] for s in stats)
    total_error   = sum(s['error_count'] for s in stats)
    total = total_success + total_error
    print("\n" + "="*60)
    print("Batch inference completed!")
    print(f"Total time: {elapsed:.2f}s")
    print(f"Processed: {total}  | Success: {total_success}  | Error: {total_error}")
    if total:
        print(f"Success rate: {100.0*total_success/total:.2f}%")
        print(f"Avg time/item: {elapsed/total:.3f}s  | Throughput: {total/elapsed:.2f} it/s")
    print(f"Results saved to: {args.output}")

    try:
        lf = lock_file
        if os.path.exists(lf): os.remove(lf)
    except:
        pass

    print("\nWorker stats:")
    for s in stats:
        print(f"  Worker {s['worker_id']}: {s['success_count']}/{s['total_processed']}")

if __name__ == "__main__":
    main()
