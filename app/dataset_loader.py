from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = ROOT / "dataset"
EXPANDED_DIR = DATASET_DIR / "expanded"


def _ensure_expanded_dataset() -> None:
    if (EXPANDED_DIR / "categories").exists() and (EXPANDED_DIR / "merchants").exists():
        return

    script = DATASET_DIR / "generate_dataset.py"
    if script.exists():
        subprocess.run(
            [sys.executable, str(script), "--seed-dir", str(DATASET_DIR), "--out", str(EXPANDED_DIR)],
            check=True,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def load_all_datasets() -> dict[str, dict[str, Any]]:
    _ensure_expanded_dataset()

    data: dict[str, dict[str, Any]] = {"categories": {}, "merchants": {}, "customers": {}, "triggers": {}}

    categories_dir = EXPANDED_DIR / "categories"
    if categories_dir.exists():
        for path in sorted(categories_dir.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            data["categories"][item["slug"]] = item

    merchants_dir = EXPANDED_DIR / "merchants"
    if merchants_dir.exists():
        for path in sorted(merchants_dir.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            data["merchants"][item["merchant_id"]] = item

    customers_dir = EXPANDED_DIR / "customers"
    if customers_dir.exists():
        for path in sorted(customers_dir.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            data["customers"][item["customer_id"]] = item

    triggers_dir = EXPANDED_DIR / "triggers"
    if triggers_dir.exists():
        for path in sorted(triggers_dir.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            data["triggers"][item["id"]] = item

    return data


def seed_context_store() -> dict[str, int]:
    datasets = load_all_datasets()
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}

    for slug, category in datasets["categories"].items():
        from app.store import contexts
        contexts[("category", slug)] = {"version": 1, "payload": category}
        counts["category"] += 1

    for merchant_id, merchant in datasets["merchants"].items():
        from app.store import contexts
        contexts[("merchant", merchant_id)] = {"version": 1, "payload": merchant}
        counts["merchant"] += 1

    for customer_id, customer in datasets["customers"].items():
        from app.store import contexts
        contexts[("customer", customer_id)] = {"version": 1, "payload": customer}
        counts["customer"] += 1

    for trigger_id, trigger in datasets["triggers"].items():
        from app.store import contexts
        contexts[("trigger", trigger_id)] = {"version": 1, "payload": trigger}
        counts["trigger"] += 1

    return counts
