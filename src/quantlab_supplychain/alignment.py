from __future__ import annotations

import gc
import json
import os
import random
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

from .io import normal_name, read_json, save_json


def alias_catalog(ctx, prepared):
    records = read_json(ctx, prepared["aliases"])
    grouped = defaultdict(list)
    for row in records:
        grouped[row.get("group_id", row["entity"])].append(row)
    pairs = []
    mapping = {}
    for entity, rows in sorted(grouped.items()):
        rows.sort(key=lambda r: r["effective_date"])
        canonical = rows[-1]["canonical"]
        for alias in sorted({r["alias"] for r in rows} | {r["canonical"] for r in rows}):
            mapping["N:" + normal_name(alias)] = rows[-1]["entity"]
            if alias != canonical:
                pairs.append({"entity": entity, "alias": alias, "canonical": canonical})
    return pairs, mapping


def align_rules(ctx, inputs, params):
    pairs, mapping = alias_catalog(ctx, inputs["prepared"])
    return {
        "alignment": {
            "mapping": mapping,
            "method": "stable identifiers, unique normalized names, verified name-change aliases; unresolved names remain separate",
            "verified_pairs": len(pairs),
        },
        "pairs": save_json(ctx, pairs, "entity_alignment_pairs.json"),
    }


def load_alignment(ctx, inputs, params):
    value = json.loads(ctx.file(params["file"]).read_text(encoding="utf-8"))
    if not isinstance(value.get("mapping"), dict) or not value.get("source_run_id"):
        raise ValueError("实体对齐输入缺少映射或已完成实验来源")
    return {"alignment": value}


