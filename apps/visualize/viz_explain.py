"""How each prediction result was reached: the method, the steps and the formula, in plain words.

Attached to every prediction by the engine (metrics: ``formula`` and ``meaning``; charts: the
explanation's ``method``, ``steps`` and ``formula``) so the user can see how a number or a chart was
computed without reading the code. Plain text with Unicode maths (Σ, √, ŷ), no LaTeX.
"""

from __future__ import annotations

from typing import Any

# metric key -> (formula, what the number means)
METRICS: dict[str, tuple[str, str]] = {
    "mae": (
        "MAE = (1/n) × Σ |actualᵢ − predictedᵢ|",
        "The average size of a miss, in the measure's own units. Smaller is better.",
    ),
    "baseline_mae": (
        "Baseline MAE = (1/n) × Σ |actualᵢ − previous periodᵢ|",
        "The error of the naive guess 'the next value equals the last one'. A useful model beats it.",
    ),
    "mape": (
        "MAPE = (100% / n) × Σ |actualᵢ − predictedᵢ| / |actualᵢ|",
        "The average miss as a percentage of the actual value. 10% means forecasts are off by a tenth.",
    ),
    "rmse": (
        "RMSE = √( (1/n) × Σ (actualᵢ − predictedᵢ)² )",
        "Like MAE but punishes large misses more. In the measure's units; smaller is better.",
    ),
    "r2": (
        "R² = 1 − Σ (actualᵢ − predictedᵢ)² / Σ (actualᵢ − average)²",
        "The share of the variation the model explains: 1 is perfect, 0 is no better than the average.",
    ),
    "accuracy": (
        "Accuracy = correct predictions / all predictions",
        "How often the predicted category is right, on rows the model did not train on.",
    ),
    "balanced_accuracy": (
        "Balanced accuracy = average over categories of (correct in category / rows in category)",
        "Accuracy that is fair when some categories are rare.",
    ),
    "f1": (
        "F1 = 2 × precision × recall / (precision + recall), averaged over categories",
        "Balances catching the right rows (recall) with not raising false alarms (precision).",
    ),
    "auc": (
        "AUC = probability that a random positive row scores higher than a random negative row",
        "0.5 is guessing, 1.0 is perfect separation.",
    ),
    "silhouette": (
        "Silhouette = average of (b − a) / max(a, b); a = distance to own group, b = to nearest other group",
        "How clearly the groups are separated: near 1 is clear, near 0 is overlapping.",
    ),
    "calinski_harabasz": (
        "Calinski–Harabasz = (between-group spread / (k − 1)) / (within-group spread / (n − k))",
        "Higher means tighter, better separated groups.",
    ),
    "davies_bouldin": (
        "Davies–Bouldin = average over groups of the worst (spreadᵢ + spreadⱼ) / distance(i, j)",
        "Lower means the groups are compact and far apart.",
    ),
    "total_change": (
        "Change = (last period − first period) / |first period| × 100%",
        "How much the measure grew or shrank over the whole history.",
    ),
    "avg_growth": (
        "Average growth = ((last / first)^(1 / periods) − 1) × 100%",
        "The steady rate per period that would take the first value to the last (compound growth).",
    ),
    "latest_growth": (
        "Latest change = (last period − previous period) / |previous period| × 100%",
        "The most recent movement.",
    ),
    "yoy": (
        "Year-on-year = (latest period − same period a year earlier) / |year earlier| × 100%",
        "Growth that ignores seasonal ups and downs.",
    ),
    "slope": (
        "Trend per period = slope b of the best straight line  value = a + b × period",
        "How much the measure adds (or loses) per period on average, fitted by least squares.",
    ),
    "seasonality": (
        "Seasonality strength = max(0, 1 − Var(remainder) / Var(seasonal + remainder))",
        "0 means no repeating pattern; close to 1 means a strong one (from a seasonal decomposition).",
    ),
    "volatility": (
        "Coefficient of variation = standard deviation / average × 100%",
        "How much the measure swings around its average.",
    ),
    "anomalies": (
        "Unusual when |value − expected| / (1.4826 × median absolute deviation) > threshold",
        "Periods or rows far from what the pattern predicts, measured with a robust z-score.",
    ),
    "total": (
        "Expected total = Σ forecastₜ over the forecast periods",
        "The sum of the forecast values.",
    ),
    "growth": (
        "Growth = (expected total − total of the last same-length period) / |that total| × 100%",
        "How the coming periods compare with the most recent ones.",
    ),
    "next": ("Next period = forecast for the first future period", "The model's best single guess."),
    "optimistic": (
        "Best case = Σ upper bound of the 80% band",
        "If things go well: 1 in 10 chance of doing better.",
    ),
    "pessimistic": (
        "Worst case = Σ lower bound of the 80% band",
        "If things go badly: 1 in 10 chance of doing worse.",
    ),
    "top_lift": (
        "Lift = outcome rate in the segment / outcome rate overall",
        "2.0 means the outcome is twice as common in that segment.",
    ),
    "share": ("Share = rows in the group / all rows × 100%", "How big the group is."),
}

