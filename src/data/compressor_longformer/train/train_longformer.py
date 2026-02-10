#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Longformer-large Question-Grounded CoT Compressor - DDP Training

Features
- DDP multi-GPU training via torch.distributed / torchrun
- Dynamic padding collate_fn (fixes "Tensor + list" error)
- AMP mixed precision, gradient accumulation, grad clipping
- Gradient checkpointing (optional)
- Optional Weighted Focal Loss with proper ignore_index masking
- Class-weight caching, early stopping, resume/best checkpoint saving
- Global-attention support (uses sample['global_attention_mask'] when provided)
- Proper distributed metric aggregation (accuracy / precision / recall / F1)

Data (.pt)
- torch.save(list_of_dicts) where each dict has:
  {
    "id": any,
    "input_ids": LongTensor[L],
    "attention_mask": LongTensor[L],          # if absent, we derive from padding
    "global_attention_mask": LongTensor[L],   # optional
    "labels": LongTensor[L],                  # -100 for question, 0/1 for CoT
    ... (other stats ignored)
  }

Launch (example)
torchrun --nproc_per_node=8 train_longformer_ddp.py \
  --data-path /path/to/your_exact_aligned.pt \
  --model-name allenai/longformer-large-4096 \
  --save-dir ./ckpt_longformer_large \
  --epochs 6 --lr 2e-5 --batch-size 1 \
  --grad-accum 16 --fp16 --use-focal --use-global-attn
