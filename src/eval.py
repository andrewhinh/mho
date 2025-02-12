import base64
import json
import os
from itertools import chain
from pathlib import Path

import modal
import numpy as np
import torch
import yaml
from huggingface_hub import login
from more_itertools import chunked
from pydantic import BaseModel
from scipy.optimize import linear_sum_assignment
from tqdm import tqdm
from vllm import LLM, SamplingParams
from vllm.sampling_params import GuidedDecodingParams

from utils import (
    APP_NAME,
    DATA_VOL_PATH,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    GPU_IMAGE,
    MINUTES,
    SECRETS,
    SPLITS,
    VOLUME_CONFIG,
)

# -----------------------------------------------------------------------------

# vlm config
TOKENIZER = "Qwen/Qwen2.5-VL-3B-Instruct"
BASE_MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
# BASE_QUANT_MODEL = f"andrewhinh/{APP_NAME}-Qwen2.5-VL-3B-Instruct-AWQ"
# SFT_MODEL = f"andrewhinh/{APP_NAME}-qwen2.5-vl-3b-instruct-lora-sft-merged"
# SFT_QUANT_MODEL = f"andrewhinh/{APP_NAME}-qwen2.5-vl-3b-instruct-lora-sft-merged-awq"
# DPO_MODEL = f"andrewhinh/{APP_NAME}-qwen2.5-vl-3b-instruct-lora-dpo-merged"
# DPO_QUANT_MODEL = f"andrewhinh/{APP_NAME}-qwen2.5-vl-3b-instruct-lora-dpo-merged-awq"

KV_CACHE_DTYPE = None  # "fp8_e5m2"
ENFORCE_EAGER = False
MAX_NUM_SEQS = 32 if modal.is_local() else 128
MIN_PIXELS = 28 * 28
MAX_PIXELS = 1280 * 28 * 28
TEMPERATURE = 0.1
TOP_P = 0.001
REPEATION_PENALTY = 1.1
STOP_TOKEN_IDS = []
MAX_MODEL_LEN = 8192 if modal.is_local() else 32768
MAX_TOKENS = 4096


# -----------------------------------------------------------------------------


# output schema
class Point(BaseModel):
    x: float
    y: float


class Substructure(BaseModel):
    name: str
    points: list[Point]


class Substructures(BaseModel):
    substructures: list[Substructure]


JSON_STRUCTURE = Substructures.model_json_schema()


## container startup fn
def download_models():
    from huggingface_hub import snapshot_download

    login(token=os.getenv("HF_TOKEN"), new_session=False)

    for model in [
        TOKENIZER,
        BASE_MODEL,
        # BASE_QUANT_MODEL,
        # SFT_MODEL,
        # SFT_QUANT_MODEL,
        # DPO_MODEL,
        # DPO_QUANT_MODEL,
    ]:
        if not os.path.exists(model):
            snapshot_download(
                model,
                ignore_patterns=["*.pt", "*.bin"],
            )
        else:  # check if preprocessor_config.json was successfully copied; if not, do so
            if not os.path.exists(f"{model}/preprocessor_config.json"):
                tok_path = snapshot_download(
                    model,
                    ignore_patterns=["*.pt", "*.bin"],
                )
                os.rename(
                    f"{tok_path}/preprocessor_config.json",
                    f"{model}/preprocessor_config.json",
                )


# Modal
IMAGE = GPU_IMAGE.run_function(
    download_models,
    secrets=SECRETS,
    volumes=VOLUME_CONFIG,
)
TIMEOUT = 24 * 60 * MINUTES

if modal.is_local():
    GPU_COUNT = torch.cuda.device_count()
else:
    GPU_COUNT = 1

GPU_TYPE = "l40s"
GPU_SIZE = None  # options = None, "40GB", "80GB"
GPU_CONFIG = f"{GPU_TYPE}:{GPU_COUNT}"

app = modal.App(name=f"{APP_NAME}-eval")

# -----------------------------------------------------------------------------

# helpers


def compute_msa_per_label(gts, preds):
    n, m = len(gts), len(preds)
    cost_matrix = np.zeros((n, m))
    for i in range(n):
        for j in range(m):
            cost_matrix[i, j] = np.linalg.norm(np.array(gts[i]) - np.array(preds[j]))
    gt_indices, pred_indices = linear_sum_assignment(cost_matrix)
    matched_distances = cost_matrix[gt_indices, pred_indices]
    num_matched = len(gt_indices)
    return {
        "average_euclidean_distance": np.mean(matched_distances)
        if num_matched > 0
        else 0.0,
        "num_matched": num_matched,
        "false_positives": m - num_matched,
        "false_negatives": n - num_matched,
    }


