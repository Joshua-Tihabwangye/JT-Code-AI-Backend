"""The visualization catalog: categories, chart types, palettes and prediction tasks.

This module is the single source of truth shared by the API (served to the UI as
``/visualize/catalog/``), request validation, the deterministic recommender, the
AI assistant's allow-list and the isolated rendering engine. It deliberately
imports nothing from Django or the application so the sandboxed engine can load
it by file path.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Column kinds come from the engine's profile: numeric, categorical, datetime, boolean.
# "any" accepts every kind; "dimension" accepts categorical, boolean and datetime.
SLOT_KINDS = {"any", "numeric", "categorical", "datetime", "dimension"}


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    kind: str = "any"
    required: bool = True
    multiple: bool = False
    help: str = ""


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    type: str  # select | boolean | number
    default: Any
    choices: tuple[tuple[str, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class ChartType:
    id: str
    name: str
    category: str
    description: str
    slots: tuple[Slot, ...]
    options: tuple[Option, ...] = ()
    tags: tuple[str, ...] = ()
    level: str = "essential"  # essential (everyone) | advanced (analysts, enterprises)


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    description: str
    color: str
    icon: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        "comparison", "Comparison", "Compare values across categories and rank them.", "#6366F1", "BarChart3"
    ),
    Category("trend", "Trends & time", "Show how values change over time.", "#0EA5E9", "TrendingUp"),
    Category("composition", "Composition", "Break a total into its parts.", "#F59E0B", "PieChart"),
    Category(
        "distribution", "Distribution", "Understand spread, shape and outliers.", "#10B981", "BarChartBig"
    ),
    Category(
        "statistical",
        "Statistical",
        "Confidence intervals, control limits and diagnostics.",
        "#14B8A6",
        "Sigma",
    ),
    Category(
        "relationship",
        "Relationship & correlation",
        "Find how variables move together.",
        "#EC4899",
        "ScatterChart",
    ),
    Category(
        "flow", "Flow & hierarchy", "Follow quantities between stages and levels.", "#8B5CF6", "Workflow"
    ),
    Category("geo", "Maps", "Plot values by country or coordinates.", "#22C55E", "Globe2"),
    Category("dimensional", "Dimensional", "Reduce and explore many variables at once.", "#F97316", "Box"),
    Category(
        "ml", "Model evaluation", "Judge classifiers, regressors and clusters.", "#EF4444", "BrainCircuit"
    ),
)

# --- Reusable options ----------------------------------------------------------------------

AGGREGATION = Option(
    "aggregation",
    "Combine values with",
    "select",
    "sum",
    (
        ("sum", "Sum"),
        ("mean", "Average"),
        ("median", "Median"),
        ("count", "Count"),
        ("min", "Minimum"),
        ("max", "Maximum"),
    ),
)
SORT = Option(
    "sort",
    "Order",
    "select",
    "none",
    (
        ("none", "As in the data"),
        ("desc", "Largest first"),
        ("asc", "Smallest first"),
        ("label", "Alphabetical"),
    ),
)
TOP_N = Option("top_n", "Show only the top", "number", 0, minimum=0, maximum=100)
ORIENTATION = Option("orientation", "Direction", "select", "v", (("v", "Vertical"), ("h", "Horizontal")))
LABELS = Option("show_values", "Show values on the chart", "boolean", False)
BINS = Option("bins", "Number of bins", "number", 0, minimum=0, maximum=200)
LOG_Y = Option("log_y", "Logarithmic value axis", "boolean", False)
TRENDLINE = Option(
    "trendline",
    "Trend line",
    "select",
    "none",
    (("none", "None"), ("ols", "Linear (OLS)"), ("lowess", "Smoothed (LOWESS)")),
)
OPACITY = Option("opacity", "Point opacity", "number", 0.8, minimum=0.1, maximum=1)
CORR_METHOD = Option(
    "method",
    "Correlation",
    "select",
    "pearson",
    (("pearson", "Pearson"), ("spearman", "Spearman"), ("kendall", "Kendall")),
)
TIME_GRAIN = Option(
    "time_grain",
    "Group dates by",
    "select",
    "auto",
    (
        ("auto", "Automatic"),
        ("none", "No grouping"),
        ("D", "Day"),
        ("W", "Week"),
        ("M", "Month"),
        ("Q", "Quarter"),
        ("Y", "Year"),
    ),
)
WINDOW = Option("window", "Rolling window (points)", "number", 7, minimum=2, maximum=365)
HOLE = Option("hole", "Centre hole", "number", 0.55, minimum=0, maximum=0.9)
POINTS = Option(
    "points",
    "Show points",
    "select",
    "outliers",
    (("outliers", "Outliers"), ("all", "All"), ("none", "None")),
)
DIST_FIT = Option(
    "distribution",
    "Distribution",
    "select",
    "norm",
    (
        ("norm", "Normal"),
        ("lognorm", "Log-normal"),
        ("expon", "Exponential"),
        ("gamma", "Gamma"),
        ("weibull_min", "Weibull"),
    ),
)
POSITIVE_CLASS = Option("positive_class", "Positive class (blank = automatic)", "select", "", ())
SCOPE = Option(
    "scope",
    "Map area",
    "select",
    "world",
    (
        ("world", "World"),
        ("africa", "Africa"),
        ("europe", "Europe"),
        ("asia", "Asia"),
        ("north america", "North America"),
        ("south america", "South America"),
    ),
)
LOCATION_MODE = Option(
    "location_mode",
    "Locations are",
    "select",
    "country names",
    (("country names", "Country names"), ("ISO-3", "ISO-3 codes")),
)
CLUSTERS = Option("clusters", "Clusters (0 = choose automatically)", "number", 0, minimum=0, maximum=12)
MAX_K = Option("max_k", "Try up to k", "number", 10, minimum=3, maximum=15)
SCALE = Option("standardize", "Standardize features", "boolean", True)
MODEL = Option(
    "model",
    "Model",
    "select",
    "random_forest",
    (
        ("random_forest", "Random forest"),
        ("gradient_boosting", "Gradient boosting"),
        ("linear", "Linear / logistic"),
    ),
)

# --- Reusable slots -------------------------------------------------------------------------

CAT_X = Slot("x", "Category", "dimension")
VALUE = Slot("y", "Value", "numeric", help="Leave a count by choosing Count as the aggregation.")
VALUE_OPT = Slot("y", "Value", "numeric", required=False, help="Optional: rows are counted when empty.")
COLOR = Slot("color", "Colour by", "dimension", required=False)
FACET = Slot("facet", "Split into panels by", "dimension", required=False)
DATE = Slot("x", "Date / time", "datetime")
NUM_X = Slot("x", "X axis", "numeric")
NUM_Y = Slot("y", "Y axis", "numeric")
SIZE = Slot("size", "Bubble size", "numeric", required=False)
FEATURES = Slot("features", "Numeric columns", "numeric", multiple=True, help="Pick two or more.")
FEATURES_OPT = Slot(
    "features",
    "Numeric columns",
    "numeric",
    required=False,
    multiple=True,
    help="Empty uses all numeric columns.",
)
LABEL_OPT = Slot("color", "Colour by (label)", "dimension", required=False)
ACTUAL = Slot("actual", "Actual value / true label", "any")
PREDICTED = Slot("predicted", "Predicted value / label", "any")
SCORE = Slot("score", "Predicted probability / score", "numeric")


def _c(*args: Any, **kwargs: Any) -> ChartType:
    return ChartType(*args, **kwargs)


CHARTS: tuple[ChartType, ...] = (
    # Comparison ---------------------------------------------------------------------------
    _c(
        "bar",
        "Bar chart",
        "comparison",
        "Compare a value across categories.",
        (CAT_X, VALUE_OPT, COLOR, FACET),
        (AGGREGATION, SORT, TOP_N, ORIENTATION, LABELS, LOG_Y),
        ("compare", "rank", "category", "column"),
    ),
    _c(
        "grouped_bar",
        "Grouped bar",
        "comparison",
        "Side-by-side bars for each sub-group.",
        (CAT_X, VALUE_OPT, Slot("color", "Group by", "dimension")),
        (AGGREGATION, SORT, ORIENTATION, LABELS),
        ("clustered", "compare"),
    ),
    _c(
        "stacked_bar",
        "Stacked bar",
        "comparison",
        "Totals with the contribution of each sub-group.",
        (CAT_X, VALUE_OPT, Slot("color", "Stack by", "dimension")),
        (AGGREGATION, SORT, ORIENTATION, LABELS),
        ("stack", "total"),
    ),
    _c(
        "percent_stacked_bar",
        "100% stacked bar",
        "comparison",
        "Share of each sub-group within every category.",
        (CAT_X, VALUE_OPT, Slot("color", "Stack by", "dimension")),
        (AGGREGATION, ORIENTATION),
        ("percentage", "share", "mix"),
    ),
    _c(
        "lollipop",
        "Lollipop chart",
        "comparison",
        "A lighter bar chart for ranked values.",
        (CAT_X, VALUE_OPT),
        (AGGREGATION, Option("sort", "Order", "select", "desc", SORT.choices), TOP_N),
        ("rank", "minimal"),
    ),
    _c(
        "dot_plot",
        "Dot plot",
        "comparison",
        "Compare several measures per category as dots.",
        (CAT_X, Slot("y", "Values", "numeric", multiple=True)),
        (AGGREGATION,),
        ("cleveland", "compare"),
    ),
    _c(
        "dumbbell",
        "Dumbbell chart",
        "comparison",
        "Show the gap between two measures per category.",
        (CAT_X, Slot("start", "Start value", "numeric"), Slot("end", "End value", "numeric")),
        (AGGREGATION, SORT),
        ("gap", "before after"),
    ),
    _c(
        "radar",
        "Radar chart",
        "comparison",
        "Profile several measures for a few items.",
        (Slot("x", "Item", "dimension"), Slot("features", "Measures", "numeric", multiple=True)),
        (AGGREGATION,),
        ("spider", "profile"),
        "advanced",
    ),
    _c(
        "bullet",
        "Bullet / target chart",
        "comparison",
        "Actual against target for each category.",
        (CAT_X, Slot("y", "Actual", "numeric"), Slot("target", "Target", "numeric")),
        (AGGREGATION,),
        ("kpi", "goal", "target"),
    ),
    _c(
        "pareto",
        "Pareto chart",
        "comparison",
        "Sorted bars with the cumulative share line (80/20).",
        (CAT_X, VALUE_OPT),
        (AGGREGATION, TOP_N),
        ("80/20", "pareto", "cumulative"),
    ),
    _c(
        "category_heatmap",
        "Heatmap table",
        "comparison",
        "Colour a value across two categories.",
        (
            Slot("x", "Columns", "dimension"),
            Slot("y", "Rows", "dimension"),
            Slot("z", "Value", "numeric", required=False),
        ),
        (AGGREGATION, Option("show_values", "Show values", "boolean", True)),
        ("matrix", "pivot", "heat"),
    ),
    # Trend ----------------------------------------------------------------------------------
    _c(
        "line",
        "Line chart",
        "trend",
        "The classic view of change over time.",
        (DATE, Slot("y", "Values", "numeric", multiple=True), COLOR),
        (AGGREGATION, TIME_GRAIN, Option("markers", "Show markers", "boolean", False), LOG_Y),
        ("time series", "trend", "over time"),
    ),
    _c(
        "area",
        "Area chart",
        "trend",
        "Emphasise the volume of a trend.",
        (DATE, VALUE_OPT, COLOR),
        (AGGREGATION, TIME_GRAIN),
        ("volume",),
    ),
    _c(
        "stacked_area",
        "Stacked area",
        "trend",
        "How parts of a total change over time.",
        (DATE, VALUE_OPT, Slot("color", "Stack by", "dimension")),
        (AGGREGATION, TIME_GRAIN, Option("normalize", "Show as 100%", "boolean", False)),
        ("composition over time",),
    ),
    _c(
        "step_line",
        "Step line",
        "trend",
        "Values that change at discrete moments (prices, stock levels).",
        (DATE, VALUE_OPT, COLOR),
        (AGGREGATION, TIME_GRAIN),
        ("step",),
    ),
    _c(
        "rolling_average",
        "Moving average",
        "trend",
        "Smooth noisy series with a rolling mean.",
        (DATE, VALUE_OPT),
        (AGGREGATION, TIME_GRAIN, WINDOW),
        ("smooth", "rolling", "moving average"),
    ),
    _c(
        "cumulative",
        "Cumulative total",
        "trend",
        "Running total over time.",
        (DATE, VALUE_OPT, COLOR),
        (AGGREGATION, TIME_GRAIN),
        ("running total", "cumsum"),
    ),
    _c(
        "period_change",
        "Period-over-period change",
        "trend",
        "Percentage change from the previous period.",
        (DATE, VALUE_OPT),
        (AGGREGATION, Option("time_grain", TIME_GRAIN.label, "select", "M", TIME_GRAIN.choices[1:])),
        ("growth", "mom", "yoy"),
    ),
    _c(
        "candlestick",
        "Candlestick",
        "trend",
        "Open/high/low/close movement (finance).",
        (
            DATE,
            Slot("open", "Open", "numeric"),
            Slot("high", "High", "numeric"),
            Slot("low", "Low", "numeric"),
            Slot("close", "Close", "numeric"),
        ),
        (),
        ("ohlc", "stock", "finance"),
        "advanced",
    ),
    _c(
        "range_band",
        "Range band",
        "trend",
        "A line with its min–max or uncertainty band.",
        (
            DATE,
            Slot("y", "Value", "numeric"),
            Slot("lower", "Lower bound", "numeric"),
            Slot("upper", "Upper bound", "numeric"),
        ),
        (),
        ("confidence band", "interval"),
        "advanced",
    ),
    _c(
        "calendar_heatmap",
        "Calendar heatmap",
        "trend",
        "Weekday × week pattern of a daily measure.",
        (DATE, VALUE_OPT),
        (AGGREGATION,),
        ("seasonality", "weekday", "calendar"),
    ),
    _c(
        "seasonal_decompose",
        "Seasonal decomposition",
        "trend",
        "Split a series into trend, seasonality and residual.",
        (DATE, Slot("y", "Value", "numeric")),
        (
            Option("time_grain", TIME_GRAIN.label, "select", "M", TIME_GRAIN.choices[2:]),
            AGGREGATION,
            Option(
                "model",
                "Seasonality",
                "select",
                "additive",
                (("additive", "Additive"), ("multiplicative", "Multiplicative")),
            ),
        ),
        ("decomposition", "seasonality", "statsmodels"),
        "advanced",
    ),
    _c(
        "autocorrelation",
        "Autocorrelation (ACF / PACF)",
        "trend",
        "How a series relates to its own past values.",
        (DATE, Slot("y", "Value", "numeric")),
        (Option("lags", "Lags", "number", 24, minimum=4, maximum=120), TIME_GRAIN, AGGREGATION),
        ("acf", "pacf", "lag"),
        "advanced",
    ),
    # Composition ---------------------------------------------------------------------------
    _c(
        "pie",
        "Pie chart",
        "composition",
        "Parts of a whole (best with few slices).",
        (Slot("x", "Slices", "dimension"), VALUE_OPT),
        (AGGREGATION, TOP_N),
        ("share", "proportion"),
    ),
    _c(
        "donut",
        "Donut chart",
        "composition",
        "A pie with room for a total in the centre.",
        (Slot("x", "Slices", "dimension"), VALUE_OPT),
        (AGGREGATION, TOP_N, HOLE),
        ("share", "ring"),
    ),
    _c(
        "treemap",
        "Treemap",
        "composition",
        "Nested rectangles sized by value.",
        (Slot("path", "Levels (outer to inner)", "dimension", multiple=True), VALUE_OPT),
        (AGGREGATION,),
        ("hierarchy", "nested"),
    ),
    _c(
        "sunburst",
        "Sunburst",
        "composition",
        "Hierarchy as concentric rings.",
        (Slot("path", "Levels (inner to outer)", "dimension", multiple=True), VALUE_OPT),
        (AGGREGATION,),
        ("hierarchy", "radial"),
    ),
    _c(
        "icicle",
        "Icicle chart",
        "composition",
        "Hierarchy as stacked rectangles.",
        (Slot("path", "Levels (top to bottom)", "dimension", multiple=True), VALUE_OPT),
        (AGGREGATION,),
        ("hierarchy",),
        "advanced",
    ),
    _c(
        "waterfall",
        "Waterfall",
        "composition",
        "How positive and negative steps build a total.",
        (Slot("x", "Step", "dimension"), VALUE),
        (Option("total", "Show the total bar", "boolean", True),),
        ("bridge", "profit and loss", "variance"),
    ),
    _c(
        "funnel",
        "Funnel",
        "composition",
        "Conversion through ordered stages.",
        (Slot("x", "Stage", "dimension"), VALUE_OPT),
        (AGGREGATION, Option("sort", "Order", "select", "desc", SORT.choices)),
        ("conversion", "pipeline", "sales"),
    ),
    _c(
        "marimekko",
        "Marimekko (mosaic)",
        "composition",
        "Two-way composition with variable widths.",
        (Slot("x", "Columns", "dimension"), Slot("color", "Segments", "dimension"), VALUE_OPT),
        (AGGREGATION,),
        ("mosaic", "mekko"),
        "advanced",
    ),
    # Distribution ---------------------------------------------------------------------------
    _c(
        "histogram",
        "Histogram",
        "distribution",
        "How often values fall in each range.",
        (Slot("x", "Value", "numeric"), COLOR),
        (
            BINS,
            Option(
                "histnorm",
                "Show as",
                "select",
                "",
                (("", "Counts"), ("percent", "Percent"), ("probability density", "Density")),
            ),
            Option(
                "marginal",
                "Add a margin plot",
                "select",
                "box",
                (("", "None"), ("box", "Box")),
            ),
        ),
        ("frequency", "spread", "bins"),
    ),
    _c(
        "density_kde",
        "Density curve (KDE)",
        "distribution",
        "A smooth estimate of the distribution.",
        (Slot("x", "Value", "numeric"), COLOR),
        (
            Option(
                "bandwidth", "Smoothing", "select", "scott", (("scott", "Scott"), ("silverman", "Silverman"))
            ),
        ),
        ("kde", "smooth"),
    ),
    _c(
        "box",
        "Box plot",
        "distribution",
        "Median, quartiles and outliers per group.",
        (Slot("y", "Value", "numeric"), Slot("x", "Group by", "dimension", required=False), COLOR),
        (POINTS,),
        ("quartiles", "outliers", "whisker"),
    ),
    _c(
        "violin",
        "Violin plot",
        "distribution",
        "Box plot plus the full distribution shape.",
        (Slot("y", "Value", "numeric"), Slot("x", "Group by", "dimension", required=False), COLOR),
        (Option("box", "Show box inside", "boolean", True), POINTS),
        ("shape",),
    ),
    _c(
        "strip",
        "Strip / jitter plot",
        "distribution",
        "Every observation as a dot per group.",
        (Slot("y", "Value", "numeric"), Slot("x", "Group by", "dimension", required=False), COLOR),
        (),
        ("beeswarm", "jitter"),
    ),
    _c(
        "ecdf",
        "Cumulative distribution (ECDF)",
        "distribution",
        "Share of values below each level.",
        (Slot("x", "Value", "numeric"), COLOR),
        (),
        ("percentile", "cumulative"),
    ),
    _c(
        "ridgeline",
        "Ridgeline",
        "distribution",
        "Stacked distributions for many groups.",
        (Slot("y", "Value", "numeric"), Slot("x", "Group by", "dimension")),
        (),
        ("joyplot",),
        "advanced",
    ),
    _c(
        "raincloud",
        "Raincloud plot",
        "distribution",
        "Half violin, box and raw points together.",
        (Slot("y", "Value", "numeric"), Slot("x", "Group by", "dimension", required=False)),
        (),
        ("violin", "raw data"),
        "advanced",
    ),
    # Statistical ----------------------------------------------------------------------------
    _c(
        "mean_ci",
        "Mean with confidence interval",
        "statistical",
        "Group means with 95% confidence intervals.",
        (Slot("x", "Group", "dimension"), Slot("y", "Value", "numeric")),
        (
            Option(
                "confidence",
                "Confidence",
                "select",
                "0.95",
                (("0.9", "90%"), ("0.95", "95%"), ("0.99", "99%")),
            ),
        ),
        ("error bars", "ci", "t-test"),
    ),
    _c(
        "qq_plot",
        "Q–Q plot",
        "statistical",
        "Check whether values follow a theoretical distribution.",
        (Slot("x", "Value", "numeric"),),
        (DIST_FIT,),
        ("normality", "quantile"),
    ),
    _c(
        "distribution_fit",
        "Distribution fit",
        "statistical",
        "Histogram with a fitted distribution and goodness of fit.",
        (Slot("x", "Value", "numeric"),),
        (DIST_FIT, BINS),
        ("fit", "pdf", "ks test"),
    ),
    _c(
        "control_chart",
        "Control chart (SPC)",
        "statistical",
        "Process values with mean and ±3σ control limits.",
        (Slot("x", "Order / time", "any"), Slot("y", "Measurement", "numeric")),
        (Option("sigma", "Limits (σ)", "number", 3, minimum=1, maximum=6),),
        ("spc", "shewhart", "quality"),
    ),
    _c(
        "bland_altman",
        "Bland–Altman",
        "statistical",
        "Agreement between two measurement methods.",
        (Slot("x", "Method A", "numeric"), Slot("y", "Method B", "numeric")),
        (),
        ("agreement", "difference"),
        "advanced",
    ),
    _c(
        "missing_values",
        "Missing values map",
        "statistical",
        "Where data is missing across columns.",
        (FEATURES_OPT,),
        (),
        ("data quality", "nulls", "completeness"),
    ),
    # Relationship ---------------------------------------------------------------------------
    _c(
        "scatter",
        "Scatter plot",
        "relationship",
        "The relationship between two measures.",
        (NUM_X, NUM_Y, COLOR, SIZE),
        (
            TRENDLINE,
            OPACITY,
            Option(
                "marginal",
                "Margin plots",
                "select",
                "",
                (("", "None"), ("histogram", "Histogram"), ("box", "Box")),
            ),
        ),
        ("correlation", "xy"),
    ),
    _c(
        "bubble",
        "Bubble chart",
        "relationship",
        "A scatter plot with a third measure as size.",
        (NUM_X, NUM_Y, Slot("size", "Bubble size", "numeric"), COLOR),
        (OPACITY,),
        ("three variables",),
    ),
    _c(
        "regression_fit",
        "Regression fit",
        "relationship",
        "Scatter with a fitted line, R² and equation.",
        (NUM_X, NUM_Y),
        (
            Option("degree", "Polynomial degree", "number", 1, minimum=1, maximum=5),
            Option("band", "Show 95% band", "boolean", True),
        ),
        ("ols", "trend", "fit", "r squared"),
    ),
    _c(
        "correlation_heatmap",
        "Correlation matrix",
        "relationship",
        "Correlation between every pair of measures.",
        (FEATURES_OPT,),
        (
            CORR_METHOD,
            Option("show_values", "Show coefficients", "boolean", True),
            Option("triangle", "Lower triangle only", "boolean", True),
        ),
        ("correlation", "pearson", "heatmap"),
    ),
    _c(
        "scatter_matrix",
        "Scatter matrix (pair plot)",
        "relationship",
        "Every pair of measures at a glance.",
        (FEATURES_OPT, LABEL_OPT),
        (),
        ("pairplot", "splom"),
    ),
    _c(
        "density_heatmap",
        "2D density heatmap",
        "relationship",
        "Where points concentrate (for many rows).",
        (NUM_X, NUM_Y),
        (Option("nbins", "Bins", "number", 40, minimum=5, maximum=200),),
        ("hexbin", "2d histogram"),
    ),
    _c(
        "density_contour",
        "Density contour",
        "relationship",
        "Contours of point density.",
        (NUM_X, NUM_Y, COLOR),
        (),
        ("contour", "kde"),
    ),
    _c(
        "joint_plot",
        "Joint plot",
        "relationship",
        "Scatter with the distribution of each axis.",
        (NUM_X, NUM_Y, COLOR),
        (TRENDLINE,),
        ("marginal", "scatter histogram"),
    ),
    _c(
        "parallel_coordinates",
        "Parallel coordinates",
        "relationship",
        "Compare many measures across rows.",
        (FEATURES_OPT, Slot("color", "Colour by (numeric)", "numeric", required=False)),
        (),
        ("multivariate",),
        "advanced",
    ),
    _c(
        "parallel_categories",
        "Parallel categories",
        "relationship",
        "Flows between categorical columns.",
        (Slot("path", "Categorical columns", "dimension", multiple=True),),
        (),
        ("parallel sets", "alluvial"),
        "advanced",
    ),
    _c(
        "crosstab_heatmap",
        "Contingency table",
        "relationship",
        "Counts across two categories with a chi-square test.",
        (Slot("x", "Columns", "dimension"), Slot("y", "Rows", "dimension")),
        (
            Option(
                "normalize",
                "Show as",
                "select",
                "none",
                (
                    ("none", "Counts"),
                    ("index", "% of row"),
                    ("columns", "% of column"),
                    ("all", "% of total"),
                ),
            ),
        ),
        ("chi square", "crosstab", "association"),
    ),
    # Flow & hierarchy -----------------------------------------------------------------------
    _c(
        "sankey",
        "Sankey diagram",
        "flow",
        "Quantities flowing between stages.",
        (Slot("path", "Stages (in order)", "dimension", multiple=True), VALUE_OPT),
        (AGGREGATION,),
        ("flow", "journey", "alluvial"),
    ),
    _c(
        "network",
        "Network graph",
        "flow",
        "Connections between entities.",
        (
            Slot("source", "From", "dimension"),
            Slot("target", "To", "dimension"),
            Slot("y", "Weight", "numeric", required=False),
        ),
        (),
        ("graph", "nodes", "links"),
        "advanced",
    ),
    # Geo ------------------------------------------------------------------------------------
    _c(
        "choropleth",
        "Choropleth map",
        "geo",
        "Colour countries by a value.",
        (Slot("location", "Country", "categorical"), VALUE_OPT),
        (AGGREGATION, LOCATION_MODE, SCOPE),
        ("world map", "countries"),
    ),
    _c(
        "scatter_geo",
        "Points on a map",
        "geo",
        "Plot latitude/longitude points.",
        (Slot("lat", "Latitude", "numeric"), Slot("lon", "Longitude", "numeric"), COLOR, SIZE),
        (SCOPE,),
        ("coordinates", "locations"),
    ),
    # Dimensional ----------------------------------------------------------------------------
    _c(
        "scatter_3d",
        "3D scatter",
        "dimensional",
        "Explore three measures in rotating 3D.",
        (NUM_X, NUM_Y, Slot("z", "Z axis", "numeric"), COLOR, SIZE),
        (OPACITY,),
        ("three dimensional",),
    ),
    _c(
        "surface_3d",
        "3D surface",
        "dimensional",
        "A surface over two measures.",
        (NUM_X, NUM_Y, Slot("z", "Height", "numeric")),
        (Option("contours", "Project contours", "boolean", True),),
        ("mesh", "terrain"),
        "advanced",
    ),
    _c(
        "pca_2d",
        "PCA map (2D)",
        "dimensional",
        "Project many measures onto two principal components.",
        (FEATURES_OPT, LABEL_OPT),
        (SCALE,),
        ("principal components", "dimensionality reduction"),
    ),
    _c(
        "pca_3d",
        "PCA map (3D)",
        "dimensional",
        "Three principal components in 3D.",
        (FEATURES_OPT, LABEL_OPT),
        (SCALE,),
        ("principal components",),
    ),
    _c(
        "pca_biplot",
        "PCA biplot",
        "dimensional",
        "Rows and the measures that drive each component.",
        (FEATURES_OPT, LABEL_OPT),
        (SCALE,),
        ("loadings",),
        "advanced",
    ),
    _c(
        "scree_plot",
        "Scree plot",
        "dimensional",
        "Variance explained by each component.",
        (FEATURES_OPT,),
        (SCALE,),
        ("explained variance",),
    ),
    _c(
        "tsne_2d",
        "t-SNE map",
        "dimensional",
        "Non-linear 2D map that keeps neighbours close.",
        (FEATURES_OPT, LABEL_OPT),
        (Option("perplexity", "Perplexity", "number", 30, minimum=5, maximum=50),),
        ("embedding", "manifold"),
        "advanced",
    ),
    _c(
        "lda_2d",
        "LDA projection",
        "dimensional",
        "Projection that best separates labelled groups.",
        (FEATURES_OPT, Slot("color", "Label", "dimension")),
        (),
        ("linear discriminant", "supervised"),
        "advanced",
    ),
    _c(
        "kmeans_clusters",
        "Cluster map (k-means)",
        "dimensional",
        "Group similar rows and show them on a PCA map.",
        (FEATURES_OPT,),
        (CLUSTERS, SCALE),
        ("segmentation", "clustering", "customers"),
    ),
    # Model evaluation -----------------------------------------------------------------------
    _c(
        "confusion_matrix",
        "Confusion matrix",
        "ml",
        "Where a classifier is right and wrong.",
        (ACTUAL, PREDICTED),
        (
            Option(
                "normalize",
                "Show as",
                "select",
                "none",
                (("none", "Counts"), ("true", "% of actual"), ("pred", "% of predicted")),
            ),
        ),
        ("classification", "accuracy"),
    ),
    _c(
        "roc_curve",
        "ROC curve",
        "ml",
        "True vs false positive rate at every threshold, with AUC.",
        (ACTUAL, SCORE),
        (POSITIVE_CLASS,),
        ("auc", "classification", "threshold"),
    ),
    _c(
        "pr_curve",
        "Precision–recall curve",
        "ml",
        "Precision and recall trade-off, with average precision.",
        (ACTUAL, SCORE),
        (POSITIVE_CLASS,),
        ("precision", "recall", "imbalanced"),
    ),
    _c(
        "calibration_curve",
        "Calibration curve",
        "ml",
        "Do predicted probabilities match observed rates?",
        (ACTUAL, SCORE),
        (POSITIVE_CLASS, Option("bins", "Bins", "number", 10, minimum=3, maximum=30)),
        ("reliability", "probability"),
        "advanced",
    ),
    _c(
        "cumulative_gain",
        "Cumulative gain & lift",
        "ml",
        "How much better than random targeting the model is.",
        (ACTUAL, SCORE),
        (POSITIVE_CLASS,),
        ("lift", "marketing", "targeting"),
        "advanced",
    ),
    _c(
        "threshold_metrics",
        "Threshold explorer",
        "ml",
        "Precision, recall and F1 at every threshold.",
        (ACTUAL, SCORE),
        (POSITIVE_CLASS,),
        ("f1", "cutoff"),
        "advanced",
    ),
    _c(
        "predicted_vs_actual",
        "Predicted vs actual",
        "ml",
        "Regression accuracy against the perfect line.",
        (ACTUAL, PREDICTED),
        (),
        ("regression", "r squared"),
    ),
    _c(
        "residual_plot",
        "Residual plot",
        "ml",
        "Errors against predictions to spot bias.",
        (ACTUAL, PREDICTED),
        (),
        ("regression", "errors"),
    ),
    _c(
        "error_distribution",
        "Error distribution",
        "ml",
        "Histogram of prediction errors.",
        (ACTUAL, PREDICTED),
        (BINS,),
        ("residuals", "mae"),
    ),
    _c(
        "feature_importance",
        "Feature importance",
        "ml",
        "Which inputs drive a target (random forest + permutation).",
        (Slot("target", "Target to explain", "any"), FEATURES_OPT),
        (Option("top_n", "Show top", "number", 15, minimum=3, maximum=50),),
        ("drivers", "explainability", "key drivers"),
    ),
    _c(
        "learning_curve",
        "Learning curve",
        "ml",
        "Would more data help the model?",
        (Slot("target", "Target", "any"), FEATURES_OPT),
        (MODEL,),
        ("overfitting", "bias variance"),
        "advanced",
    ),
    _c(
        "elbow",
        "Elbow chart",
        "ml",
        "Choose the number of clusters by inertia.",
        (FEATURES_OPT,),
        (MAX_K, SCALE),
        ("k-means", "clusters"),
    ),
    _c(
        "silhouette",
        "Silhouette analysis",
        "ml",
        "Cluster quality for every k.",
        (FEATURES_OPT,),
        (MAX_K, SCALE),
        ("clusters", "quality"),
        "advanced",
    ),
    _c(
        "kpi_card",
        "KPI card",
        "trend",
        "One headline number with its change and a sparkline.",
        (DATE, Slot("y", "Measure", "numeric")),
        (AGGREGATION, Option("time_grain", TIME_GRAIN.label, "select", "M", TIME_GRAIN.choices[2:])),
        ("kpi", "headline", "scorecard", "dashboard"),
    ),
    _c(
        "gauge",
        "Gauge (progress to target)",
        "comparison",
        "How far a measure is towards a target.",
        (Slot("y", "Measure", "numeric"), Slot("target", "Target (optional)", "numeric", required=False)),
        (
            Option("aggregation", AGGREGATION.label, "select", "sum", AGGREGATION.choices),
            Option("goal", "Target value (0 = use the target column)", "number", 0, minimum=0),
        ),
        ("kpi", "goal", "progress", "meter"),
    ),
    _c(
        "decomposition_tree",
        "Decomposition tree",
        "composition",
        "Break a total down level by level to see where it comes from.",
        (Slot("path", "Break down by (in order)", "dimension", multiple=True), VALUE_OPT),
        (AGGREGATION, Option("top_n", "Branches per level", "number", 6, minimum=2, maximum=20)),
        ("drill down", "root cause", "breakdown", "why"),
    ),
)

CHARTS_BY_ID: dict[str, ChartType] = {chart.id: chart for chart in CHARTS}

# --- Palettes --------------------------------------------------------------------------------

PALETTES: dict[str, dict[str, Any]] = {
    "vivid": {
        "name": "JT Vivid",
        "colors": [
            "#6366F1",
            "#06B6D4",
            "#F59E0B",
            "#EC4899",
            "#10B981",
            "#8B5CF6",
            "#EF4444",
            "#84CC16",
            "#F97316",
            "#14B8A6",
        ],
        "sequential": ["#0D0887", "#6A00A8", "#B12A90", "#E16462", "#FCA636", "#F0F921"],
    },
    "colorblind": {
        "name": "Colour-blind safe (Okabe–Ito)",
        "colors": ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"],
        "sequential": ["#440154", "#3B528B", "#21918C", "#5EC962", "#FDE725"],
    },
    "tableau": {
        "name": "Tableau 10",
        "colors": [
            "#4E79A7",
            "#F28E2B",
            "#E15759",
            "#76B7B2",
            "#59A14F",
            "#EDC948",
            "#B07AA1",
            "#FF9DA7",
            "#9C755F",
            "#BAB0AC",
        ],
        "sequential": ["#EFF6FF", "#BFDBFE", "#60A5FA", "#2563EB", "#1E3A8A"],
    },
    "ocean": {
        "name": "Ocean",
        "colors": ["#0B3C5D", "#1D65A6", "#328CC1", "#72B5DD", "#14B8A6", "#5EEAD4", "#0E7490", "#164E63"],
        "sequential": ["#F0FDFA", "#99F6E4", "#2DD4BF", "#0D9488", "#134E4A"],
    },
    "sunset": {
        "name": "Sunset",
        "colors": ["#F94144", "#F3722C", "#F8961E", "#F9C74F", "#90BE6D", "#43AA8B", "#577590", "#9D4EDD"],
        "sequential": ["#FCDE9C", "#FAA476", "#F0746E", "#E34F6F", "#DC3977", "#B9257A", "#7C1D6F"],
    },
    "corporate": {
        "name": "Corporate",
        "colors": ["#1E3A8A", "#2563EB", "#60A5FA", "#64748B", "#94A3B8", "#0F766E", "#F59E0B", "#DC2626"],
        "sequential": ["#EFF6FF", "#BFDBFE", "#60A5FA", "#2563EB", "#1E3A8A"],
    },
    "pastel": {
        "name": "Pastel",
        "colors": ["#A5B4FC", "#99F6E4", "#FDE68A", "#FBCFE8", "#BBF7D0", "#DDD6FE", "#FECACA", "#BAE6FD"],
        "sequential": ["#E4F1E1", "#B4D9CC", "#89C0B6", "#63A6A0", "#448C8A", "#287274", "#0D585F"],
    },
    "earth": {
        "name": "Earth",
        "colors": ["#8C510A", "#BF812D", "#DFC27D", "#80CDC1", "#35978F", "#01665E", "#543005", "#003C30"],
        "sequential": ["#EDE5CF", "#E0C2A2", "#D39C83", "#C1766F", "#A65461", "#813753", "#541F3F"],
    },
}
DEFAULT_PALETTE = "vivid"
THEMES = ("light", "dark")

# --- Prediction ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictionTask:
    id: str
    name: str
    description: str
    slots: tuple[Slot, ...]
    models: tuple[tuple[str, str], ...]
    options: tuple[Option, ...] = ()
    outputs: tuple[str, ...] = field(default_factory=tuple)


INPUTS = Slot(
    "features",
    "Input columns",
    "any",
    required=False,
    multiple=True,
    help="Empty uses every other column; categories are one-hot encoded and dates split into parts.",
)

PREDICTION_TASKS: tuple[PredictionTask, ...] = (
    PredictionTask(
        "forecast",
        "Forecast a time series",
        "Project a measure forward (sales, demand, revenue) with uncertainty bands.",
        (
            Slot("date", "Date / time", "datetime"),
            Slot("target", "Measure to forecast", "numeric"),
            Slot(
                "split_by",
                "Forecast each (optional)",
                "dimension",
                required=False,
                help="e.g. one forecast per region",
            ),
        ),
        (
            ("auto", "Best of all (chosen by backtest)"),
            ("holt_winters", "Holt–Winters exponential smoothing"),
            ("arima", "ARIMA / SARIMAX"),
            ("linear_trend", "Linear trend + seasonality"),
        ),
        (Option("horizon", "Periods ahead", "number", 12, minimum=1, maximum=365), TIME_GRAIN, AGGREGATION),
        (
            "Forecast chart with 80%/95% bands",
            "Best and worst case",
            "Backtest accuracy (MAE, MAPE)",
            "Future values table",
        ),
    ),
    PredictionTask(
        "trend",
        "Trend & seasonality analysis",
        "Growth rates, moving averages, seasonal patterns, change points and unusual periods.",
        (Slot("date", "Date / time", "datetime"), Slot("target", "Measure", "numeric")),
        (("robust", "Automatic (robust trend + anomaly detection)"),),
        (
            TIME_GRAIN,
            AGGREGATION,
            Option("window", "Moving average (periods)", "number", 3, minimum=2, maximum=52),
        ),
        (
            "Growth: total, average, latest and year-on-year",
            "Seasonal profile",
            "Change points and anomalies",
        ),
    ),
    PredictionTask(
        "drivers",
        "Key drivers (what influences an outcome)",
        "Find which factors and segments push an outcome up or down — churn, sales, satisfaction.",
        (Slot("target", "Outcome to explain", "any"), INPUTS),
        (("segments", "Segment comparison + random forest importance"),),
        (Option("positive", "Outcome value to explain (blank = automatic)", "select", "", ()),),
        ("Top influencers with lift", "Feature importance", "Best and worst segments"),
    ),
    PredictionTask(
        "regression",
        "Predict a number",
        "Estimate a numeric outcome (price, demand, score) from other columns.",
        (Slot("target", "Number to predict", "numeric"), INPUTS),
        (
            ("auto", "Best of all (cross-validated)"),
            ("linear", "Linear regression"),
            ("ridge", "Ridge regression"),
            ("random_forest", "Random forest"),
            ("gradient_boosting", "Gradient boosting"),
        ),
        (
            Option(
                "test_size",
                "Hold-out share",
                "select",
                "0.2",
                (("0.2", "20%"), ("0.25", "25%"), ("0.3", "30%")),
            ),
        ),
        ("R², MAE, RMSE", "Predicted vs actual, residuals", "Feature importance"),
    ),
    PredictionTask(
        "classification",
        "Predict a category",
        "Classify rows (churn, approval, segment) and judge the classifier.",
        (Slot("target", "Category to predict", "any"), INPUTS),
        (
            ("auto", "Best of all (cross-validated)"),
            ("logistic", "Logistic regression"),
            ("random_forest", "Random forest"),
            ("gradient_boosting", "Gradient boosting"),
        ),
        (
            Option(
                "test_size",
                "Hold-out share",
                "select",
                "0.2",
                (("0.2", "20%"), ("0.25", "25%"), ("0.3", "30%")),
            ),
        ),
        ("Accuracy, F1, ROC AUC", "Confusion matrix, ROC and PR curves", "Feature importance"),
    ),
    PredictionTask(
        "clustering",
        "Find segments",
        "Group similar rows (customers, products) without labels.",
        (INPUTS,),
        (
            ("kmeans", "k-means"),
            ("gaussian_mixture", "Gaussian mixture"),
            ("agglomerative", "Hierarchical (Ward)"),
        ),
        (CLUSTERS,),
        ("Silhouette score", "Cluster map and profiles", "Segment for every row"),
    ),
    PredictionTask(
        "anomaly",
        "Detect anomalies",
        "Flag unusual rows (fraud, faults, data errors).",
        (INPUTS,),
        (
            ("isolation_forest", "Isolation forest"),
            ("zscore", "Robust z-score"),
            ("lof", "Local outlier factor"),
        ),
        (
            Option(
                "contamination",
                "Expected share of anomalies",
                "select",
                "0.05",
                (("0.01", "1%"), ("0.02", "2%"), ("0.05", "5%"), ("0.1", "10%")),
            ),
        ),
        ("Anomaly count and share", "Anomalies highlighted on a map", "Score for every row"),
    ),
)
PREDICTION_TASKS_BY_ID = {task.id: task for task in PREDICTION_TASKS}

# --- Import formats ----------------------------------------------------------------------------

IMPORT_FORMATS: tuple[dict[str, str], ...] = (
    {"id": "csv", "name": "CSV (comma separated)", "extensions": ".csv"},
    {"id": "tsv", "name": "TSV (tab separated)", "extensions": ".tsv,.tab"},
    {"id": "txt", "name": "Delimited text (auto-detect ; | ,)", "extensions": ".txt"},
    {"id": "json", "name": "JSON (array of records)", "extensions": ".json"},
    {"id": "jsonl", "name": "JSON Lines", "extensions": ".jsonl,.ndjson"},
    {"id": "xlsx", "name": "Excel workbook (first sheet)", "extensions": ".xlsx"},
)
EXPORT_FORMATS: tuple[dict[str, str], ...] = (
    {"id": "csv", "name": "CSV"},
    {"id": "xlsx", "name": "Excel (data, profile and summary sheets)"},
    {"id": "json", "name": "JSON records"},
)


READ_GUIDE: dict[str, str] = {
    "kpi_card": "The big number is the latest period; the arrow shows the change from the period "
    "before, and the sparkline the recent trend.",
    "gauge": "The needle shows the measure against its target; green means on track, amber close, red "
    "behind.",
    "decomposition_tree": "Start from the total on the left and follow the branches to see which parts "
    "make it up; bigger nodes contribute more.",
    "bar": "Each bar's length is the value for that category; compare heights to rank them.",
    "grouped_bar": "Bars sit side by side within each category so you can compare sub-groups directly.",
    "stacked_bar": "Each bar is a total; the coloured segments show how much each sub-group contributes.",
    "percent_stacked_bar": "Every bar adds up to 100%, so segment heights show the mix within each category.",
    "lollipop": "The dot marks each category's value; the longer the stick, the larger the value.",
    "dot_plot": "Each dot is one measure for a category; dots further right are larger.",
    "dumbbell": "The two dots are the start and end values; a green bar means it rose, red that it fell.",
    "radar": "Each spoke is a measure; a larger shape means higher values across the measures.",
    "bullet": (
        "The dark bar is the actual value, the light bar and tick are the target; green means the "
        "target was met."
    ),
    "pareto": (
        "Bars are sorted largest first; the line shows the running share, so you can see which few "
        "items make up 80%."
    ),
    "category_heatmap": "Darker cells hold higher values for that row and column combination.",
    "line": "The line follows the value over time; slopes show growth or decline.",
    "area": "The filled area shows the value over time; its height is the amount at each date.",
    "stacked_area": (
        "Layers stack on top of each other, so the top edge is the total and each band is one group."
    ),
    "step_line": (
        "The value stays flat until it changes, which suits prices, stock levels and other discrete updates."
    ),
    "rolling_average": (
        "The faint line is the raw series; the bold line averages recent points to reveal the trend."
    ),
    "cumulative": (
        "The line adds every period to the previous total, so its slope shows how fast the total grows."
    ),
    "period_change": (
        "Bars show the percentage change from the previous period: green is growth, red is decline."
    ),
    "candlestick": (
        "Each candle spans the low–high range; the body runs from open to close (green up, red down)."
    ),
    "range_band": "The line is the value; the shaded band shows its lower and upper bounds.",
    "calendar_heatmap": (
        "Each square is a day; darker squares have higher values, revealing weekday and seasonal patterns."
    ),
    "seasonal_decompose": (
        "The series is split into trend (direction), seasonal (repeating pattern) and residual (noise)."
    ),
    "autocorrelation": "Bars outside the red band show lags where the series repeats itself.",
    "pie": "Each slice is a share of the whole; bigger slices are bigger shares.",
    "donut": "Each segment is a share of the total shown in the centre.",
    "treemap": "Rectangle area is proportional to value; nested rectangles break a group into its parts.",
    "sunburst": "Inner rings are top-level groups, outer rings their sub-groups; arc length is the value.",
    "icicle": "Each row is a level of the hierarchy; a block's width is its share of the total.",
    "waterfall": "Green steps add, red steps subtract, and the last bar is the resulting total.",
    "funnel": "Each stage is narrower than the last; the drop between stages is where people are lost.",
    "marimekko": "Column width is each column's share of the total; segment height is the mix within it.",
    "histogram": (
        "Bars count how many values fall in each range; the tallest bars are the most common values."
    ),
    "density_kde": "The smooth curve estimates how values are distributed; peaks are the most common values.",
    "box": (
        "The box holds the middle 50% of values, the line is the median, whiskers show the typical "
        "range and dots are outliers."
    ),
    "violin": "The width at each level shows how many values sit there; the dark bar is the middle 50%.",
    "strip": "Every dot is one row, spread sideways so overlapping values stay visible.",
    "ecdf": "For any value on the x axis, the curve shows the share of values at or below it.",
    "ridgeline": (
        "Each ridge is the distribution of one group; compare where peaks sit and how wide they are."
    ),
    "raincloud": "Each group shows its distribution (cloud), its middle 50% (bar) and every value (rain).",
    "mean_ci": (
        "Bars are group averages and whiskers the confidence interval; overlapping whiskers mean the "
        "difference may be chance."
    ),
    "qq_plot": "If points follow the red line, the data matches the chosen distribution.",
    "distribution_fit": (
        "Bars are the observed distribution; the curve is the best-fitting theoretical distribution."
    ),
    "control_chart": (
        "Points between the dashed limits are normal variation; red points outside them need attention."
    ),
    "bland_altman": (
        "Each dot compares two measurements of the same thing; the lines show the average bias and "
        "limits of agreement."
    ),
    "missing_values": (
        "Bars show the share of blanks per column; red marks in the pattern show where they occur."
    ),
    "scatter": "Each dot is one row; a rising cloud means the two measures increase together.",
    "bubble": "Like a scatter plot, but bubble size shows a third measure.",
    "regression_fit": (
        "The curve is the fitted model and the band its 95% confidence interval; R² is the share of "
        "variation explained."
    ),
    "correlation_heatmap": "Each cell is the correlation between two measures, from −1 (blue) to +1 (red).",
    "scatter_matrix": (
        "Every pair of measures gets its own scatter; the diagonal shows each measure's distribution."
    ),
    "density_heatmap": "Darker cells contain more rows, which shows where points concentrate.",
    "density_contour": "Rings enclose areas of equal point density; inner rings hold the most rows.",
    "joint_plot": "The scatter shows the relationship; the side histograms show each measure's distribution.",
    "parallel_coordinates": (
        "Each line is a row crossing every measure's axis; bundles of lines reveal common profiles."
    ),
    "parallel_categories": "Bands flow between category columns; their thickness is the number of rows.",
    "crosstab_heatmap": (
        "Cells count rows for each pair of categories; the chi-square test says whether they are related."
    ),
    "sankey": "Bands flow from one stage to the next; their width is the quantity moving between them.",
    "network": "Nodes are entities and lines are connections; bigger, darker nodes have more links.",
    "choropleth": "Countries are shaded by value; darker means higher.",
    "scatter_geo": "Each point is a location plotted by latitude and longitude.",
    "scatter_3d": "Drag to rotate; each point is a row positioned by three measures.",
    "surface_3d": (
        "The surface height and colour show the third measure across the first two; drag to rotate."
    ),
    "pca_2d": "Many measures are compressed into two components; points close together are similar rows.",
    "pca_3d": "Three principal components in 3D; drag to rotate and look for clusters.",
    "pca_biplot": "Points are rows and arrows are measures; arrows pointing the same way are correlated.",
    "scree_plot": "Bars show how much information each component holds; the line shows the running total.",
    "tsne_2d": "A non-linear map that keeps similar rows together; look for separated clusters.",
    "lda_2d": "A projection chosen to separate the labelled groups as much as possible.",
    "kmeans_clusters": "Colours show the clusters found by k-means; diamonds mark each cluster's centre.",
    "confusion_matrix": (
        "The diagonal holds correct predictions; other cells show which classes get confused."
    ),
    "roc_curve": (
        "The higher the curve sits above the diagonal, the better the model ranks positives; AUC "
        "summarises it."
    ),
    "pr_curve": "Shows the precision you keep as recall rises; higher and further right is better.",
    "calibration_curve": "Points on the dashed line mean predicted probabilities match observed rates.",
    "cumulative_gain": (
        "Shows how many positives you capture by targeting the top-scored share of rows, and the lift "
        "over random."
    ),
    "threshold_metrics": "Precision, recall and F1 at every cut-off; the dotted line marks the best balance.",
    "predicted_vs_actual": "Points on the dashed diagonal are perfect predictions; spread shows the error.",
    "residual_plot": (
        "Errors plotted against predictions; a random cloud around zero means no systematic bias."
    ),
    "error_distribution": "How large the prediction errors are; a narrow peak around zero is best.",
    "feature_importance": "Longer bars are inputs the model relies on most; whiskers show uncertainty.",
    "learning_curve": "Training and validation scores as data grows; a large gap means over-fitting.",
    "elbow": "Inertia drops as clusters are added; the elbow is where extra clusters stop helping.",
    "silhouette": "Higher bars mean better-separated clusters; the green bar is the best number of clusters.",
}


def public_catalog() -> dict[str, Any]:
    """The catalog as served to clients."""
    return {
        "version": 1,
        "categories": [asdict(category) for category in CATEGORIES],
        "charts": [
            {**asdict(chart), "howToRead": READ_GUIDE.get(chart.id, chart.description)} for chart in CHARTS
        ],
        "palettes": [{"id": key, **value} for key, value in PALETTES.items()],
        "themes": list(THEMES),
        "predictionTasks": [asdict(task) for task in PREDICTION_TASKS],
        "importFormats": list(IMPORT_FORMATS),
        "exportFormats": list(EXPORT_FORMATS),
    }


def accepts(slot_kind: str, column_kind: str) -> bool:
    if slot_kind == "any":
        return True
    if slot_kind == "dimension":
        return column_kind in {"categorical", "boolean", "datetime"}
    if slot_kind == "categorical":
        return column_kind in {"categorical", "boolean"}
    return slot_kind == column_kind


def validate_selection(
    slots: tuple[Slot, ...], options: tuple[Option, ...], fields: Any, values: Any, kinds: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate field mappings and options against the catalog; raises ``ValueError``.

    ``kinds`` maps each dataset column to its profiled kind. Numeric columns are
    accepted for dimension slots too (bucketed by the engine as labels), because
    integer codes such as years or ratings are commonly used as categories.
    """
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object mapping slots to columns.")
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError("options must be an object.")
    known_slots = {slot.key: slot for slot in slots}
    unknown = set(fields) - set(known_slots)
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    clean_fields: dict[str, Any] = {}
    for slot in slots:
        value = fields.get(slot.key)
        if value in (None, "", []):
            if slot.required:
                raise ValueError(f"Choose a column for “{slot.label}”.")
            continue
        columns = value if isinstance(value, list) else [value]
        if not slot.multiple and len(columns) != 1:
            raise ValueError(f"“{slot.label}” takes one column.")
        if len(columns) > 50:
            raise ValueError(f"“{slot.label}” takes at most 50 columns.")
        for column in columns:
            if not isinstance(column, str) or column not in kinds:
                raise ValueError(f"Unknown column {column!r} for “{slot.label}”.")
            kind = kinds[column]
            numeric_as_dimension = slot.kind in {"dimension", "categorical"} and kind == "numeric"
            if not accepts(slot.kind, kind) and not numeric_as_dimension:
                raise ValueError(f"“{slot.label}” needs a {slot.kind} column; {column} is {kind}.")
        clean_fields[slot.key] = columns if slot.multiple else columns[0]
    known_options = {option.key: option for option in options}
    clean_options: dict[str, Any] = {}
    for key, value in values.items():
        option = known_options.get(key)
        if option is None:
            continue  # unknown options are ignored, never forwarded
        if option.type == "boolean":
            clean_options[key] = bool(value)
        elif option.type == "number":
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{option.label} must be a number.") from exc
            if option.minimum is not None and number < option.minimum:
                raise ValueError(f"{option.label} must be at least {option.minimum:g}.")
            if option.maximum is not None and number > option.maximum:
                raise ValueError(f"{option.label} must be at most {option.maximum:g}.")
            clean_options[key] = number
        else:
            text = "" if value is None else str(value)
            if option.choices and text not in {choice for choice, _ in option.choices}:
                raise ValueError(f"Unsupported value for {option.label}.")
            clean_options[key] = text[:200]
    return clean_fields, clean_options