def train_lora(ctx, inputs, params):
    import torch
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import linear_kernel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    torch.set_num_threads(8)
    random.seed(ctx.seed)
    np.random.seed(ctx.seed)
    torch.manual_seed(ctx.seed)
    if not torch.cuda.is_available():
        raise RuntimeError("Qwen3-4B量化LoRA需要可用CUDA GPU；未将规则匹配伪装成微调")
    pairs, mapping = alias_catalog(ctx, inputs["prepared"])
    groups = sorted({p["entity"] for p in pairs})
    random.Random(ctx.seed).shuffle(groups)
    train_ids = set(groups[: int(len(groups) * 0.8)])
    val_ids = set(groups[int(len(groups) * 0.8) : int(len(groups) * 0.9)])
    train = [p for p in pairs if p["entity"] in train_ids][: params["max_train_examples"]]
    validation = [p for p in pairs if p["entity"] in val_ids]
    test = [p for p in pairs if p["entity"] not in train_ids | val_ids][: params["test_examples"]]
    if len(train) < 50 or len(test) < 10:
        raise ValueError("真实更名对不足以进行按企业留出的LoRA评估")
    model_manifest = json.loads(ctx.file(params["model_manifest"]).read_text(encoding="utf-8"))
    model_path = Path(ctx.work) / "base_model"
    model_path.mkdir()
    for filename, stored in model_manifest["files"].items():
        source = ctx.file(stored)
        target = model_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            __import__("shutil").copyfile(source, target)
    ctx.log(f"真实Qwen3-4B：训练{len(train)}对，验证{len(validation)}对，测试{len(test)}对；按企业隔离")
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, local_files_only=True, trust_remote_code=False, padding_side="left"
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        local_files_only=True,
        trust_remote_code=False,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )

    def prompt(alias):
        return tokenizer.apply_chat_template(
            [
                {
                    "role": "system",
                    "content": "将企业历史名称规范化为标准企业全称。仅输出名称，不解释；无法确认时保留原名。",
                },
                {"role": "user", "content": alias},
            ],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def generate(examples, network):
        network.eval()
        predictions = []
        for i in range(0, len(examples), 4):
            rows = examples[i : i + 4]
            tokens = tokenizer(
                [prompt(p["alias"]) for p in rows], return_tensors="pt", padding=True, truncation=True, max_length=144
            ).to("cuda")
            with torch.inference_mode():
                output = network.generate(
                    **tokens, max_new_tokens=56, do_sample=False, pad_token_id=tokenizer.pad_token_id
                )
            values = tokenizer.batch_decode(output[:, tokens.input_ids.shape[1] :], skip_special_tokens=True)
            predictions.extend(normal_name(v.split("\n")[0].strip('"“”')) for v in values)
        return predictions

    before = generate(test, model)
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    model = get_peft_model(
        model,
        LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "v_proj"],
            bias="none",
            task_type="CAUSAL_LM",
        ),
    )
    model.config.use_cache = False
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=2e-4)
    model.train()
    losses = []
    order = list(range(len(train)))
    random.Random(ctx.seed).shuffle(order)
    accumulation = 4
    optimizer.zero_grad(set_to_none=True)
    for step in range(params["steps"] * accumulation):
        row = train[order[step % len(order)]]
        prefix = tokenizer(prompt(row["alias"]), add_special_tokens=False).input_ids
        target = tokenizer(row["canonical"] + tokenizer.eos_token, add_special_tokens=False).input_ids
        ids = (prefix + target)[:192]
        labels = ([-100] * len(prefix) + target)[:192]
        batch = {
            "input_ids": torch.tensor([ids], device="cuda"),
            "attention_mask": torch.ones((1, len(ids)), dtype=torch.long, device="cuda"),
            "labels": torch.tensor([labels], device="cuda"),
        }
        loss = model(**batch).loss
        (loss / accumulation).backward()
        losses.append(float(loss.detach()))
        if (step + 1) % accumulation == 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            if (step + 1) % (accumulation * 10) == 0:
                ctx.log(
                    f"LoRA步{(step + 1) // accumulation}/{params['steps']}，最近损失{np.mean(losses[-accumulation * 10 :]):.4f}"
                )
    model.config.use_cache = True
    after = generate(test, model)
    adapter = Path(ctx.work) / "adapter"
    model.save_pretrained(adapter)
    tokenizer.save_pretrained(adapter)
    archive = Path(ctx.work) / "qwen3_4b_lora.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in adapter.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(adapter))
    adapter_ref = ctx.save_file(archive)
    canonical = sorted({p["canonical"] for p in pairs})
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 4), min_df=1)
    library = vectorizer.fit_transform(canonical)

    def retrieval(values):
        score = linear_kernel(vectorizer.transform(values), library)
        return [canonical[int(i)] for i in score.argmax(axis=1)]

    raw = [p["alias"] for p in test]
    baseline = retrieval(raw)
    base_retrieval = retrieval(before)
    lora_retrieval = retrieval(after)
    gold = [p["canonical"] for p in test]

    def accuracy(predictions):
        return float(np.mean([a == b for a, b in zip(predictions, gold, strict=True)]))

    examples = [
        {
            **p,
            "base_generation": b,
            "lora_generation": a,
            "raw_retrieval": r,
            "base_retrieval": br,
            "lora_retrieval": lr,
        }
        for p, b, a, r, br, lr in zip(test, before, after, baseline, base_retrieval, lora_retrieval, strict=True)
    ]
    result = {
        "model": "Qwen/Qwen3-4B",
        "model_revision": model_manifest.get("revision"),
        "method": "4-bit NF4 QLoRA;rank8;q_proj,v_proj",
        "training_examples": len(train),
        "validation_examples": len(validation),
        "test_examples": len(test),
        "split_by": "InstitutionID;80/10/10;test entities absent from training",
        "optimizer_steps": params["steps"],
        "seed": ctx.seed,
        "final_loss": float(np.mean(losses[-40:])),
        "raw_tfidf_top1": accuracy(baseline),
        "base_exact_name": accuracy(before),
        "lora_exact_name": accuracy(after),
        "base_tfidf_top1": accuracy(base_retrieval),
        "lora_tfidf_top1": accuracy(lora_retrieval),
        "peak_gpu_gb": float(torch.cuda.max_memory_allocated() / 1e9),
        "test_examples_evidence": examples,
        "loss_history": losses,
        "adapter": adapter_ref,
        "interpretation": "Historical renaming can change facts rather than spelling; generative suggestions are not accepted as identity without independent evidence. Verified identifier/alias constraints determine graph merges.",
    }
    result_ref = save_json(ctx, result, "lora_evaluation.json")
    del model, optimizer
    gc.collect()
    torch.cuda.empty_cache()
    return {
        "lora_report": result,
        "alignment": {
            "mapping": mapping,
            "method": "Qwen3-4B QLoRA normalization and TF-IDF candidate retrieval evaluated; graph merges constrained by verified IDs/name-change records",
            "verified_pairs": len(pairs),
            "evaluation": result_ref,
        },
    }