def compute_msa(gt_list, pred_list):
    all_metrics = []
    for gt_labels, pred_labels in zip(gt_list, pred_list):
        gt_ids, pred_ids = set(gt_labels.keys()), set(pred_labels.keys())
        matched_ids = gt_ids & pred_ids
        false_negative_labels = gt_ids - pred_ids
        false_positive_labels = pred_ids - gt_ids
        metrics = {
            "num_matched_labels": len(matched_ids),
            "false_positive_labels": len(false_positive_labels),
            "false_negative_labels": len(false_negative_labels),
            "point_metrics_per_label": [],
        }
        for label in matched_ids:
            gt_points = gt_labels[label]
            pred_points = pred_labels[label]
            if len(gt_points) > 0 and len(pred_points) > 0:
                metrics["point_metrics_per_label"].append(
                    {"label": label, **compute_msa_per_label(gt_points, pred_points)}
                )
            elif len(gt_points) <= 0:
                false_negative_labels.add(label)
            elif len(pred_points) <= 0:
                false_positive_labels.add(label)

        # Add unmatched labels as metrics (FN for ground truth, FP for predictions)
        for label in false_negative_labels:
            metrics["point_metrics_per_label"].append(
                {
                    "label": label,
                    "average_euclidean_distance": 0.0,
                    "num_matched": 0,
                    "false_positives": 0,
                    "false_negatives": len(gt_labels[label]),
                    "precision": 0.0,
                    "recall": 0.0,
                }
            )
        for label in false_positive_labels:
            metrics["point_metrics_per_label"].append(
                {
                    "label": label,
                    "average_euclidean_distance": 0.0,
                    "num_matched": 0,
                    "false_positives": len(pred_labels[label]),
                    "false_negatives": 0,
                    "precision": 0.0,
                    "recall": 0.0,
                }
            )
        all_metrics.append(metrics)
    return all_metrics


def summarize_msa(msa):
    total_labels = {"matched": 0, "fp": 0, "fn": 0}
    total_points = {"matched": 0, "fp": 0, "fn": 0, "euclidean_distance": 0.0}

    for metric in msa:
        total_labels["matched"] += metric["num_matched_labels"]
        total_labels["fp"] += metric["false_positive_labels"]
        total_labels["fn"] += metric["false_negative_labels"]
        for point_metric in metric["point_metrics_per_label"]:
            total_points["matched"] += point_metric["num_matched"]
            total_points["fp"] += point_metric["false_positives"]
            total_points["fn"] += point_metric["false_negatives"]
            total_points["euclidean_distance"] += (
                point_metric["average_euclidean_distance"] * point_metric["num_matched"]
            )

    # Compute metrics
    label_precision = (
        total_labels["matched"] / (total_labels["matched"] + total_labels["fp"])
        if total_labels["fp"] + total_labels["matched"] > 0
        else 0.0
    )
    label_recall = (
        total_labels["matched"] / (total_labels["matched"] + total_labels["fn"])
        if total_labels["fn"] + total_labels["matched"] > 0
        else 0.0
    )
    label_f1 = (
        2 * label_precision * label_recall / (label_precision + label_recall)
        if label_precision + label_recall > 0
        else 0.0
    )
    point_precision = (
        total_points["matched"] / (total_points["matched"] + total_points["fp"])
        if total_points["fp"] + total_points["matched"] > 0
        else 0.0
    )
    point_recall = (
        total_points["matched"] / (total_points["matched"] + total_points["fn"])
        if total_points["fn"] + total_points["matched"] > 0
        else 0.0
    )
    point_f1 = (
        2 * point_precision * point_recall / (point_precision + point_recall)
        if point_precision + point_recall > 0
        else 0.0
    )
    avg_euclidean_distance = (
        total_points["euclidean_distance"] / total_points["matched"]
        if total_points["matched"] > 0
        else float("inf")
    )

    return {
        "label_metrics": {
            "precision": round(label_precision, 2),
            "recall": round(label_recall, 2),
            "f1": round(label_f1, 2),
        },
        "point_metrics": {
            "precision": round(point_precision, 2),
            "recall": round(point_recall, 2),
            "f1": round(point_f1, 2),
            "avg_euclidean_distance": round(avg_euclidean_distance, 2),
        },
    }