# --- Readable column names ----------------------------------------------------------------------

ACRONYMS = {
    "id",
    "uuid",
    "url",
    "kpi",
    "sku",
    "iso",
    "iso2",
    "iso3",
    "gdp",
    "roi",
    "vat",
    "usd",
    "eur",
    "ugx",
}


def nice(name: Any) -> str:
    """A column name as people read it: ``unit_price`` → ``Unit price``, ``orderId`` → ``Order ID``."""
    text = str(name)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])|(?<=[a-z])(?=[A-Z])", " ", text)
    spaced = re.sub(r"[_\s]+", " ", spaced).strip()
    if not spaced:
        return text
    words = [word.upper() if word.lower() in ACRONYMS else word for word in spaced.split(" ")]
    out = " ".join(words)
    return out[:1].upper() + out[1:]


def code_like(name: str) -> bool:
    return "_" in name or bool(re.search(r"[a-z][A-Z]", name))


def humanize_text(text: Any, mapping: dict[str, str], capital: bool = False) -> Any:
    """Replace code-style column names inside free text (titles, takeaways, notes)."""
    if not isinstance(text, str) or not mapping:
        return text
    out = text
    for column in sorted((c for c in mapping if code_like(c)), key=len, reverse=True):
        label = mapping[column]
        inline = label if label.split(" ")[0].isupper() else label[:1].lower() + label[1:]
        out = re.sub(rf"(?<![\w]){re.escape(column)}(?![\w])", inline.replace("\\", "\\\\"), out)
    return out[:1].upper() + out[1:] if capital and out else out
