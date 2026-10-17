"""Check live model inference and the Telum AI accelerator on this machine.

Run from the repository root on LinuxONE:
    python3 app/check_accelerator.py

Prints the CPU details, which Snap ML tree formats load the trained model, whether
their predictions match XGBoost and the stored scores, and batch timings.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_service as ms  # noqa: E402
from engine import load_localities  # noqa: E402


def main():
    cpu = ms.cpu_info()
    print(f"CPU: {cpu['machine']}, machine type {cpu['machine_type'] or 'n/a'}, "
          f"nnpa (AI accelerator): {'yes' if cpu['nnpa'] else 'no'}")

    localities = load_localities()
    service = ms.ModelService(localities)
    for message in service.status["errors"]:
        print("Note:", message)
    if not service.backends:
        print("No model backend loaded.")
        return 1

    for name, info in service.status["backends"].items():
        print(f"{name}: {info}")

    try:
        import numpy as np
        from snapml import BoostingMachineClassifier
    except ImportError:
        print("Snap ML not installed; skipping the tree-format comparison.")
    else:
        print("\nSnap ML tree formats:")
        reference = service.backends["xgboost"](service.X) if "xgboost" in service.backends else None
        for tree_format in ("zdnn_tensors", "compress_trees", "ensemble", "auto"):
            model = BoostingMachineClassifier()
            try:
                model.import_model(str(ms.MODEL_PATH), input_type="xgb_json", tree_format=tree_format)
                started = time.perf_counter()
                for _ in range(20):
                    preds = model.predict_proba(service.X)[:, 1]
                ms_per_batch = (time.perf_counter() - started) * 1000 / 20
                agree = (f", max diff vs XGBoost {float(np.max(np.abs(preds - reference))):.2e}"
                         if reference is not None else "")
                print(f"  {tree_format:15s} OK  {ms_per_batch:8.3f} ms per {len(service.X)} rows{agree}")
            except Exception as exc:
                print(f"  {tree_format:15s} failed: {exc.__class__.__name__}: {exc}")

    bench = service.benchmark(repeats=30)
    print("\nBenchmark (median of 30 batches):")
    for name, r in bench["results"].items():
        print(f"  {name:8s} {r['median_ms']:8.3f} ms per batch, {r['per_locality_us']:6.2f} us per locality")
    return 0


if __name__ == "__main__":
    sys.exit(main())