@app.function(
    image=IMAGE,
    gpu=GPU_CONFIG,
    volumes=VOLUME_CONFIG,
    secrets=SECRETS,
    timeout=TIMEOUT,
)
def run_model(img_paths: list[Path], model: str, quant: bool) -> list[dict]:
    conversations = []
    for img_path in img_paths:
        with open(img_path, "rb") as image_file:
            base64_img = base64.b64encode(image_file.read()).decode("utf-8")
        img_url = f"data:image/jpeg;base64,{base64_img}"
        conversations.append(
            [
                {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": DEFAULT_USER_PROMPT},
                        {"type": "image_url", "image_url": {"url": img_url}},
                    ],
                },
            ]
        )

    global quantization
    global llm
    global sampling_params
    # load pretrained vlm if not already loaded
    if "quantization" not in globals():
        quantization = "awq_marlin" if quant else None
    if "llm" not in globals():
        llm = LLM(
            model=model,
            enforce_eager=ENFORCE_EAGER,
            max_num_seqs=MAX_NUM_SEQS,
            tensor_parallel_size=GPU_COUNT,
            trust_remote_code=True,
            max_model_len=MAX_MODEL_LEN,
            mm_processor_kwargs={
                "min_pixels": MIN_PIXELS,
                "max_pixels": MAX_PIXELS,
            },
            **{
                k: v
                for k, v in [
                    ("quantization", quantization),
                    ("kv_cache_dtype", KV_CACHE_DTYPE),
                ]
                if v is not None
            },
        )
    if "sampling_params" not in globals():
        sampling_params = SamplingParams(
            temperature=TEMPERATURE,
            top_p=TOP_P,
            repetition_penalty=REPEATION_PENALTY,
            stop_token_ids=STOP_TOKEN_IDS,
            max_tokens=MAX_TOKENS,
            guided_decoding=GuidedDecodingParams(json=JSON_STRUCTURE),
        )
    outputs = llm.chat(conversations, sampling_params, use_tqdm=True)
    preds = [out.outputs[0].text.strip() for out in outputs]
    for pred in preds:
        try:
            json.loads(pred)
        except Exception:
            print(pred)
    preds = [json.loads(pred)["substructures"] for pred in preds]
    return preds


# -----------------------------------------------------------------------------

# main


def main(base: bool, sft: bool, dpo: bool, quant: bool):
    if not base and not sft and not dpo:
        raise ValueError("Must specify at least one of `base`, `sft`, or `dpo`)")

    split_msa = {}
    for split in SPLITS:
        with open(DATA_VOL_PATH / f"sft_{split}.json", "r") as f:
            read_ds = yaml.safe_load(f)
        img_paths = [sample["images"][0] for sample in read_ds]
        labels = [json.loads(sample["conversations"][1]["value"]) for sample in read_ds]

        ## run
        img_batches = list(chunked(img_paths, MAX_NUM_SEQS))
        model = (
            BASE_MODEL
            if base and not quant
            # else SFT_MODEL
            # if sft and not quant
            # else DPO_MODEL
            # if dpo and not quant
            # else BASE_QUANT_MODEL
            # if base and quant
            # else SFT_QUANT_MODEL
            # if sft and quant
            # else DPO_QUANT_MODEL
            # if dpo and quant
            else None
        )
        if modal.is_local():
            preds = list(
                tqdm(
                    chain.from_iterable(
                        run_model.local(batch, model, quant) for batch in img_batches
                    ),
                    desc=split,
                    total=len(img_batches),
                )
            )
        else:
            lst_preds = run_model.starmap(
                [(batch, model, quant) for batch in img_batches]
            )
            preds = [item for lst in lst_preds for item in lst]

        preds = [
            {
                substructure["name"]: [
                    [point["x"], point["y"]] for point in substructure["points"]
                ]
                for substructure in pred
            }
            for pred in preds
        ]

        split_msa[split] = compute_msa(labels, preds)

    for split, msa in split_msa.items():
        summary = summarize_msa(msa)

        print(f"\n{'='*50}")
        print(f"Metrics for Split: '{split}'")
        print(f"{'='*50}")
        print(f"{'Metric':<25}{'Value':>10}")
        print(f"{'-'*50}")

        # Label-level metrics
        print(
            f"{'Label-Level Precision':<25}{summary['label_metrics']['precision']:.2f}"
        )
        print(f"{'Label-Level Recall':<25}{summary['label_metrics']['recall']:.2f}")
        print(f"{'Label-Level F1-Score':<25}{summary['label_metrics']['f1']:.2f}")

        # Separator for point-level metrics
        print(f"{'-'*50}")

        # Point-level metrics
        print(
            f"{'Point-Level Precision':<25}{summary['point_metrics']['precision']:.2f}"
        )
        print(f"{'Point-Level Recall':<25}{summary['point_metrics']['recall']:.2f}")
        print(f"{'Point-Level F1-Score':<25}{summary['point_metrics']['f1']:.2f}")
        print(
            f"{'Avg. Euclidean Distance':<25}{summary['point_metrics']['avg_euclidean_distance']:.2f}"
        )
        print(f"{'='*50}")


@app.function(
    image=IMAGE,
    gpu=GPU_CONFIG,
    volumes=VOLUME_CONFIG,
    secrets=SECRETS,
    timeout=TIMEOUT,
)
def run(base: bool, sft: bool, dpo: bool, quant: bool):
    main(base, sft, dpo, quant)


@app.local_entrypoint()
def local(
    base: bool = False, sft: bool = False, dpo: bool = False, quant: bool = False
):
    run.remote(base, sft, dpo, quant)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", action="store_true")
    parser.add_argument("--sft", action="store_true")
    parser.add_argument("--dpo", action="store_true")
    parser.add_argument("--quant", action="store_true")
    args = parser.parse_args()
    main(args.base, args.sft, args.dpo, args.quant)
