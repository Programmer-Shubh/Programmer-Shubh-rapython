import logging
from pathlib import Path
from typing import Optional, Dict, List
import numpy as np
import json

logger = logging.getLogger(__name__)


class XGBoostPooledModel:
    """Pooled XGBoost model trained on all symbols jointly.

    Features are ~80 TA + 50 cross-sectional rank features.
    Uses lazy loading — model only loaded on first predict call.

    ONNX export for fast inference. If ONNX unavailable, falls back to native.
    """

    def __init__(self, model_path: Optional[Path] = None):
        self.model_path = model_path or Path(__file__).resolve().parent.parent / "models" / "xgboost_pooled"
        self._model = None
        self._onnx_session = None
        self._loaded = False
        self._feature_names: List[str] = []
        self._calibrator = None

    def load(self) -> bool:
        """Lazy load — call before predict. Returns True if loaded."""
        if self._loaded:
            return True

        # Try ONNX first
        onnx_path = self.model_path.with_suffix(".onnx")
        if onnx_path.exists():
            try:
                import onnxruntime as ort
                self._onnx_session = ort.InferenceSession(str(onnx_path))
                self._loaded = True
                logger.info(f"ONNX model loaded: {onnx_path}")
                return True
            except Exception as e:
                logger.warning(f"ONNX load failed ({e}), trying native...")

        # Native XGBoost
        native_path = self.model_path.with_suffix(".json")
        if native_path.exists():
            try:
                import xgboost as xgb
                self._model = xgb.Booster()
                self._model.load_model(str(native_path))
                self._loaded = True
                logger.info(f"XGBoost native model loaded: {native_path}")
                return True
            except Exception as e:
                logger.warning(f"Native XGBoost load failed: {e}")

        # Load feature names and calibrator
        meta_path = self.model_path.with_name(self.model_path.name + "_meta.json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                self._feature_names = meta.get("features", [])
            except Exception:
                pass

        cal_path = self.model_path.with_name(self.model_path.name + "_calibration.json")
        if cal_path.exists():
            try:
                self._calibrator = json.loads(cal_path.read_text())
            except Exception:
                pass

        logger.warning(f"Model not found at {self.model_path}.* — will return empty predictions")
        return False

    def predict(self, features: np.ndarray) -> np.ndarray:
        """Predict probabilities (0-1) for a batch of feature vectors.

        Returns array of probabilities. If model not loaded, returns zeros.
        """
        if not self._loaded:
            if not self.load():
                return np.zeros(len(features), dtype=float)

        if self._onnx_session is not None:
            input_name = self._onnx_session.get_inputs()[0].name
            probs = self._onnx_session.run(None, {input_name: features.astype(np.float32)})[0]
            if probs.ndim == 2:
                probs = probs[:, 1]
        elif self._model is not None:
            import xgboost as xgb
            dmat = xgb.DMatrix(features)
            probs = self._model.predict(dmat)
        else:
            return np.zeros(len(features), dtype=float)

        # Isotonic calibration
        if self._calibrator is not None:
            probs = _calibrate(probs, self._calibrator)

        return np.clip(probs, 0.0, 1.0)

    def save(self, model, feature_names: List[str], calib_params: Optional[Dict] = None):
        """Save model, feature names, and calibration params."""
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        native_path = self.model_path.with_suffix(".json")
        model.save_model(str(native_path))
        logger.info(f"Model saved: {native_path}")

        meta = {"features": feature_names}
        meta_path = self.model_path.with_name(self.model_path.name + "_meta.json")
        meta_path.write_text(json.dumps(meta, indent=2))

        if calib_params:
            cal_path = self.model_path.with_name(self.model_path.name + "_calibration.json")
            cal_path.write_text(json.dumps(calib_params, indent=2))

        self._feature_names = feature_names
        self._calibrator = calib_params

    def export_onnx(self):
        """Export native model to ONNX for faster inference."""
        if self._model is None:
            logger.warning("No native model to export")
            return False
        try:
            from skl2onnx import convert_xgboost
            from skl2onnx.common.data_types import FloatTensorType
            n_features = len(self._feature_names) if self._feature_names else 80
            initial_type = [("float_input", FloatTensorType([None, n_features]))]
            onx = convert_xgboost(self._model, "xgboost_pooled", initial_type)
            onnx_path = self.model_path.with_suffix(".onnx")
            with open(onnx_path, "wb") as f:
                f.write(onx.SerializeToString())
            logger.info(f"ONNX model exported: {onnx_path}")
            return True
        except Exception as e:
            logger.warning(f"ONNX export failed: {e}")
            return False


def _calibrate(probs: np.ndarray, calib: Dict) -> np.ndarray:
    """Simple isotonic calibration using bin mapping."""
    bins = calib.get("bins", [])
    if not bins:
        return probs
    calibrated = np.zeros_like(probs)
    for i, p in enumerate(probs):
        for bin_def in bins:
            if bin_def["lo"] <= p <= bin_def["hi"]:
                calibrated[i] = bin_def["calibrated"]
                break
        else:
            calibrated[i] = p
    return calibrated
