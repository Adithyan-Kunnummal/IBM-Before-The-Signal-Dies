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
    import warnings
    warnings.simplefilter("once")  # Snap ML repeats its NNPA warning on every call

    cpu = ms.cpu_info()
    print(f"CPU: {cpu['machine']}, machine type {cpu['machine_type'] or 'n/a'}, "
          f"nnpa (AI accelerator): {'yes' if cpu['nnpa'] else 'no'}")
    for lib in ("libzdnn.so", "libzdnn.so.0"):
        try:
            import ctypes
            ctypes.CDLL(lib)
            print(f"zDNN library: {lib} found")
            break
        except OSError:
            pass
    else:
        print("zDNN library: not found (Snap ML needs it to use the AI accelerator)")

    localities = load_localities()
    service = ms.ModelService(localities)
    for message in service.status["errors"]:
        print("Note:", message)
    for name, info in service.status["backends"].items():
        print(f"{name}: {info}")

    try:
        import numpy as np
        import snapml
        from snapml import BoostingMachineClassifier
    except ImportError as exc:
        print(f"Snap ML not importable ({exc}); skipping the Snap ML checks.")
    else:
        print(f"\nSnap ML {getattr(snapml, '__version__', '')}: input type x tree format")
        reference = service.backends["xgboost"](service.X) if "xgboost" in service.backends else None
        stored = np.array(service.stored)
        for input_type in ("xgb_json", "xgb"):
            for tree_format in ("zdnn_tensors", "compress_trees", "ensemble", "auto"):
                label = f"{input_type:9s} {tree_format:15s}"
                model = BoostingMachineClassifier()
                try:
                    model.import_model(str(ms.MODEL_PATH), input_type=input_type, tree_format=tree_format)
                    started = time.perf_counter()
                    for _ in range(20):
                        preds = model.predict_proba(service.X)[:, 1]
                    ms_per_batch = (time.perf_counter() - started) * 1000 / 20
                    diff = float(np.max(np.abs(preds - (reference if reference is not None else stored))))
                    print(f"  {label} OK  {ms_per_batch:8.3f} ms per {len(service.X)} rows, "
                          f"max diff vs {'XGBoost' if reference is not None else 'stored scores'} {diff:.2e}")
                except Exception as exc:
                    print(f"  {label} failed: {exc.__class__.__name__}: {exc}")

    if not service.backends:
        print("\nNo model backend loaded in the app's own loader.")
        return 1
    bench = service.benchmark(repeats=30)
    print("\nBenchmark (median of 30 batches):")
    for name, r in bench["results"].items():
        print(f"  {name:8s} {r['median_ms']:8.3f} ms per batch, {r['per_locality_us']:6.2f} us per locality")
    return 0


if __name__ == "__main__":
    sys.exit(main())
