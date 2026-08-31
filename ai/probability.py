from typing import Dict, Optional

from core.config import LEVEL_PROXIMITY_PCT

WEIGHTS = {
    "rvol_spike": 0.15,
    "obv_divergence": 0.20,
    "sr_confluence": 0.15,
    "orderflow": 0.10,
    "rsi_oversold": 0.15,
    "rsi_overbought": 0.15,
    "vwap_reclaim": 0.10,
    "mirror_obv_confirmation": 0.10,
    "atr_squeeze": 0.05,
}


def compute_buy_probability(
    obv_features: Dict,
    volume_features: Dict,
    sr_result: Dict,
    confluence: Dict,
    indicators: Optional[Dict] = None,
    orderflow: Optional[Dict] = None,
) -> Dict:
    score = 0.0
    signals = []
    reasons = []

    rvol = volume_features.get("rvol", 0)
    if rvol > 2.0:
        score += WEIGHTS["rvol_spike"]
        signals.append("rvol_spike")
        reasons.append(f"RVOL {rvol:.1f}x > 2x")

    divergence = obv_features.get("obv_divergence", "none")
    if divergence == "bullish_regular":
        score += WEIGHTS["obv_divergence"]
        signals.append("obv_divergence")
        reasons.append("Bullish OBV divergence detected")
    elif divergence == "bullish_hidden":
        score += WEIGHTS["obv_divergence"] * 0.7
        signals.append("hidden_bullish_divergence")
        reasons.append("Hidden bullish OBV divergence")

    rsi = (indicators or {}).get("rsi", 50)
    if rsi and rsi < 35:
        score += WEIGHTS["rsi_oversold"]
        signals.append("rsi_oversold")
        reasons.append(f"RSI {rsi:.0f} < 35 (oversold)")

    nearest_type = confluence.get("nearest_type", "")
    nearest_dist_pct = confluence.get("nearest_dist_pct", 100)
    if nearest_type == "support" and nearest_dist_pct < LEVEL_PROXIMITY_PCT:
        score += WEIGHTS["sr_confluence"]
        signals.append("at_support")
        reasons.append(f"Price at support ({nearest_dist_pct:.1f}% away)")

    vwap = volume_features.get("price_vs_vwap", "")
    if vwap == "above":
        score += WEIGHTS["vwap_reclaim"]
        signals.append("vwap_reclaim")
        reasons.append("Price above VWAP (bullish)")

    obv_trend = obv_features.get("obv_trend", "")
    mirror_obv = obv_features.get("mirror_obv", 0)
    if obv_trend == "rising" and mirror_obv > 0:
        score += WEIGHTS["mirror_obv_confirmation"]
        signals.append("mirror_obv_bullish")
        reasons.append("Mirror OBV confirming uptrend")

    is_absorption = volume_features.get("is_absorption", False)
    if is_absorption:
        score += WEIGHTS["atr_squeeze"]
        signals.append("absorption")
        reasons.append("Volume absorption at level")

    # Order flow features
    of = orderflow or {}
    of_cvd = of.get("cvd", 0)
    of_absorption = of.get("absorption", False)
    of_rejection = of.get("rejection", False)
    of_touches = of.get("touches", 0)
    of_delta = of.get("delta_at_level", 0)
    if of_cvd > 0 and of_touches >= 2:
        score += WEIGHTS["orderflow"]
        signals.append("orderflow_bullish")
        reasons.append(f"Order flow: CVD+{of_cvd:.0f}, {of_touches} touches at level")
    elif of_absorption:
        score += WEIGHTS["orderflow"] * 0.7
        signals.append("orderflow_absorption")
        reasons.append("Order flow: absorption at level")
    elif of_rejection:
        score += WEIGHTS["orderflow"] * 0.5
        signals.append("orderflow_rejection")
        reasons.append("Order flow: rejection at level")

    if "bearish" in divergence:
        score -= 0.10
        signals.append("conflict_bearish_divergence")
        reasons.append("Bearish divergence contradicts buy signal")
    elif obv_trend == "falling":
        score -= 0.05
        signals.append("conflict_obv_falling")
        reasons.append("Falling OBV contradicts buy signal")

    score = min(max(score, 0.0), 1.0)
    confidence = (
        "HIGH" if score >= 0.75 else
        "MEDIUM" if score >= 0.50 else
        "LOW"
    )

    alert_level = (
        "P0_FLASH" if score >= 0.85 else
        "P1_NOTIFY" if score >= 0.70 else
        "P2_WATCH" if score >= 0.50 else
        "P3_PASSIVE"
    )

    return {
        "buy_probability": round(score * 100, 1),
        "confidence": confidence,
        "alert_level": alert_level,
        "signals": signals,
        "reasons": reasons,
    }