# model id -> how it works
MODELS: dict[str, str] = {
    "holt_winters": "Holt–Winters exponential smoothing: level, trend and season are updated each period, "
    "recent periods weighing more (ŷₜ₊ₕ = levelₜ + h × trendₜ + seasonₜ₊ₕ₋ₘ).",
    "arima": "ARIMA/SARIMAX: each value is explained by earlier values and earlier errors, after removing "
    "trend and season by differencing.",
    "linear_trend": "Linear trend + seasonality: a straight line through time plus an average effect for "
    "each season (ŷ = a + b × t + seasonₘ).",
    "robust": "Robust trend: moving averages, least-squares slope, seasonal decomposition and robust "
    "z-scores to flag unusual periods.",
    "segments": "Segments + random forest: compares the outcome rate in every segment with the overall "
    "rate (lift), and ranks columns by how much a random forest relies on them.",
    "linear": "Linear regression: ŷ = b₀ + b₁x₁ + … + bₖxₖ, coefficients chosen to minimise Σ (y − ŷ)².",
    "ridge": "Ridge regression: linear regression with a penalty on large coefficients, "
    "minimising Σ (y − ŷ)² + λ Σ b², which is steadier with many columns.",
    "random_forest": "Random forest: hundreds of decision trees on random samples of rows and columns; the "
    "prediction is their average (numbers) or majority vote (categories).",
    "gradient_boosting": "Gradient boosting: small decision trees added one after another, each correcting "
    "the errors of the trees before it.",
    "logistic": "Logistic regression: P(category) = 1 / (1 + e^−(b₀ + b₁x₁ + … + bₖxₖ)).",
    "kmeans": "k-means: puts every row in the group with the nearest centre, then moves each centre to the "
    "average of its rows, until nothing changes.",
    "gaussian_mixture": "Gaussian mixture: groups are bell-shaped clouds; each row gets the group it most "
    "probably belongs to.",
    "agglomerative": "Hierarchical (Ward): starts with every row alone and repeatedly joins the two groups "
    "whose merge increases the within-group spread the least.",
    "isolation_forest": "Isolation forest: random splits isolate unusual rows in fewer steps than normal "
    "ones; fewer steps means a higher anomaly score.",
    "zscore": "Robust z-score: z = (value − median) / (1.4826 × median absolute deviation); "
    "|z| above the threshold is unusual.",
    "lof": "Local outlier factor: compares how crowded a row's neighbourhood is with its neighbours' "
    "neighbourhoods; rows in much emptier areas are unusual.",
}

