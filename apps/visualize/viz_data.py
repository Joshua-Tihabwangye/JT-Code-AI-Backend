"""Data preparation and modelling helpers shared by the isolated visualization engine.

Loaded by file path inside the sandbox (``python -I``); imports nothing from Django.
"""

from __future__ import annotations

import base64
import math
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _sibling(name: str, filename: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, filename)
    if spec is None or spec.loader is None:  # pragma: no cover - fixed, packaged paths
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


base = _sibling("jt_analytics_engine", str(_HERE.parent / "analytics" / "engine.py"))
catalog = _sibling("jt_visualize_catalog", str(_HERE / "catalog.py"))
AnalysisError = base.AnalysisError

MISSING = "(missing)"
MAX_CATEGORIES = 60
MODEL_ROWS = 20_000
GRAIN_FREQ = {"D": "D", "W": "W", "M": "MS", "Q": "QS", "Y": "YS"}
SEASON = {"D": 7, "W": 52, "M": 12, "Q": 4}


def load_frame(request: dict[str, Any], limits: dict[str, Any]) -> Any:
    content = base64.b64decode(request.get("data") or "")
    frame = base.dataframe_from_bytes(content, mime_type=request.get("mime_type", "text/csv"), limits=limits)
    return frame


def column_kinds(frame: Any) -> tuple[dict[str, Any], dict[str, str]]:
    profile = base.profile_frame(frame)
    kinds = {entry["name"]: entry.get("kind", "categorical") for entry in profile["columnProfiles"]}
    return profile, kinds


def typed(frame: Any, kinds: dict[str, str]) -> Any:
    """Convert profiled datetime columns to real datetimes (other columns are unchanged)."""
    frame = frame.copy()
    for column, kind in kinds.items():
        if kind == "datetime":
            frame[column] = _dates(frame[column])
    return frame


def _dates(series: Any) -> Any:
    import pandas as pd

    if pd.api.types.is_datetime64_any_dtype(series):
        if getattr(series.dt, "tz", None) is not None:
            return series.dt.tz_convert(None)
        return series
    if pd.api.types.is_numeric_dtype(series):
        values = pd.to_numeric(series, errors="coerce")
        if values.dropna().between(1000, 3000).all() and (values.dropna() % 1 == 0).all():
            return pd.to_datetime(values.astype("Int64").astype(str), format="%Y", errors="coerce")
        raise AnalysisError(f"{series.name} holds numbers, not dates.")
    parsed = pd.to_datetime(series, errors="coerce", format="mixed", utc=True)
    return parsed.dt.tz_convert(None)


def _numeric(frame: Any, column: str) -> Any:
    import pandas as pd

    return pd.to_numeric(frame[column], errors="coerce")


def _label(series: Any) -> Any:
    """Categorical labels as strings, with integer-valued numbers printed without '.0'."""
    import pandas as pd

    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    if pd.api.types.is_bool_dtype(series):
        return series.map({True: "True", False: "False"}).fillna(MISSING)
    if pd.api.types.is_numeric_dtype(series):
        values = series.dropna()
        if not values.empty and (values % 1 == 0).all():
            return series.astype("Int64").astype(str).replace("<NA>", MISSING)
    return series.where(series.notna(), MISSING).astype(str)


def _labels(frame: Any, columns: list[str]) -> Any:
    frame = frame.copy()
    for column in columns:
        frame[column] = _label(frame[column])
    return frame


def _sample(frame: Any, limit: int, ctx: dict[str, Any], what: str = "points") -> Any:
    if len(frame.index) <= limit:
        return frame
    ctx["notes"].append(
        f"Showing a random sample of {limit:,} of {len(frame.index):,} rows to keep the {what} readable."
    )
    ctx["sampled"] = True
    return frame.sample(limit, random_state=0).sort_index()


