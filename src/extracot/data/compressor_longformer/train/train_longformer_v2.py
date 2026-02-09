#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Longformer-large Question-Grounded CoT Compressor - DDP Training (+TB, Argmax & Top-k eval)

Launch (example)
torchrun --nproc_per_node=8 train_longformer_ddp.py \
  --data-path /path/to/your_exact_aligned.pt \
  --model-name allenai/longformer-large-4096 \
  --save-dir ./ckpt_longformer_large \
  --epochs 6 --lr 2e-5 --batch-size 1 \
  --grad-accum 16 --fp16 --use-global-attn \
  --eval-keep-ratios 0.1,0.2,0.3
"""

import os
import json
import math
import time
import random
import argparse
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import torch
import torch.nn as nn
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader, DistributedSampler
from torch.nn.utils.rnn import pad_sequence
from torch import amp
from torch.cuda.amp import GradScaler
from tqdm import tqdm

from transformers import (
    AutoTokenizer,
    AutoModelForTokenClassification,
    get_linear_schedule_with_warmup,
)

# ================== Utils ==================

def is_ddp():
    return dist.is_available() and dist.is_initialized()

def is_main():
    return (not is_ddp()) or dist.get_rank() == 0

def barrier():
    if is_ddp(): dist.barrier()

def set_seed(seed: int):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def to_device(x, device):
    return x.to(device, non_blocking=True) if torch.is_tensor(x) else x

def all_reduce_scalar(v: float, device) -> float:
    """reduce SUM over ranks, return python float"""
    t = torch.tensor([v], device=device, dtype=torch.float)
    if is_ddp(): dist.all_reduce(t, op=dist.ReduceOp.SUM)
    return float(t.item())

def reduce_tensor_sum(t: torch.Tensor):
    """inplace sum-reduce"""
    if is_ddp(): dist.all_reduce(t, op=dist.ReduceOp.SUM)
    return t

# ================== Dataset & Collate ==================

class ExactCOTDataset(Dataset):
    """
    items: list[dict] with fields:
      - input_ids: LongTensor[L]
      - labels:    LongTensor[L]  (question tokens = -100; CoT tokens = 0/1)
      - attention_mask: LongTensor[L] (optional)
      - global_attention_mask: LongTensor[L] (optional)
    """
    def __init__(self, items: List[Dict[str, Any]]):
        self.items = items
    def __len__(self): return len(self.items)
    def __getitem__(self, idx): return self.items[idx]

@dataclass
class CollateCfg:
    pad_token_id: int
    label_pad_id: int = -100

def make_collate_fn(cfg: CollateCfg):
    def ensure_1d_long(x):
        if isinstance(x, torch.Tensor): return x.long().view(-1)
        return torch.tensor(x, dtype=torch.long).view(-1)

    def collate(batch: List[Dict[str, Any]]) -> Dict[str, torch.Tensor]:
        ids  = [ensure_1d_long(b["input_ids"]) for b in batch]
        labs = [ensure_1d_long(b["labels"])    for b in batch]

        has_attn  = all("attention_mask" in b for b in batch)
        has_gattn = all("global_attention_mask" in b for b in batch)

        if has_attn:
            atts = [ensure_1d_long(b["attention_mask"]) for b in batch]
        else:
            atts = None

        if has_gattn:
            gatts = [ensure_1d_long(b["global_attention_mask"]) for b in batch]
        else:
            gatts = None

        input_ids = pad_sequence(ids,  batch_first=True, padding_value=cfg.pad_token_id)
        labels    = pad_sequence(labs, batch_first=True, padding_value=cfg.label_pad_id)

        if atts is not None:
            attention_mask = pad_sequence(atts, batch_first=True, padding_value=0)
        else:
            attention_mask = (input_ids != cfg.pad_token_id).long()

        if gatts is not None:
            global_attention_mask = pad_sequence(gatts, batch_first=True, padding_value=0)
        else:
            global_attention_mask = torch.zeros_like(attention_mask)

        return {
            "ids":    input_ids,
            "mask":   attention_mask,
            "gmask":  global_attention_mask,
            "targets": labels,
        }
    return collate

# ================== Loss ==================

class WeightedFocalLoss(nn.Module):
    """
    Weighted Focal Loss with ignore_index masking.
    """
    def __init__(self, class_weights: Optional[torch.Tensor] = None,
                 alpha: float = 1.0, gamma: float = 2.0, ignore_index: int = -100):
        super().__init__()
        self.class_weights = class_weights
        self.alpha = alpha
        self.gamma = gamma
        self.ignore_index = ignore_index

    def forward(self, logits: torch.Tensor, targets: torch.Tensor):
        # logits: [B,L,C] -> [N,C]; targets [B,L] -> [N]
        N, C = logits.numel() // logits.size(-1), logits.size(-1)
        logits = logits.view(-1, C)
        targets = targets.view(-1)

        valid = (targets != self.ignore_index)
        if valid.sum() == 0:
            return logits.new_zeros(())

        logits = logits[valid]
        targets = targets[valid]

        ce = nn.functional.cross_entropy(
            logits, targets, weight=self.class_weights, reduction="none"
        )
        pt = torch.exp(-ce)  # prob of true class
        focal = self.alpha * (1 - pt) ** self.gamma * ce
        return focal.mean()

# ================== Metrics ==================

@torch.no_grad()
def batch_confmat(pred: torch.Tensor, gold: torch.Tensor, mask: torch.Tensor, ignore_index=-100):
    """
    Return dict{total, correct, tp1, fp1, fn1} on valid positions.
    """
    valid = (mask == 1) & (gold != ignore_index)
    if valid.sum() == 0:
        return dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)

    pv = pred[valid]
    gv = gold[valid]
    total   = int(valid.sum())
    correct = int((pv == gv).sum())

    tp1 = int(((pv == 1) & (gv == 1)).sum())
    fp1 = int(((pv == 1) & (gv == 0)).sum())
    fn1 = int(((pv == 0) & (gv == 1)).sum())

    return dict(total=total, correct=correct, tp1=tp1, fp1=fp1, fn1=fn1)

def reduce_counts(c: Dict[str, int], device) -> Dict[str, int]:
    out = {}
    for k, v in c.items():
        t = torch.tensor([v], device=device, dtype=torch.long)
        reduce_tensor_sum(t)
        out[k] = int(t.item())
    return out

def counts_to_metrics(c: Dict[str, int]) -> Dict[str, float]:
    total   = max(1, c.get("total", 0))
    correct = c.get("correct", 0)
    tp1 = c.get("tp1", 0); fp1 = c.get("fp1", 0); fn1 = c.get("fn1", 0)

    acc  = correct / total
    p1   = tp1 / max(1, tp1 + fp1)
    r1   = tp1 / max(1, tp1 + fn1)
    f1_1 = 0.0 if (p1 + r1) == 0 else 2 * p1 * r1 / (p1 + r1)

    # class 0 from totals
    tn   = total - (tp1 + fp1 + fn1)  # TN for class 1
    p0   = tn / max(1, tn + fn1)      # prec(0) = TN / (TN + FN0) and FN0=FP1
    r0   = tn / max(1, tn + fp1)      # rec(0)  = TN / (TN + FP0) and FP0=FN1
    f1_0 = 0.0 if (p0 + r0) == 0 else 2 * p0 * r0 / (p0 + r0)

    macro_f1 = 0.5 * (f1_0 + f1_1)
    return dict(
        acc=acc,
        keep_precision=p1,   keep_recall=r1,   keep_f1=f1_1,
        delete_precision=p0, delete_recall=r0, delete_f1=f1_0,
        macro_f1=macro_f1,
    )

def logits_to_topk_preds(logits: torch.Tensor, labels: torch.Tensor, mask: torch.Tensor, ratio: float):
    """
    Top‑k per sample on valid positions -> binary preds with exactly k ones per sample.
    """
    keep_score = logits[..., 1]  # [B,L]
    B, L = keep_score.shape
    preds = torch.zeros_like(labels)
    valid = (mask == 1) & (labels != -100)
    for b in range(B):
        idx = torch.nonzero(valid[b], as_tuple=False).flatten()
        if idx.numel() == 0:
            continue
        k = max(1, int(math.ceil(ratio * idx.numel())))
        k = min(k, idx.numel())
        vals = keep_score[b, idx]
        topk = torch.topk(vals, k=k, largest=True, sorted=False).indices
        chosen = idx[topk]
        preds[b, chosen] = 1
    return preds

# ================== Train / Eval ==================

def grad_norm(model) -> float:
    total_norm = 0.0
    for p in model.parameters():
        if p.grad is not None:
            param_norm = p.grad.data.norm(2)
            total_norm += param_norm.item() ** 2
    return (total_norm ** 0.5)

def train_one_epoch(
    epoch: int,
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scheduler,
    scaler: GradScaler,
    criterion: nn.Module,
    device: torch.device,
    grad_accum: int,
    fp16: bool,
    writer,                    # TensorBoard writer (main rank only)
    log_interval: int = 50,
):
    model.train()
    running_loss = 0.0
    steps_in_loss = 0

    # streaming counts（用于进度条/可视化）
    local_counts = dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)

    pbar = tqdm(loader, disable=not is_main(), desc=f"Train {epoch+1}")
    global_step = epoch * len(loader)

    for step, batch in enumerate(pbar):
        ids   = to_device(batch["ids"],   device)
        mask  = to_device(batch["mask"],  device)
        gmask = to_device(batch["gmask"], device)
        gold  = to_device(batch["targets"], device)

        with amp.autocast('cuda', enabled=fp16):
            # 如果是 focal，用自定义 loss；否则直接用模型内置 CE（更快、数值稳定）
            if isinstance(criterion, WeightedFocalLoss):
                out    = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=None)
                logits = out.logits
                loss   = criterion(logits, gold) / grad_accum
            else:
                out    = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=gold)
                logits = out.logits
                loss   = out.loss / grad_accum

        if scaler is not None:
            scaler.scale(loss).backward()
        else:
            loss.backward()

        steps_in_loss += 1
        running_loss += float(loss.item())

        # 训练中滚动 argmax 指标（用于进度展示 & TB）
        with torch.no_grad():
            pred = torch.argmax(logits, dim=-1)
            c = batch_confmat(pred, gold, mask)
            for k in local_counts: local_counts[k] += c[k]

        if (step + 1) % grad_accum == 0:
            # 更新
            if scaler is not None:
                scaler.unscale_(optimizer)
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                scaler.step(optimizer)
                scaler.update()
            else:
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            if scheduler is not None:
                scheduler.step()

            # TB（step 级）
            if writer is not None:
                gn = grad_norm(model.module if hasattr(model, "module") else model)
                cur_lr = optimizer.param_groups[0]["lr"]
                avg_loss = running_loss / max(1, steps_in_loss)
                writer.add_scalar("train/batch_loss", avg_loss, global_step + step + 1)
                writer.add_scalar("train/lr", cur_lr, global_step + step + 1)
                writer.add_scalar("train/grad_norm", gn, global_step + step + 1)

            # 重置步内 loss 计数（显示更平滑）
            running_loss = 0.0
            steps_in_loss = 0

        # 进度条显示局部指标（本 rank 即可）
        if local_counts["total"] > 0:
            m_local = counts_to_metrics(local_counts)
            pbar.set_postfix({
                "loss": f"{(running_loss/max(1,steps_in_loss)):.4f}",
                "acc":  f"{m_local['acc']:.3f}",
                "mf1":  f"{m_local['macro_f1']:.3f}",
                "lr":   f"{optimizer.param_groups[0]['lr']:.2e}"
            })

        # 周期性把局部指标写入 TB（注意：未聚合，仅用于可视化趋势）
        if writer is not None and (step + 1) % log_interval == 0:
            m_local = counts_to_metrics(local_counts if local_counts["total"] > 0 else dict(total=1, correct=0, tp1=0, fp1=0, fn1=0))
            writer.add_scalar("train/argmax_acc_stream",    m_local["acc"],      global_step + step + 1)
            writer.add_scalar("train/argmax_macroF1_stream",m_local["macro_f1"], global_step + step + 1)
            writer.add_scalar("train/keep_f1_stream",       m_local["keep_f1"],  global_step + step + 1)
            writer.add_scalar("train/delete_f1_stream",     m_local["delete_f1"],global_step + step + 1)

    # 训练 epoch 结束：聚合（所有 rank）
    device = ids.device
    red = reduce_counts(local_counts, device)
    met = counts_to_metrics(red)
    return met  # 返回训练期（聚合后）的 argmax 指标


@torch.no_grad()
def evaluate_argmax(model, loader, device, fp16: bool):
    model.eval()
    local_counts = dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)
    total_loss = 0.0
    steps = 0

    for batch in loader:
        ids   = to_device(batch["ids"],   device)
        mask  = to_device(batch["mask"],  device)
        gmask = to_device(batch["gmask"], device)
        gold  = to_device(batch["targets"], device)

        with amp.autocast('cuda', enabled=fp16):
            out    = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=gold)
            logits = out.logits
            loss   = out.loss

        total_loss += float(loss.item()); steps += 1
        pred = torch.argmax(logits, dim=-1)
        c = batch_confmat(pred, gold, mask)
        for k in local_counts: local_counts[k] += c[k]

    # reduce
    device = ids.device
    red = reduce_counts(local_counts, device)
    met = counts_to_metrics(red)

    # 平均 loss 再做一次 reduce+平均
    avg_loss = total_loss / max(1, steps)
    avg_loss = all_reduce_scalar(avg_loss, device) / (dist.get_world_size() if is_ddp() else 1)
    return avg_loss, met


@torch.no_grad()
def evaluate_topk(model, loader, device, fp16: bool, ratios: List[float]):
    model.eval()
    # 为每个 ratio 维护计数
    agg = {r: dict(total=0, correct=0, tp1=0, fp1=0, fn1=0) for r in ratios}

    for batch in loader:
        ids   = to_device(batch["ids"],   device)
        mask  = to_device(batch["mask"],  device)
        gmask = to_device(batch["gmask"], device)
        gold  = to_device(batch["targets"], device)

        with amp.autocast('cuda', enabled=fp16):
            out    = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=None)
            logits = out.logits

        for r in ratios:
            preds = logits_to_topk_preds(logits, gold, mask, r)
            c = batch_confmat(preds, gold, mask)
            for k in agg[r]: agg[r][k] += c[k]

    # reduce 并转指标
    out = {}
    for r in ratios:
        red = reduce_counts(agg[r], device)
        out[r] = counts_to_metrics(red)
    return out

# ================== Main ==================

def parse_args():
    ap = argparse.ArgumentParser("Longformer CoT Compressor (DDP + TB + Argmax & Top‑k eval)")
    # data & model
    ap.add_argument("--data-path", required=True, type=str, help=".pt file produced by exact aligner (list[dict])")
    ap.add_argument("--model-name", default="allenai/longformer-large-4096", type=str)
    ap.add_argument("--save-dir",  default="./ckpt_longformer_large", type=str)

    # train
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--warmup-ratio", type=float, default=0.06)
    ap.add_argument("--max-grad-norm", type=float, default=1.0)

    # loss
    ap.add_argument("--use-focal", action="store_true", default=False)
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--gamma", type=float, default=2.0)

    # optimization
    ap.add_argument("--fp16", action="store_true", default=False)
    ap.add_argument("--grad-chkpt", action="store_true", default=True)

    # attention
    ap.add_argument("--use-global-attn", action="store_true", default=True)

    # eval
    ap.add_argument("--eval-keep-ratios", type=str, default="0.1,0.2,0.3",
                    help='comma separated ratios, e.g. "0.1,0.2,0.3"')

    # runtime
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--log-interval", type=int, default=50)  # steps
    ap.add_argument("--early-stop", type=int, default=3)     # patience epochs

    # resume
    ap.add_argument("--resume", type=str, default=None)

    return ap.parse_args()

def main():
    args = parse_args()

    # ----- DDP init -----
    if "LOCAL_RANK" in os.environ:
        local_rank = int(os.environ["LOCAL_RANK"])
        torch.cuda.set_device(local_rank)
        dist.init_process_group(backend="nccl", init_method="env://")
        device = torch.device(f"cuda:{local_rank}")
    else:
        local_rank = 0
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if is_main():
        os.makedirs(args.save_dir, exist_ok=True)
        print(json.dumps(vars(args), indent=2))

    set_seed(args.seed + (dist.get_rank() if is_ddp() else 0))
    barrier()

    # ----- load data -----
    if is_main():
        print(f"[INFO] Loading data from: {args.data_path}")
    data = torch.load(args.data_path, map_location="cpu")
    assert isinstance(data, list) and len(data) > 0, "Expect list[dict] from exact aligner."

    # split (80/20)
    N = len(data)
    idx = list(range(N))
    random.Random(2025).shuffle(idx)
    cut = int(0.8 * N)
    tr_items = [data[i] for i in idx[:cut]]
    ev_items = [data[i] for i in idx[cut:]]

    train_ds = ExactCOTDataset(tr_items)
    eval_ds  = ExactCOTDataset(ev_items)

    # ----- tokenizer / pad id -----
    tokenizer = AutoTokenizer.from_pretrained(args.model_name, use_fast=True)
    if tokenizer.pad_token_id is None:
        tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    pad_id = tokenizer.pad_token_id

    collate = make_collate_fn(CollateCfg(pad_token_id=pad_id, label_pad_id=-100))

    # ----- samplers & loaders -----
    if is_ddp():
        train_sampler = DistributedSampler(train_ds, shuffle=True,  drop_last=False)
        eval_sampler  = DistributedSampler(eval_ds,  shuffle=False, drop_last=False)
    else:
        train_sampler = None
        eval_sampler  = None

    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size,
        sampler=train_sampler, shuffle=(train_sampler is None),
        num_workers=args.num_workers, pin_memory=True, drop_last=False,
        collate_fn=collate
    )
    eval_loader = DataLoader(
        eval_ds, batch_size=args.batch_size,
        sampler=eval_sampler, shuffle=False,
        num_workers=args.num_workers, pin_memory=True, drop_last=False,
        collate_fn=collate
    )

    # ----- model -----
    model = AutoModelForTokenClassification.from_pretrained(
        args.model_name, num_labels=2, ignore_mismatched_sizes=True
    )
    if args.grad_chkpt:
        try:
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        except TypeError:
            model.gradient_checkpointing_enable()

    model.to(device)
    if is_ddp():
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], output_device=local_rank, find_unused_parameters=False
        )

    # ----- optim / sched -----
    steps_per_epoch = math.ceil(len(train_loader) / max(1, args.grad_accum))
    total_steps = steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay, eps=1e-8
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps
    )
    scaler = GradScaler(enabled=args.fp16)

    # ----- loss -----
    criterion = WeightedFocalLoss(alpha=args.alpha, gamma=args.gamma) if args.use_focal \
                else nn.CrossEntropyLoss(ignore_index=-100)

    # ----- resume -----
    start_epoch = 0
    best_macro_f1 = 0.0
    if args.resume and os.path.exists(args.resume):
        ckpt = torch.load(args.resume, map_location="cpu")
        (model.module if hasattr(model, "module") else model).load_state_dict(ckpt["model_state_dict"])
        optimizer.load_state_dict(ckpt["optimizer_state_dict"])
        if ckpt.get("scheduler_state_dict"): scheduler.load_state_dict(ckpt["scheduler_state_dict"])
        start_epoch = ckpt.get("epoch", 0)
        best_macro_f1 = ckpt.get("best_f1", 0.0)
        if is_main(): print(f"[Resume] epoch={start_epoch}, best_macro_f1={best_macro_f1:.4f}")

    # ----- TB -----
    writer = None
    if is_main():
        from torch.utils.tensorboard import SummaryWriter
        tb_dir = os.path.join(args.save_dir, "tb_logs")
        os.makedirs(tb_dir, exist_ok=True)
        writer = SummaryWriter(tb_dir)
        print(f"[TensorBoard] run: tensorboard --logdir {tb_dir}")

    # eval ratios
    ratios = [float(x) for x in args.eval_keep_ratios.split(",") if x.strip()]

    # ----- loop -----
    patience = 0
    for epoch in range(start_epoch, args.epochs):
        if is_ddp() and isinstance(train_loader.sampler, DistributedSampler):
            train_loader.sampler.set_epoch(epoch)

        t0 = time.time()
        train_met = train_one_epoch(
            epoch, model, train_loader, optimizer, scheduler, scaler, criterion,
            device, grad_accum=args.grad_accum, fp16=args.fp16,
            writer=writer, log_interval=args.log_interval
        )
        t1 = time.time()

        eval_loss, eval_argmax = evaluate_argmax(model, eval_loader, device, fp16=args.fp16)
        topk = evaluate_topk(model, eval_loader, device, fp16=args.fp16, ratios=ratios)
        t2 = time.time()

        if is_main():
            # —— print —— #
            print(f"\nEpoch {epoch+1}/{args.epochs}")
            print(f"  Train(argmax): acc={train_met['acc']:.4f} macroF1={train_met['macro_f1']:.4f} "
                  f"| keep_F1={train_met['keep_f1']:.4f} (P={train_met['keep_precision']:.4f},R={train_met['keep_recall']:.4f}) "
                  f"| del_F1={train_met['delete_f1']:.4f} (P={train_met['delete_precision']:.4f},R={train_met['delete_recall']:.4f}) "
                  f"| time={t1-t0:.1f}s")
            print(f"  Eval (argmax): loss={eval_loss:.4f} acc={eval_argmax['acc']:.4f} macroF1={eval_argmax['macro_f1']:.4f} "
                  f"| keep_F1={eval_argmax['keep_f1']:.4f} (P={eval_argmax['keep_precision']:.4f},R={eval_argmax['keep_recall']:.4f}) "
                  f"| del_F1={eval_argmax['delete_f1']:.4f} (P={eval_argmax['delete_precision']:.4f},R={eval_argmax['delete_recall']:.4f}) "
                  f"| time={t2-t1:.1f}s")

            for r, m in topk.items():
                print(f"  Eval(top‑k@{int(r*100)}%): acc={m['acc']:.4f} macroF1={m['macro_f1']:.4f} "
                      f"| keep_F1={m['keep_f1']:.4f} del_F1={m['delete_f1']:.4f}")

            # —— TB —— #
            # train epoch
            writer.add_scalar("epoch/train_argmax_acc",      train_met["acc"], epoch+1)
            writer.add_scalar("epoch/train_argmax_macroF1",  train_met["macro_f1"], epoch+1)
            writer.add_scalar("epoch/train_keep_F1",         train_met["keep_f1"], epoch+1)
            writer.add_scalar("epoch/train_delete_F1",       train_met["delete_f1"], epoch+1)
            writer.add_scalar("epoch/time_train_sec",        t1-t0, epoch+1)

            # eval argmax
            writer.add_scalar("epoch/eval_argmax_loss",      eval_loss, epoch+1)
            writer.add_scalar("epoch/eval_argmax_acc",       eval_argmax["acc"], epoch+1)
            writer.add_scalar("epoch/eval_argmax_macroF1",   eval_argmax["macro_f1"], epoch+1)
            writer.add_scalar("epoch/eval_keep_F1",          eval_argmax["keep_f1"], epoch+1)
            writer.add_scalar("epoch/eval_delete_F1",        eval_argmax["delete_f1"], epoch+1)
            writer.add_scalar("epoch/time_eval_sec",         t2-t1, epoch+1)

            # eval top‑k
            for r, m in topk.items():
                tag = f"epoch/topk_{int(r*100)}"
                writer.add_scalar(f"{tag}/acc",      m["acc"], epoch+1)
                writer.add_scalar(f"{tag}/macroF1",  m["macro_f1"], epoch+1)
                writer.add_scalar(f"{tag}/keep_F1",  m["keep_f1"], epoch+1)
                writer.add_scalar(f"{tag}/delete_F1",m["delete_f1"], epoch+1)
            writer.flush()

            # —— save latest —— #
            latest = {
                "epoch": epoch+1,
                "model_state_dict": (model.module if hasattr(model, "module") else model).state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict(),
                "best_f1": best_macro_f1,
                "args": vars(args)
            }
            torch.save(latest, os.path.join(args.save_dir, "latest_checkpoint.pth"))

            # —— save best（以 argmax macro‑F1 为准，你也可以改成某个 top‑k 比率）—— #
            if eval_argmax["macro_f1"] > best_macro_f1:
                best_macro_f1 = eval_argmax["macro_f1"]
                torch.save(latest, os.path.join(args.save_dir, "best_checkpoint.pth"))
                (model.module if hasattr(model, "module") else model).save_pretrained(args.save_dir)
                tokenizer.save_pretrained(args.save_dir)
                with open(os.path.join(args.save_dir, "training_config.json"), "w") as f:
                    json.dump({
                        "best_macro_f1_argmax": best_macro_f1,
                        "epoch": epoch+1,
                        "args": vars(args)
                    }, f, indent=2)
                print("  ✅ New best model saved.")
                patience = 0
            else:
                patience += 1
                print(f"  (no improv; patience {patience}/{args.early_stop})")

            if patience >= args.early_stop:
                print("Early stopping triggered.")
                break

    if is_main() and writer is not None:
        writer.close()
        print(f"\n[Done] Best argmax macro‑F1 = {best_macro_f1:.4f}")

    barrier()

if __name__ == "__main__":
    main()