# chart id -> (method, steps, formula)
CHARTS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "forecast": (
        "Time-series forecast with uncertainty bands",
        (
            "Rows are grouped by period (day, week, month…) and combined with the chosen aggregation.",
            "The last periods are held back; each model forecasts them and is scored on its misses "
            "(backtest).",
            "The best model is refitted on all history and projected forward.",
            "Bands come from the spread of past forecast errors: 80% and 95% of outcomes fall inside.",
        ),
        "band = forecast ± z × σ(errors) × √h   (z = 1.28 for 80%, 1.96 for 95%; h = periods ahead)",
    ),
    "forecast_by_segment": (
        "One forecast per segment",
        ("The forecast steps are repeated for each value of the split column.",),
        "Segment forecastₜ = model fitted on that segment's history",
    ),
    "scenarios": (
        "Best, expected and worst case",
        ("Expected = sum of the forecast.", "Best and worst = sums of the upper and lower 80% bounds."),
        "Expected = Σ ŷₜ;  Best = Σ upperₜ;  Worst = Σ lowerₜ",
    ),
    "trend": (
        "Trend line and moving average",
        (
            "Values are aggregated per period.",
            "A moving average smooths short-term noise.",
            "A straight line is fitted by least squares; points far from the expected value are marked.",
        ),
        "moving averageₜ = (valueₜ + … + valueₜ₋ₖ₊₁) / k;  trend = a + b × t",
    ),
    "growth": (
        "Period-over-period growth",
        ("Each period is compared with the one before it.",),
        "growthₜ = (valueₜ − valueₜ₋₁) / |valueₜ₋₁| × 100%",
    ),
    "year_over_year": (
        "Year-on-year comparison",
        ("Each period is compared with the same period one year earlier.",),
        "YoYₜ = (valueₜ − valueₜ₋₁₂) / |valueₜ₋₁₂| × 100%  (monthly data)",
    ),
    "seasonal_profile": (
        "Seasonal profile",
        ("Values are averaged per season position (month of year, weekday…) after removing the trend.",),
        "seasonₘ = average(value − trend) over all periods in position m",
    ),
    "segment_growth": (
        "Growth by segment",
        ("The growth formula is applied to each segment's own history.",),
        "growth = (last − first) / |first| × 100%",
    ),
    "influencers": (
        "Top influencers",
        (
            "For every column value (segment) the outcome rate inside it is compared with the overall rate.",
            "Segments with too few rows are skipped so chance does not look like a pattern.",
        ),
        "lift = rate(outcome | segment) / rate(outcome)",
    ),
    "top_factor": (
        "The strongest factor",
        ("The column whose segments differ the most in outcome rate, shown segment by segment.",),
        "rate(outcome | segment) for each segment",
    ),
    "feature_importance": (
        "Feature importance",
        (
            "The model is scored on rows it did not train on.",
            "One column at a time is shuffled; the drop in score shows how much the model relied on it.",
        ),
        "importance(column) = score(original) − score(column shuffled)",
    ),
    "predicted_vs_actual": (
        "Predicted vs actual",
        (
            "Rows are split: the model trains on most and is tested on the rest (cross-validation).",
            "Each dot is a test row; on the diagonal the prediction is exact.",
        ),
        "errorᵢ = actualᵢ − predictedᵢ",
    ),
    "residual_plot": (
        "Errors (residuals)",
        ("The miss of every test row against its prediction. A good model has errors centred on zero.",),
        "residualᵢ = actualᵢ − predictedᵢ",
    ),
    "what_if": (
        "What-if",
        ("One input is varied over its range while the others stay at typical values; the model predicts.",),
        "ŷ(x) = model(x, other inputs fixed)",
    ),
    "confusion_matrix": (
        "Confusion matrix",
        ("Counts of test rows by actual category (rows) and predicted category (columns).",),
        "cell(i, j) = rows with actual i predicted as j",
    ),
    "roc_curve": (
        "ROC curve",
        ("For every threshold on the predicted probability, the share of positives caught vs false alarms.",),
        "TPR = TP / (TP + FN);  FPR = FP / (FP + TN)",
    ),
    "pr_curve": (
        "Precision–recall curve",
        ("For every threshold, precision against recall.",),
        "precision = TP / (TP + FP);  recall = TP / (TP + FN)",
    ),
    "cluster_map": (
        "Segments map",
        (
            "Numbers are scaled to comparable ranges; the rows are projected onto two axes (PCA) "
            "to draw them.",
        ),
        "zᵢ = (xᵢ − mean) / std;  axes = first two principal components",
    ),
    "kmeans_clusters": (
        "Groups found",
        ("Every row is assigned to its nearest group centre.",),
        "group(row) = argminₖ ‖row − centreₖ‖",
    ),
    "cluster_profile": (
        "What makes each group different",
        ("Each group's average is compared with the overall average for every column.",),
        "difference = (group mean − overall mean) / overall std",
    ),
    "cluster_sizes": ("Group sizes", ("Rows counted per group.",), "size = rows in the group"),
    "silhouette": (
        "Silhouette per row",
        ("For every row: how close it is to its own group compared with the nearest other group.",),
        "s = (b − a) / max(a, b)",
    ),
    "anomaly_map": (
        "Unusual rows",
        ("Every row gets an anomaly score; the highest-scoring rows are marked.",),
        "unusual when score > threshold",
    ),
    "anomaly_scores": (
        "Anomaly scores",
        ("The score of every row, highest first.",),
        "score = model's anomaly score (see the method above)",
    ),
}


def attach(task_id: str, model_name: str, metrics: list[dict[str, Any]], charts: list[dict[str, Any]]) -> str:
    """Adds formulas to metrics and methods to charts; returns how the model works."""
    for metric in metrics:
        known = METRICS.get(str(metric.get("key")))
        if known:
            metric.setdefault("formula", known[0])
            metric.setdefault("meaning", known[1])
    for chart in charts:
        known_chart = CHARTS.get(str(chart.get("id")))
        if known_chart:
            explanation = chart.setdefault("explanation", {})
            explanation.setdefault("method", known_chart[0])
            explanation.setdefault("steps", list(known_chart[1]))
            explanation.setdefault("formula", known_chart[2])
    return MODELS.get(model_name, "")
