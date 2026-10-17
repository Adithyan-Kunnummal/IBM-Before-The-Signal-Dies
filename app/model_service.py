"""Live inference for the reported-outage model, with XGBoost and IBM Snap ML.

Both libraries load the same trained model (models/xgb_pu_outage.json). Snap ML is
asked for its zDNN tree format first, which runs inference on the Telum on-chip AI
accelerator (Integrated Accelerator for AI) when the processor exposes it ("nnpa" in
/proc/cpuinfo). Every backend is checked against the stored scores at start-up, and
the status reports exactly which backend and tree format loaded.

numpy, xgboost and snapml are optional: whatever is missing is reported, and the rest
of the app keeps working.
"""
import csv
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / "models" / "xgb_pu_outage.json"
FEATURES_CSV = ROOT / "data" / "processed" / "wa_outage_modelling.csv"
QUALITY_JSON = ROOT / "data" / "processed" / "outage_data_quality.json"

# Snap ML engines: tree format, backend name, label. "compress_trees" runs on the CPU
# cores; "zdnn_tensors" runs on the Telum on-chip AI accelerator and is only loaded when
# the processor reports nnpa (elsewhere Snap ML would silently fall back to the CPU).
SNAPML_ENGINES = [("compress_trees", "snapml_cpu", "Snap ML"),
                  ("zdnn_tensors", "snapml_telum", "Snap ML on Telum")]
AGREEMENT_TOLERANCE = 1e-3


def snapml_model_files():
    """The trained model, then copies in older XGBoost JSON layouts for Snap ML.

    Snap ML 1.17 reads XGBoost JSON from before XGBoost 3, which wrote base_score as a
    plain number and had no categorical "cats" block; with the 3.x layout it reports
    "no objective defined". The copies change only the layout, not the trees, and every
    backend is still checked against the stored scores.
    """
    yield "original", MODEL_PATH
    model = json.loads(MODEL_PATH.read_text())
    learner = model["learner"]
    param = learner["learner_model_param"]
    param["base_score"] = param["base_score"].strip("[]")
    param.pop("num_target", None)
    booster = learner["gradient_booster"]["model"]
    booster.pop("cats", None)
    booster.pop("iteration_indptr", None)
    model["version"] = [2, 1, 4]
    folder = Path(tempfile.mkdtemp(prefix="snapml-model-"))
    yield "xgboost-2-layout", _write(folder / "xgb_v2.json", model)
    # Older still: the objective repeated as a training parameter and attribute.
    learner["learner_train_param"] = {"booster": "gbtree", "objective": learner["objective"]["name"]}
    learner["attributes"] = {"objective": learner["objective"]["name"]}
    model["version"] = [1, 7, 6]
    yield "xgboost-1-layout", _write(folder / "xgb_v1.json", model)


def _write(path, model):
    path.write_text(json.dumps(model, separators=(",", ":")))
    return path


def library_version(name):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return ""


def cpu_info():
    info = {"machine": platform.machine(), "nnpa": False, "machine_type": ""}
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return info
    for line in text.splitlines():
        if line.startswith("features"):
            info["nnpa"] = "nnpa" in line.split(":", 1)[1].split()
        if "machine =" in line and not info["machine_type"]:
            info["machine_type"] = line.rsplit("=", 1)[1].strip()
    return info