def _aggregate(frame: Any, by: list[str], value: str | None, how: str) -> tuple[Any, str]:
    """Group ``frame`` by ``by`` and combine ``value``; returns (data, value column)."""
    import pandas as pd

    data = _labels(
        frame, [column for column in by if not pd.api.types.is_datetime64_any_dtype(frame[column])]
    )
    if not value:
        return data.groupby(by, dropna=False, sort=False).size().reset_index(name="count"), "count"
    data[value] = pd.to_numeric(data[value], errors="coerce")
    how = how if how in {"sum", "mean", "median", "count", "min", "max"} else "sum"
    result = data.groupby(by, dropna=False, sort=False)[value].agg(how).reset_index()
    return result, value


def _order(data: Any, x: str, value: str, options: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    totals = data.groupby(x, sort=False)[value].sum()
    sort = options.get("sort", "none")
    if sort == "desc":
        totals = totals.sort_values(ascending=False, kind="mergesort")
    elif sort == "asc":
        totals = totals.sort_values(ascending=True, kind="mergesort")
    elif sort == "label":
        totals = totals.sort_index()
    top = int(options.get("top_n") or 0)
    if not top and len(totals) > MAX_CATEGORIES:
        top = MAX_CATEGORIES
        ctx["notes"].append(f"Showing the {top} largest of {len(totals)} categories.")
        totals = totals.sort_values(ascending=False, kind="mergesort")
    if top:
        if sort in {"none", "label"}:
            keep = set(totals.sort_values(ascending=False).head(top).index)
            totals = totals[totals.index.isin(keep)]
        else:
            totals = totals.head(top)
    return [str(item) for item in totals.index]


def _time_aggregate(
    frame: Any, x: str, values: list[str], by: list[str], how: str, grain: str
) -> tuple[Any, list[str], str]:
    import pandas as pd

    data = frame.copy()
    data[x] = _dates(data[x])
    data = data.dropna(subset=[x])
    if data.empty:
        raise AnalysisError(f"{x} has no readable dates.")
    if grain == "auto":
        # Aim for a readable number of periods: raw points only for short series.
        unique = data[x].nunique()
        span = (data[x].max() - data[x].min()).days
        if unique <= 60:
            grain = "none"
        else:
            grain = "D" if span <= 90 else "W" if span <= 400 else "M" if span <= 3650 else "Y"
    data = _labels(data, by)
    keys: list[Any] = [pd.Grouper(key=x, freq=GRAIN_FREQ[grain])] if grain in GRAIN_FREQ else [x]
    keys += by
    if values:
        for column in values:
            data[column] = pd.to_numeric(data[column], errors="coerce")
        how = how if how in {"sum", "mean", "median", "count", "min", "max"} else "sum"
        result = data.groupby(keys, dropna=False)[values].agg(how).reset_index()
    else:
        result = data.groupby(keys, dropna=False).size().reset_index(name="count")
        values = ["count"]
    return result.sort_values(x, kind="mergesort").reset_index(drop=True), values, grain


def _require_numeric(frame: Any, columns: list[str], minimum: int = 1) -> Any:
    import pandas as pd

    data = frame[columns].apply(pd.to_numeric, errors="coerce").dropna()
    if len(data.index) < minimum:
        raise AnalysisError(f"At least {minimum} complete rows are needed; found {len(data.index)}.")
    return data


def _feature_columns(
    frame: Any, fields: dict[str, Any], kinds: dict[str, str], exclude: tuple[str, ...] = ()
) -> list[str]:
    chosen = fields.get("features") or [
        c for c, kind in kinds.items() if kind == "numeric" and c not in exclude
    ]
    chosen = [column for column in chosen if column not in exclude]
    if len(chosen) < 2:
        raise AnalysisError("Choose at least two numeric columns.")
    if len(chosen) > 50:
        raise AnalysisError("Choose at most 50 columns.")
    return list(chosen)


def _matrix(frame, f, ctx, standardize: bool = True, limit: int | None = None) -> tuple[Any, Any, list[str]]:
    color = f.get("color")
    columns = _feature_columns(frame, f, ctx["kinds"], tuple(filter(None, [color])))
    data = frame[columns + ([color] if color else [])].copy()
    for column in columns:
        data[column] = _numeric(data, column)
    data = data.dropna(subset=columns)
    if limit:
        data = _sample(data, limit, ctx)
    if len(data.index) < 3:
        raise AnalysisError("At least three complete rows are needed.")
    values = data[columns].to_numpy(dtype=float)
    if standardize:
        from sklearn.preprocessing import StandardScaler

        values = StandardScaler().fit_transform(values)
    labels = _label(data[color]) if color else None
    return values, labels, columns


def _pca(frame, f, o, ctx, components: int):
    from sklearn.decomposition import PCA

    values, labels, columns = _matrix(frame, f, ctx, bool(o.get("standardize", True)), ctx["max_points"])
    components = min(components, values.shape[1], values.shape[0])
    model = PCA(n_components=components, random_state=0).fit(values)
    return model, model.transform(values), labels, columns


def _axis(model, index: int) -> str:
    return f"PC{index + 1} ({model.explained_variance_ratio_[index]:.1%})"


def _choose_k(values, max_k: int = 8) -> tuple[int, list[tuple[int, float]]]:
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score

    sample = (
        values
        if len(values) <= 3000
        else values[__import__("numpy").random.default_rng(0).choice(len(values), 3000, replace=False)]
    )
    scores = []
    for k in range(2, min(max_k, len(sample) - 1) + 1):
        labels = KMeans(n_clusters=k, n_init=4, random_state=0).fit_predict(sample)
        scores.append((k, float(silhouette_score(sample, labels))))
    if not scores:
        raise AnalysisError("Not enough rows to form clusters.")
    return max(scores, key=lambda item: item[1])[0], scores


def _supervised(frame, f, ctx, limit: int = 5000):
    """Numeric features plus target for the ML explainers (classification unless the target is numeric)."""
    import pandas as pd

    target = f["target"]
    columns = _feature_columns(frame, f, ctx["kinds"], (target,))
    data = frame[columns + [target]].copy()
    for column in columns:
        data[column] = _numeric(data, column)
    data = data.dropna(subset=[target])
    data = _sample(data, limit, ctx, "model")
    numeric_target = (
        ctx["kinds"].get(target) == "numeric" and pd.to_numeric(data[target], errors="coerce").nunique() > 10
    )
    y = pd.to_numeric(data[target], errors="coerce") if numeric_target else _label(data[target])
    if not numeric_target and y.nunique() < 2:
        raise AnalysisError("The target needs at least two classes.")
    x = data[columns].fillna(data[columns].median())
    return x, y, columns, numeric_target


def _estimator(name: str, regression: bool) -> Any:
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if regression:
        return {
            "linear": lambda: make_pipeline(StandardScaler(), LinearRegression()),
            "ridge": lambda: make_pipeline(StandardScaler(), Ridge(alpha=1.0)),
            "random_forest": lambda: RandomForestRegressor(
                n_estimators=150, max_depth=12, n_jobs=1, random_state=0
            ),
            "gradient_boosting": lambda: GradientBoostingRegressor(random_state=0),
        }[name if name != "logistic" else "linear"]()
    return {
        "logistic": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000)),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=150, max_depth=12, n_jobs=1, random_state=0, class_weight="balanced"
        ),
        "gradient_boosting": lambda: GradientBoostingClassifier(random_state=0),
    }[name if name not in {"linear", "ridge"} else "logistic"]()