"""

import os
import json
import math
import time
import random
import argparse
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

import torch
import torch.nn as nn
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader, DistributedSampler
from torch.nn.utils.rnn import pad_sequence

from transformers import (
    AutoTokenizer,
    AutoModelForTokenClassification,
    get_linear_schedule_with_warmup,
)

from sklearn.metrics import precision_recall_fscore_support

# ===== Utilities =====

def is_main_process() -> bool:
    return (not dist.is_available()) or (not dist.is_initialized()) or (dist.get_rank() == 0)

def barrier():
    if dist.is_available() and dist.is_initialized():
        dist.barrier()

def set_seed(seed: int):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def reduce_scalar(v: torch.Tensor, op=dist.ReduceOp.SUM) -> float:
    if not (dist.is_available() and dist.is_initialized()):
        return float(v.item())
    rt = v.clone().detach()
    dist.all_reduce(rt, op=op)
    return float(rt.item())

def to_device(x, device):
    if isinstance(x, torch.Tensor):
        return x.to(device, non_blocking=True)
    return x

# ===== Dataset & Collate =====

class ExactCOTDataset(Dataset):
    """
    Reads the torch-saved list of dict samples (exact token-level alignment).
    """
    def __init__(self, items: List[Dict[str, Any]]):
        self.items = items
    def __len__(self):
        return len(self.items)
    def __getitem__(self, idx):
        return self.items[idx]

@dataclass
class CollateCfg:
    pad_token_id: int
    label_pad_id: int = -100

def make_collate_fn(cfg: CollateCfg):
    """
    Batch of variable-length tensors -> padded batch tensors
    Fields handled: input_ids, attention_mask (optional), global_attention_mask (optional), labels
    """
    def f(batch: List[Dict[str, Any]]) -> Dict[str, torch.Tensor]:
        # ensure tensors and flatten to 1D
        def ensure_1d_tensor(x) -> torch.Tensor:
            if isinstance(x, torch.Tensor):
                return x.long().view(-1)
            # list/tuple
            return torch.tensor(x, dtype=torch.long).view(-1)

        input_ids_list = [ensure_1d_tensor(ex["input_ids"]) for ex in batch]
        labels_list    = [ensure_1d_tensor(ex["labels"])     for ex in batch]

        # attention_mask: if provided per sample, we will pad it; otherwise derive from padded ids
        attn_list = []
        has_attn = all(("attention_mask" in ex) for ex in batch)
        if has_attn:
            attn_list = [ensure_1d_tensor(ex["attention_mask"]) for ex in batch]

        # global attention (optional)
        gattn_list = []
        has_gattn = all(("global_attention_mask" in ex) for ex in batch)
        if has_gattn:
            gattn_list = [ensure_1d_tensor(ex["global_attention_mask"]) for ex in batch]

        # pad
        input_ids = pad_sequence(input_ids_list, batch_first=True, padding_value=cfg.pad_token_id)
        labels    = pad_sequence(labels_list,    batch_first=True, padding_value=cfg.label_pad_id)

        if has_attn:
            attention_mask = pad_sequence(attn_list, batch_first=True, padding_value=0)
        else:
            attention_mask = (input_ids != cfg.pad_token_id).long()

        if has_gattn:
            global_attention_mask = pad_sequence(gattn_list, batch_first=True, padding_value=0)
        else:
            # default no global attention
            global_attention_mask = torch.zeros_like(attention_mask)

        return {
            "ids":    input_ids,
            "mask":   attention_mask,
            "gmask":  global_attention_mask,
            "targets": labels,
        }
    return f

# ===== Loss =====

class WeightedFocalLoss(nn.Module):
    """
    Weighted Focal Loss with proper ignore_index masking.
    """
    def __init__(self, class_weights: Optional[torch.Tensor] = None, alpha: float = 1.0, gamma: float = 2.0, ignore_index: int = -100):
        super().__init__()
        self.class_weights = class_weights
        self.alpha = alpha
        self.gamma = gamma
        self.ignore_index = ignore_index

    def forward(self, logits: torch.Tensor, targets: torch.Tensor):
        # logits: [B, L, C]; targets: [B, L]
        B, L, C = logits.shape
        logits = logits.view(-1, C)
        targets = targets.view(-1)

        valid_mask = (targets != self.ignore_index)
        if valid_mask.sum() == 0:
            return logits.new_zeros(()).mean()

        logits = logits[valid_mask]
        targets = targets[valid_mask]

        ce = nn.functional.cross_entropy(
            logits, targets, weight=self.class_weights, reduction='none'
        )
        pt = torch.exp(-ce)                  # pt = softmax prob of the true class
        focal = self.alpha * (1 - pt) ** self.gamma * ce
        return focal.mean()

# ===== Metrics (distributed, streaming) =====

@torch.no_grad()
def batch_confmat(pred: torch.Tensor, gold: torch.Tensor, mask: torch.Tensor, ignore_index: int = -100) -> Dict[str, int]:
    """
    Compute confusion-like counts for binary classes at valid positions:
      valid = (mask == 1) & (gold != -100)
    Returns dict of counts we can reduce across ranks:
      total, correct, tp1, fp1, fn1  (for class=1; class=0 is derivable if needed)
    """
    # pred,gold,mask shape: [B, L]
    valid = (mask == 1) & (gold != ignore_index)
    if valid.sum() == 0:
        return dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)

    pv = pred[valid]
    gv = gold[valid]
    total  = int(valid.sum().item())
    correct= int((pv == gv).sum().item())

    tp1 = int(((pv == 1) & (gv == 1)).sum().item())
    fp1 = int(((pv == 1) & (gv == 0)).sum().item())
    fn1 = int(((pv == 0) & (gv == 1)).sum().item())

    return dict(total=total, correct=correct, tp1=tp1, fp1=fp1, fn1=fn1)

def reduce_counts(c: Dict[str, int]) -> Dict[str, int]:
    out = {}
    for k,v in c.items():
        t = torch.tensor([v], dtype=torch.long, device='cuda' if torch.cuda.is_available() else 'cpu')
        vv = reduce_scalar(t, op=dist.ReduceOp.SUM)
        out[k] = int(vv)
    return out

def counts_to_metrics(c: Dict[str, int]) -> Dict[str, float]:
    total = max(1, c.get('total', 0))
    correct = c.get('correct', 0)
    tp1 = c.get('tp1', 0); fp1 = c.get('fp1', 0); fn1 = c.get('fn1', 0)

    acc = correct / total
    prec1 = tp1 / max(1, (tp1 + fp1))
    rec1  = tp1 / max(1, (tp1 + fn1))
    f1_1  = 0.0 if (prec1 + rec1) == 0 else 2 * prec1 * rec1 / (prec1 + rec1)

    # For binary, compute class-0 similarly from totals
    # tp0: pred=0, gold=0
    tn = (total - (tp1 + fp1 + fn1))  # true negatives for class 1
    # For class-0: tp0 is tn; fp0 is fn1; fn0 is fp1
    prec0 = tn / max(1, (tn + fn1))
    rec0  = tn / max(1, (tn + fp1))
    f1_0  = 0.0 if (prec0 + rec0) == 0 else 2 * prec0 * rec0 / (prec0 + rec0)

    macro_f1 = 0.5 * (f1_0 + f1_1)
    return dict(acc=acc, p1=prec1, r1=rec1, f1_1=f1_1, f1_0=f1_0, macro_f1=macro_f1)

# ===== Train / Eval =====

def train_one_epoch(
    epoch: int,
    model: nn.Module,
    train_loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    scheduler,
    scaler,
    criterion: nn.Module,
    device: torch.device,
    grad_accum: int,
    max_grad_norm: float,
    fp16: bool,
):
    model.train()
    total_loss = 0.0
    step_loss  = 0.0
    step_count = 0

    # streaming counts for display (local)
    local_counts = dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)

    for step, batch in enumerate(train_loader):
        ids   = to_device(batch["ids"],   device)
        mask  = to_device(batch["mask"],  device)
        gmask = to_device(batch["gmask"], device)
        gold  = to_device(batch["targets"], device)

        outputs = None
        with torch.cuda.amp.autocast(enabled=fp16):
            outputs = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=None)
            logits = outputs.logits  # [B, L, 2]
            loss = criterion(logits, gold) / grad_accum

        if scaler is not None:
            scaler.scale(loss).backward()
        else:
            loss.backward()

        step_loss += float(loss.item())
        step_count += 1

        # metrics on-the-fly (no sync here; just for prog bar on each rank)
        with torch.no_grad():
            pred = torch.argmax(logits, dim=-1)
            c = batch_confmat(pred, gold, mask)
            for k in local_counts:
                local_counts[k] += c[k]

        if (step + 1) % grad_accum == 0:
            if scaler is not None:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
                scaler.step(optimizer)
                scaler.update()
            else:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_grad_norm)
                optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            if scheduler is not None:
                scheduler.step()
            total_loss += step_loss
            step_loss = 0.0

    # reduce metrics across ranks
    red_counts = reduce_counts(local_counts)
    metrics = counts_to_metrics(red_counts)

    # average loss across gradient steps
    world = dist.get_world_size() if (dist.is_available() and dist.is_initialized()) else 1
    total_loss = total_loss / max(1, (step_count // grad_accum))
    # average loss across ranks
    tl = torch.tensor([total_loss], dtype=torch.float, device=device)
    total_loss = reduce_scalar(tl, op=dist.ReduceOp.SUM) / world

    return total_loss, metrics


@torch.no_grad()
def evaluate(
    model: nn.Module,
    eval_loader: DataLoader,
    criterion: nn.Module,
    device: torch.device,
    fp16: bool,
):
    model.eval()
    total_loss = 0.0
    nb = 0

    local_counts = dict(total=0, correct=0, tp1=0, fp1=0, fn1=0)

    for batch in eval_loader:
        ids   = to_device(batch["ids"],   device)
        mask  = to_device(batch["mask"],  device)
        gmask = to_device(batch["gmask"], device)
        gold  = to_device(batch["targets"], device)

        with torch.cuda.amp.autocast(enabled=fp16):
            outputs = model(input_ids=ids, attention_mask=mask, global_attention_mask=gmask, labels=None)
            logits = outputs.logits
            loss = criterion(logits, gold)

        total_loss += float(loss.item())
        nb += 1

        pred = torch.argmax(logits, dim=-1)
        c = batch_confmat(pred, gold, mask)
        for k in local_counts:
            local_counts[k] += c[k]

    red_counts = reduce_counts(local_counts)
    metrics = counts_to_metrics(red_counts)

    # average loss across batches
    total_loss = total_loss / max(1, nb)
    # average across ranks
    tl = torch.tensor([total_loss], dtype=torch.float, device=device)
    world = dist.get_world_size() if (dist.is_available() and dist.is_initialized()) else 1
    total_loss = reduce_scalar(tl, op=dist.ReduceOp.SUM) / world

    return total_loss, metrics

# ===== Main =====

def main():
    parser = argparse.ArgumentParser()
    # data & model
    parser.add_argument("--data-path", required=True, type=str, help=".pt file produced by exact aligner (list[dict])")
    parser.add_argument("--model-name", default="allenai/longformer-large-4096", type=str)
    parser.add_argument("--save-dir",  default="./ckpt_longformer_large", type=str)

    # train
    parser.add_argument("--epochs", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.06)
    parser.add_argument("--max-grad-norm", type=float, default=1.0)

    # loss
    parser.add_argument("--use-focal", action="store_true", default=False)
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--gamma", type=float, default=2.0)

    # optimization
    parser.add_argument("--fp16", action="store_true", default=False)
    parser.add_argument("--grad-chkpt", action="store_true", default=True)

    # attention
    parser.add_argument("--use-global-attn", action="store_true", default=True)

    # runtime
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--log-interval", type=int, default=50)
    parser.add_argument("--save-every", type=int, default=0, help="save every N steps (0 to disable)")
    parser.add_argument("--early-stop", type=int, default=3, help="patience epochs on macro-F1")

    # resume
    parser.add_argument("--resume", type=str, default=None)

    args = parser.parse_args()

    # ----- DDP init -----
    if "LOCAL_RANK" in os.environ:
        local_rank = int(os.environ["LOCAL_RANK"])
        torch.cuda.set_device(local_rank)
        dist.init_process_group(backend="nccl", init_method="env://")
        device = torch.device(f"cuda:{local_rank}")
    else:
        local_rank = 0
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    if is_main_process():
        os.makedirs(args.save_dir, exist_ok=True)
        print(vars(args))

    set_seed(args.seed)
    barrier()

    # ----- load data -----
    if is_main_process():
        print(f"Loading data from: {args.data-path if hasattr(args,'data-path') else args.data_path}")
    data = torch.load(args.data_path, map_location="cpu")

    # split (80/20)
    N = len(data)
    idx = list(range(N))
    random.shuffle(idx)
    cut = int(0.8 * N)
    tr_idx = idx[:cut]
    ev_idx = idx[cut:]

    train_items = [data[i] for i in tr_idx]
    eval_items  = [data[i] for i in ev_idx]

    train_ds = ExactCOTDataset(train_items)
    eval_ds  = ExactCOTDataset(eval_items)

    # ----- tokenizer / pad id -----
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    pad_id = tokenizer.pad_token_id
    if pad_id is None:
        # Longformer has pad token; but just in case
        tokenizer.add_special_tokens({"pad_token": "[PAD]"})
        pad_id = tokenizer.pad_token_id

    collate = make_collate_fn(CollateCfg(pad_token_id=pad_id, label_pad_id=-100))

    # ----- samplers & loaders -----
    if dist.is_available() and dist.is_initialized():
        train_sampler = DistributedSampler(train_ds, shuffle=True, drop_last=False)
        eval_sampler  = DistributedSampler(eval_ds,  shuffle=False, drop_last=False)
    else:
        train_sampler = None
        eval_sampler  = None

    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        sampler=train_sampler,
        shuffle=(train_sampler is None),
        num_workers=args.num_workers,
        pin_memory=True,
        collate_fn=collate,
        drop_last=False,
    )
    eval_loader = DataLoader(
        eval_ds,
        batch_size=args.batch_size,
        sampler=eval_sampler,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=True,
        collate_fn=collate,
        drop_last=False,
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

    if dist.is_available() and dist.is_initialized():
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], output_device=local_rank, find_unused_parameters=False
        )

    # ----- optimizer / scheduler -----
    # steps per epoch (effective)
    steps_per_epoch = math.ceil(len(train_loader) / max(1, args.grad_accum))
    total_steps = steps_per_epoch * args.epochs
    warmup_steps = int(total_steps * args.warmup_ratio)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay, eps=1e-8)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=warmup_steps, num_training_steps=total_steps)

    # ----- loss -----
    if args.use_focal:
        criterion = WeightedFocalLoss(class_weights=None, alpha=args.alpha, gamma=args.gamma, ignore_index=-100)
    else:
        criterion = nn.CrossEntropyLoss(ignore_index=-100)

    scaler = torch.cuda.amp.GradScaler(enabled=args.fp16)

    # ----- resume -----
    start_epoch = 0
    best_macro_f1 = 0.0
    if args.resume and os.path.exists(args.resume):
        ckpt = torch.load(args.resume, map_location="cpu")
        if "model_state_dict" in ckpt:
            (model.module if hasattr(model, "module") else model).load_state_dict(ckpt["model_state_dict"])
        if "optimizer_state_dict" in ckpt:
            optimizer.load_state_dict(ckpt["optimizer_state_dict"])
        if "scheduler_state_dict" in ckpt and ckpt["scheduler_state_dict"] is not None:
            scheduler.load_state_dict(ckpt["scheduler_state_dict"])
        start_epoch = ckpt.get("epoch", 0)
        best_macro_f1 = ckpt.get("best_f1", 0.0)
        if is_main_process():
            print(f"[Resume] from epoch={start_epoch}, best_macro_f1={best_macro_f1:.4f}")

    # ----- tensorboard (main process only) -----
    writer = None
    if is_main_process():
        from torch.utils.tensorboard import SummaryWriter
        tb_dir = os.path.join(args.save_dir, "tb_logs")
        os.makedirs(tb_dir, exist_ok=True)
        writer = SummaryWriter(tb_dir)

    # ----- training loop -----
    patience = 0
    for epoch in range(start_epoch, args.epochs):
        if dist.is_available() and dist.is_initialized():
            if isinstance(train_loader.sampler, DistributedSampler):
                train_loader.sampler.set_epoch(epoch)

        t0 = time.time()
        train_loss, train_metrics = train_one_epoch(
            epoch, model, train_loader, optimizer, scheduler, scaler, criterion,
            device=device, grad_accum=args.grad_accum, max_grad_norm=args.max_grad_norm, fp16=args.fp16
        )
        t1 = time.time()

        eval_loss, eval_metrics = evaluate(
            model, eval_loader, criterion, device=device, fp16=args.fp16
        )
        t2 = time.time()

        if is_main_process():
            print(f"\nEpoch {epoch+1}/{args.epochs}")
            print(f"  Train: loss={train_loss:.4f}, acc={train_metrics['acc']:.4f}, macroF1={train_metrics['macro_f1']:.4f}  ({t1-t0:.1f}s)")
            print(f"  Eval : loss={eval_loss:.4f}, acc={eval_metrics['acc']:.4f}, macroF1={eval_metrics['macro_f1']:.4f}  ({t2-t1:.1f}s)")

            # TB
            writer.add_scalar("loss/train", train_loss, epoch)
            writer.add_scalar("loss/eval",  eval_loss,  epoch)
            writer.add_scalar("metric/train_acc",    train_metrics["acc"], epoch)
            writer.add_scalar("metric/train_macroF1",train_metrics["macro_f1"], epoch)
            writer.add_scalar("metric/eval_acc",     eval_metrics["acc"], epoch)
            writer.add_scalar("metric/eval_macroF1", eval_metrics["macro_f1"], epoch)
            writer.flush()

            # save latest
            latest = {
                "epoch": epoch+1,
                "model_state_dict": (model.module if hasattr(model, "module") else model).state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "scheduler_state_dict": scheduler.state_dict(),
                "best_f1": best_macro_f1,
                "args": vars(args)
            }
            torch.save(latest, os.path.join(args.save_dir, "latest_checkpoint.pth"))

            # best
            if eval_metrics["macro_f1"] > best_macro_f1:
                best_macro_f1 = eval_metrics["macro_f1"]
                patience = 0
                torch.save(latest, os.path.join(args.save_dir, "best_checkpoint.pth"))
                (model.module if hasattr(model, "module") else model).save_pretrained(args.save_dir)
                tokenizer.save_pretrained(args.save_dir)
                with open(os.path.join(args.save_dir, "training_config.json"), "w") as f:
                    json.dump({
                        "best_macro_f1": best_macro_f1,
                        "epoch": epoch+1,
                        "args": vars(args)
                    }, f, indent=2)
                print("  ✅ New best model saved.")
            else:
                patience += 1
                print(f"  (no improv; patience {patience}/{args.early_stop})")

            if patience >= args.early_stop:
                print("Early stopping triggered.")
                break

    if writer is not None:
        writer.close()

    barrier()
    if is_main_process():
        print("Training finished.")
        print(f"Best macro-F1: {best_macro_f1:.4f}")

if __name__ == "__main__":
    main()