class ModelService:
    def __init__(self, localities):
        self.cpu = cpu_info()
        self.status = {"available": False, "backends": {}, "errors": [], "cpu": self.cpu}
        self.backends = {}          # name -> predict(X) -> list of probabilities
        self.stored = [loc["model_score"] for loc in localities]
        self.sals = [loc["sal"] for loc in localities]
        try:
            import numpy as np
            self.np = np
        except ImportError:
            self.status["errors"].append("numpy is not installed, so live model inference is off.")
            return

        self.features = json.loads(QUALITY_JSON.read_text())["model_features"]
        with open(FEATURES_CSV, newline="", encoding="utf-8") as handle:
            rows = {r["sal_code_2021"]: r for r in csv.DictReader(handle)}
        self.X = np.array([[float(rows[s][f]) for f in self.features] for s in self.sals], dtype=np.float32)
        self._load_xgboost()
        self._load_snapml()
        self._verify()
        self.status["available"] = bool(self.backends)

    # ---------- loading ----------
    def _load_xgboost(self):
        try:
            import xgboost as xgb
        except ImportError:
            self.status["errors"].append("xgboost is not installed.")
            return
        booster = xgb.Booster()
        booster.load_model(str(MODEL_PATH))
        self.xgb, self.booster = xgb, booster

        def predict(X):
            return booster.predict(xgb.DMatrix(X, feature_names=self.features))
        self.backends["xgboost"] = predict
        self.status["backends"]["xgboost"] = {"label": "XGBoost", "library": f"xgboost {xgb.__version__}",
                                          "accelerator": "CPU"}

    def _load_snapml(self):
        try:
            import snapml
            from snapml import BoostingMachineClassifier
        except ImportError:
            self.status["errors"].append("snapml is not installed (pip install snapml on LinuxONE).")
            return
        version = f"snapml {library_version('snapml')}".strip()
        errors = []
        for tree_format, name, label in SNAPML_ENGINES:
            if tree_format == "zdnn_tensors" and not self.cpu["nnpa"]:
                continue
            for layout, path in snapml_model_files():
                model = BoostingMachineClassifier()
                try:
                    model.import_model(str(path), input_type="xgb_json", tree_format=tree_format)
                    # One prediction proves the format works on this machine.
                    model.predict_proba(self.X[:2])
                except Exception as exc:  # Snap ML raises various errors per format
                    errors.append(f"{tree_format}/{layout}: {exc.__class__.__name__}: {exc}")
                    continue

                def predict(X, m=model):
                    return m.predict_proba(X)[:, 1]
                self.backends[name] = predict
                self.status["backends"][name] = {
                    "label": label, "library": version, "model_layout": layout, "tree_format": tree_format,
                    "accelerator": "Telum AI accelerator (zDNN)" if tree_format == "zdnn_tensors" else "CPU",
                }
                break
        if errors and not any(n.startswith("snapml") for n in self.backends):
            self.status["errors"].append(f"Snap ML could not load the model ({'; '.join(errors)}).")

    def _verify(self):
        stored = self.np.array(self.stored, dtype=float)
        for name, predict in list(self.backends.items()):
            diff = float(self.np.max(self.np.abs(predict(self.X) - stored)))
            info = self.status["backends"][name]
            # Stored scores are rounded to 4 decimals in the API; allow for that.
            info["max_difference_vs_stored"] = round(diff, 6)
            info["agrees_with_stored"] = diff <= AGREEMENT_TOLERANCE
            if not info["agrees_with_stored"]:
                self.status["errors"].append(f"{name} predictions differ from the stored scores by up to {diff:.4f}.")

    # ---------- inference ----------
    def score(self, index):
        """Live score for one locality from every backend, plus XGBoost feature contributions."""
        if not self.backends:
            return None
        row = self.X[index:index + 1]
        out = {"scores": {}, "contributions": []}
        for name, predict in self.backends.items():
            started = time.perf_counter()
            value = float(predict(row)[0])
            out["scores"][name] = {"score": round(value, 4),
                                   "ms": round((time.perf_counter() - started) * 1000, 3)}
        if "xgboost" in self.backends:
            contrib = self.booster.predict(self.xgb.DMatrix(row, feature_names=self.features), pred_contribs=True)[0]
            pairs = sorted(zip(self.features, contrib[:-1], row[0]), key=lambda t: -abs(t[1]))[:4]
            out["contributions"] = [{"feature": f, "value": round(float(v), 4), "contribution": round(float(c), 3)}
                                    for f, c, v in pairs]
        return out

    def benchmark(self, repeats=30):
        """Median time to score all localities in one batch, per backend."""
        if not self.backends:
            return None
        results = {}
        for name, predict in self.backends.items():
            predict(self.X)  # warm up
            times = []
            for _ in range(repeats):
                started = time.perf_counter()
                predict(self.X)
                times.append((time.perf_counter() - started) * 1000)
            results[name] = {"median_ms": round(statistics.median(times), 3),
                             "per_locality_us": round(statistics.median(times) * 1000 / len(self.X), 2)}
        return {"rows": len(self.X), "repeats": repeats, "results": results,
                "backends": self.status["backends"], "cpu": self.cpu}