def _quality(metric: str, value: float, baseline: float | None = None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "unknown"
    bands = {
        "r2": (0.8, 0.6, 0.3),
        "auc": (0.9, 0.8, 0.7),
        "silhouette": (0.5, 0.35, 0.2),
        "f1": (0.85, 0.7, 0.5),
    }
    if metric == "mape":
        return "excellent" if value <= 10 else "good" if value <= 20 else "fair" if value <= 50 else "weak"
    if metric == "accuracy" and baseline is not None:
        lift = value - baseline
        return "excellent" if lift >= 0.25 else "good" if lift >= 0.1 else "fair" if lift > 0.02 else "weak"
    excellent, good, fair = bands.get(metric, (0.9, 0.75, 0.5))
    return (
        "excellent"
        if value >= excellent
        else "good"
        if value >= good
        else "fair"
        if value >= fair
        else "weak"
    )


def _metric(
    key: str, label: str, value: Any, fmt: str = "number", quality: str | None = None, help_text: str = ""
) -> dict[str, Any]:
    if isinstance(value, float):
        value = None if math.isnan(value) or math.isinf(value) else round(value, 6)
    entry = {"key": key, "label": label, "value": value, "format": fmt, "help": help_text}
    if quality:
        entry["quality"] = quality
    return entry


def _design_matrix(frame: Any, features: list[str], kinds: dict[str, str], ctx: dict[str, Any]) -> Any:
    """Numeric model inputs: numbers as-is, dates as parts, categories one-hot (≤ 30 levels)."""
    import pandas as pd

    parts = []
    for column in features:
        kind = kinds.get(column, "categorical")
        series = frame[column]
        if kind == "numeric":
            parts.append(pd.to_numeric(series, errors="coerce").rename(column))
        elif kind == "boolean":
            parts.append(series.astype(float).rename(column))
        elif kind == "datetime":
            dates = _dates(series)
            parts.append(
                pd.DataFrame(
                    {
                        f"{column}_year": dates.dt.year,
                        f"{column}_month": dates.dt.month,
                        f"{column}_dayofweek": dates.dt.dayofweek,
                    },
                    index=frame.index,
                )
            )
        else:
            labels = _label(series)
            if labels.nunique() > 30:
                ctx["notes"].append(
                    f"{column} has too many distinct values to use as a model input and was skipped."
                )
                continue
            parts.append(pd.get_dummies(labels, prefix=column, dtype=float))
    if not parts:
        raise AnalysisError("No usable input columns; choose numeric, date or low-cardinality columns.")
    matrix = pd.concat(parts, axis=1)
    return matrix.fillna(matrix.median(numeric_only=True)).fillna(0)


def _inputs(frame, fields, kinds, target, ctx):
    features = [
        c for c in (fields.get("features") or [c for c in frame.columns if c != target]) if c != target
    ]
    identifier = [
        c for c in features if kinds.get(c) == "categorical" and frame[c].nunique() > 0.9 * len(frame.index)
    ]
    features = [c for c in features if c not in identifier]
    if identifier and not fields.get("features"):
        ctx["notes"].append(f"Skipped identifier-like columns: {', '.join(identifier[:5])}.")
    if not features:
        raise AnalysisError("Choose at least one input column.")
    return features[:60]


def _model_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for task in catalog.PREDICTION_TASKS:
        names.update(dict(task.models))
    names.update(
        {"linear_trend": "Linear trend + seasonality", "holt_winters": "Holt–Winters", "arima": "ARIMA"}
    )
    return names


def _color_groups(frame: Any, color: str | None, limit: int = 12) -> Any:
    if not color:
        return frame, None
    data = _labels(frame, [color])
    top = data[color].value_counts().head(limit).index
    if data[color].nunique() > limit:
        data = data[data[color].isin(top)]
    return data, color


def _with_other(data: Any, label: str, value: str, top: int) -> Any:
    import pandas as pd

    data = data.sort_values(value, ascending=False, kind="mergesort")
    if top and len(data.index) > top:
        rest = data.iloc[top:][value].sum()
        data = pd.concat([data.head(top), pd.DataFrame({label: ["Other"], value: [rest]})], ignore_index=True)
    return data


def _series_frame(frame, f, o, values: list[str] | None = None):
    x = f["x"]
    color = f.get("color")
    ys = (
        values
        if values is not None
        else ([f["y"]] if isinstance(f.get("y"), str) else list(f.get("y") or []))
    )
    return _time_aggregate(
        frame, x, ys, [color] if color else [], o.get("aggregation", "sum"), o.get("time_grain", "auto")
    )


def _distribution_values(frame, f, distribution: str) -> Any:
    values = _numeric(frame, f["x"]).dropna()
    if len(values) < 8:
        raise AnalysisError("At least 8 values are needed.")
    if distribution in {"lognorm", "gamma", "weibull_min"} and (values <= 0).any():
        raise AnalysisError("This distribution needs strictly positive values.")
    if distribution == "expon" and (values < 0).any():
        raise AnalysisError("The exponential distribution needs non-negative values.")
    return values.to_numpy()


def _fit(values, distribution: str):
    from scipy import stats

    dist = getattr(stats, distribution)
    kwargs = {"floc": 0} if distribution in {"lognorm", "gamma", "weibull_min", "expon"} else {}
    return dist, dist.fit(values, **kwargs)


def _xy(frame, f, ctx, extra: tuple[str, ...] = ()) -> Any:
    columns = [f["x"], f["y"]] + [f[key] for key in extra if f.get(key)]
    color = f.get("color")
    data = frame[list(dict.fromkeys(columns + ([color] if color else [])))].copy()
    for column in columns:
        data[column] = _numeric(data, column)
    data = data.dropna(subset=columns)
    if color and ctx["kinds"].get(color) != "numeric":
        data = _labels(data, [color])
    if len(data.index) < 2:
        raise AnalysisError("At least two complete rows are needed.")
    return _sample(data, ctx["max_points"], ctx)


def _grouped_axes(frame, f, ctx):
    y, x = f["y"], f.get("x")
    data, color = _color_groups(frame, f.get("color"))
    if x:
        data = _labels(data, [x])
        if data[x].nunique() > 40:
            data = data[data[x].isin(data[x].value_counts().head(40).index)]
            ctx["notes"].append("Showing the 40 most frequent groups.")
    data = data.assign(**{y: _numeric(data, y)}).dropna(subset=[y])
    return data, x, y, color


def _truth_and_score(frame, f, o, ctx):
    data = frame[[f["actual"], f["score"]]].copy()
    data[f["score"]] = _numeric(data, f["score"])
    data = data.dropna()
    if len(data.index) < 5:
        raise AnalysisError("At least five complete rows are needed.")
    actual = _label(data[f["actual"]])
    classes = list(actual.value_counts().index)
    if len(classes) < 2:
        raise AnalysisError("The actual column needs at least two classes.")
    positive = str(o.get("positive_class") or "")
    if positive not in classes:
        preferred = [
            c for c in classes if c.lower() in {"1", "true", "yes", "y", "positive", "churn", "fraud", "spam"}
        ]
        positive = preferred[0] if preferred else classes[-1]
    truth = (actual == positive).astype(int).to_numpy()
    score = data[f["score"]].to_numpy(dtype=float)
    if len(classes) > 2:
        ctx["notes"].append(f"Treating “{positive}” as positive and every other class as negative.")
    ctx["notes"].append(f"Positive class: {positive}.")
    if score.min() < 0 or score.max() > 1:
        ctx["scores_scaled"] = True
    return truth, score, positive


def _regression_pairs(frame, f, ctx):
    data = _require_numeric(frame, [f["actual"], f["predicted"]], 3)
    data = _sample(data, ctx["max_points"], ctx)
    actual, predicted = data[f["actual"]].to_numpy(), data[f["predicted"]].to_numpy()
    _regression_stats(ctx, actual, predicted)
    return actual, predicted


def _stat(ctx: dict[str, Any], label: str, value: Any, fmt: str = "number") -> None:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return
    ctx["stats"].append({"label": label, "value": value, "format": fmt})


def _regression_stats(ctx, actual, predicted) -> None:
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    _stat(ctx, "R²", round(float(r2_score(actual, predicted)), 4))
    _stat(ctx, "MAE", round(float(mean_absolute_error(actual, predicted)), 4))
    _stat(ctx, "RMSE", round(float(math.sqrt(mean_squared_error(actual, predicted))), 4))


def _value_label(column: str | None, options: dict[str, Any]) -> str:
    if not column:
        return "Count"
    how = options.get("aggregation", "sum")
    return {
        "sum": "Total",
        "mean": "Average",
        "median": "Median",
        "count": "Count of",
        "min": "Minimum",
        "max": "Maximum",
    }.get(how, "") + f" {column}"


def _correlation_stats(ctx, x, y) -> None:
    from scipy import stats

    if len(x) >= 3 and x.std() and y.std():
        pearson = stats.pearsonr(x, y)
        _stat(ctx, "Pearson r", round(float(pearson.statistic), 4))
        _stat(ctx, "p-value", float(f"{pearson.pvalue:.4g}"))
        _stat(ctx, "Spearman ρ", round(float(stats.spearmanr(x, y).statistic), 4))