def compute_sell_probability(
    obv_features: Dict,
    volume_features: Dict,
    sr_result: Dict,
    confluence: Dict,
    indicators: Optional[Dict] = None,
    orderflow: Optional[Dict] = None,
) -> Dict:
    score = 0.0
    signals = []
    reasons = []

    rvol = volume_features.get("rvol", 0)
    if rvol > 2.0:
        score += WEIGHTS["rvol_spike"]
        signals.append("rvol_spike")
        reasons.append(f"RVOL {rvol:.1f}x > 2x")

    divergence = obv_features.get("obv_divergence", "none")
    if divergence == "bearish_regular":
        score += WEIGHTS["obv_divergence"]
        signals.append("obv_divergence")
        reasons.append("Bearish OBV divergence detected")
    elif divergence == "bearish_hidden":
        score += WEIGHTS["obv_divergence"] * 0.7
        signals.append("hidden_bearish_divergence")
        reasons.append("Hidden bearish OBV divergence")

    rsi = (indicators or {}).get("rsi", 50)
    if rsi and rsi > 65:
        score += WEIGHTS["rsi_overbought"]
        signals.append("rsi_overbought")
        reasons.append(f"RSI {rsi:.0f} > 65 (overbought)")

    nearest_type = confluence.get("nearest_type", "")
    nearest_dist_pct = confluence.get("nearest_dist_pct", 100)
    if nearest_type == "resistance" and nearest_dist_pct < LEVEL_PROXIMITY_PCT:
        score += WEIGHTS["sr_confluence"]
        signals.append("at_resistance")
        reasons.append(f"Price at resistance ({nearest_dist_pct:.1f}% away)")

    vwap = volume_features.get("price_vs_vwap", "")
    if vwap == "below":
        score += WEIGHTS["vwap_reclaim"]
        signals.append("vwap_breakdown")
        reasons.append("Price below VWAP (bearish)")

    obv_trend = obv_features.get("obv_trend", "")
    mirror_obv = obv_features.get("mirror_obv", 0)
    if obv_trend == "falling" and mirror_obv < 0:
        score += WEIGHTS["mirror_obv_confirmation"]
        signals.append("obv_falling")
        reasons.append("OBV falling (distribution)")

    # Order flow features
    of = orderflow or {}
    of_cvd = of.get("cvd", 0)
    of_absorption = of.get("absorption", False)
    of_rejection = of.get("rejection", False)
    of_touches = of.get("touches", 0)
    of_delta = of.get("delta_at_level", 0)
    if of_cvd < 0 and of_touches >= 2:
        score += WEIGHTS["orderflow"]
        signals.append("orderflow_bearish")
        reasons.append(f"Order flow: CVD{of_cvd:.0f}, {of_touches} touches at level")
    elif of_rejection:
        score += WEIGHTS["orderflow"] * 0.7
        signals.append("orderflow_rejection")
        reasons.append("Order flow: rejection at level")
    elif of_absorption:
        score += WEIGHTS["orderflow"] * 0.5
        signals.append("orderflow_absorption")
        reasons.append("Order flow: absorption at level")

    if "bullish" in divergence:
        score -= 0.10
        signals.append("conflict_bullish_divergence")
        reasons.append("Bullish divergence contradicts sell signal")
    elif obv_trend == "rising":
        score -= 0.05
        signals.append("conflict_obv_rising")
        reasons.append("Rising OBV contradicts sell signal")

    score = min(max(score, 0.0), 1.0)
    confidence = (
        "HIGH" if score >= 0.75 else
        "MEDIUM" if score >= 0.50 else
        "LOW"
    )
    alert_level = (
        "P0_FLASH" if score >= 0.85 else
        "P1_NOTIFY" if score >= 0.70 else
        "P2_WATCH" if score >= 0.50 else
        "P3_PASSIVE"
    )

    return {
        "sell_probability": round(score * 100, 1),
        "confidence": confidence,
        "alert_level": alert_level,
        "signals": signals,
        "reasons": reasons,
    }
