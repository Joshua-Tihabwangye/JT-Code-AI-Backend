"""Built-in sample datasets for trying the visualization tool (deterministic, generated)."""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Sample:
    id: str
    name: str
    description: str
    audience: str
    build: Callable[[], Any]


def _rng(seed: int) -> Any:
    import numpy as np

    return np.random.default_rng(seed)


def retail_sales() -> Any:
    import numpy as np
    import pandas as pd

    rng = _rng(7)
    dates = pd.date_range("2024-01-01", "2025-12-31", freq="D")
    regions = {
        "East": ("Kenya", 1.0),
        "West": ("Nigeria", 1.25),
        "North": ("Egypt", 0.8),
        "South": ("South Africa", 1.1),
        "Central": ("Uganda", 0.7),
    }
    products = {"Laptops": 820.0, "Phones": 410.0, "Tablets": 290.0, "Accessories": 35.0, "Monitors": 180.0}
    channels = ("Online", "Retail store", "Partner")
    rows = []
    for _ in range(2400):
        day = dates[rng.integers(len(dates))]
        region = rng.choice(list(regions))
        product = rng.choice(list(products))
        channel = rng.choice(channels, p=[0.5, 0.35, 0.15])
        season = 1 + 0.25 * np.sin(2 * np.pi * day.dayofyear / 365) + (0.35 if day.month in (11, 12) else 0)
        growth = 1 + 0.18 * (day - dates[0]).days / 365
        units = max(1, int(rng.poisson(6 * season * growth * regions[region][1])))
        discount = float(rng.choice([0, 0, 0, 0.05, 0.1, 0.15, 0.2]))
        price = products[product] * rng.uniform(0.9, 1.1)
        revenue = round(units * price * (1 - discount), 2)
        cost = round(units * products[product] * rng.uniform(0.55, 0.75), 2)
        rows.append(
            {
                "date": day.date().isoformat(),
                "region": region,
                "country": regions[region][0],
                "product": product,
                "channel": channel,
                "units": units,
                "unit_price": round(price, 2),
                "discount": discount,
                "revenue": revenue,
                "cost": cost,
                "profit": round(revenue - cost, 2),
                "target_revenue": round(revenue * rng.uniform(0.85, 1.2), 2),
                "satisfaction": int(np.clip(round(rng.normal(4 - 4 * discount, 0.8)), 1, 5)),
            }
        )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def customer_churn() -> Any:
    import numpy as np
    import pandas as pd

    rng = _rng(11)
    count = 1500
    tenure = rng.integers(1, 72, count)
    contract = rng.choice(["Month-to-month", "One year", "Two year"], count, p=[0.55, 0.25, 0.2])
    monthly = np.round(rng.uniform(18, 120, count), 2)
    calls = rng.poisson(1.5, count)
    satisfaction = np.clip(np.round(rng.normal(3.6, 1.0, count)), 1, 5).astype(int)
    payment = rng.choice(["Card", "Bank transfer", "Mobile money", "Cash"], count)
    logit = (
        -1.2
        + 0.9 * (contract == "Month-to-month")
        - 0.035 * tenure
        + 0.012 * monthly
        + 0.35 * calls
        - 0.45 * (satisfaction - 3)
    )
    probability = 1 / (1 + np.exp(-logit))
    churned = rng.uniform(size=count) < probability
    noisy = np.clip(probability + rng.normal(0, 0.12, count), 0.001, 0.999)
    return pd.DataFrame(
        {
            "customer_id": [f"C{10000 + i}" for i in range(count)],
            "tenure_months": tenure,
            "contract": contract,
            "payment_method": payment,
            "monthly_charges": monthly,
            "total_charges": np.round(monthly * tenure * rng.uniform(0.95, 1.05, count), 2),
            "support_calls": calls,
            "satisfaction": satisfaction,
            "churned": np.where(churned, "Yes", "No"),
            "churn_score": np.round(noisy, 4),
            "predicted_churn": np.where(noisy >= 0.5, "Yes", "No"),
        }
    )


def stock_prices() -> Any:
    import numpy as np
    import pandas as pd

    rng = _rng(3)
    frames = []
    for ticker, start, drift in (("JTC", 120.0, 0.0006), ("ACME", 80.0, 0.0002), ("NOVA", 45.0, 0.0011)):
        dates = pd.bdate_range("2024-01-02", periods=480)
        close = start * np.exp(np.cumsum(rng.normal(drift, 0.018, len(dates))))
        open_ = close * (1 + rng.normal(0, 0.006, len(dates)))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, len(dates))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, len(dates))))
        frames.append(
            pd.DataFrame(
                {
                    "date": dates.date.astype(str),
                    "ticker": ticker,
                    "open": open_.round(2),
                    "high": high.round(2),
                    "low": low.round(2),
                    "close": close.round(2),
                    "volume": rng.integers(2e5, 3e6, len(dates)),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def house_prices() -> Any:
    import numpy as np
    import pandas as pd

    rng = _rng(5)
    count = 1200
    hoods = {
        "Kololo": (0.3326, 32.5953, 1.6),
        "Ntinda": (0.3537, 32.6155, 1.15),
        "Muyenga": (0.2989, 32.6126, 1.3),
        "Kira": (0.3970, 32.6406, 0.9),
        "Entebbe": (0.0512, 32.4637, 1.0),
    }
    hood = rng.choice(list(hoods), count)
    area = np.round(rng.lognormal(7.2, 0.35, count))
    bedrooms = np.clip(np.round(area / 450 + rng.normal(0, 0.7, count)), 1, 7).astype(int)
    bathrooms = np.clip(bedrooms - rng.integers(0, 2, count), 1, 6)
    age = rng.integers(0, 45, count)
    distance = np.round(rng.gamma(2.2, 3.5, count), 2)
    factor = np.array([hoods[h][2] for h in hood])
    price = np.round(
        (area * 210 + bedrooms * 9000 + bathrooms * 6000 - age * 900 - distance * 2500) * factor
        + rng.normal(0, 15000, count),
        -2,
    )
    return pd.DataFrame(
        {
            "neighborhood": hood,
            "area_sqft": area.astype(int),
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "age_years": age,
            "distance_km": distance,
            "price": price,
            "predicted_price": np.round(price * rng.normal(1, 0.08, count), -2),
            "lat": np.round([hoods[h][0] + rng.normal(0, 0.01) for h in hood], 5),
            "lon": np.round([hoods[h][1] + rng.normal(0, 0.01) for h in hood], 5),
        }
    )


def website_traffic() -> Any:
    import numpy as np
    import pandas as pd

    rng = _rng(9)
    dates = pd.date_range("2025-01-01", periods=365, freq="D")
    rows = []
    for source, base_, rate in (
        ("Organic search", 900, 0.031),
        ("Paid ads", 520, 0.045),
        ("Social", 640, 0.018),
        ("Email", 210, 0.062),
        ("Referral", 160, 0.038),
    ):
        weekly = 1 + 0.25 * np.sin(2 * np.pi * dates.dayofweek / 7)
        sessions = rng.poisson(base_ * weekly * (1 + np.arange(len(dates)) / 900))
        conversions = rng.binomial(sessions, rate)
        for day, s, c in zip(dates, sessions, conversions, strict=True):
            rows.append(
                {
                    "date": day.date().isoformat(),
                    "source": source,
                    "stage_visit": int(s),
                    "stage_signup": int(c * 3),
                    "stage_purchase": int(c),
                    "sessions": int(s),
                    "conversions": int(c),
                    "bounce_rate": round(float(np.clip(rng.normal(0.45, 0.08), 0.1, 0.9)), 3),
                }
            )
    return pd.DataFrame(rows)


def world_indicators() -> Any:
    import pandas as pd

    data = [
        ("Uganda", "UGA", "Africa", 1014, 63.4, 47.2, 0.13),
        ("Kenya", "KEN", "Africa", 2099, 61.4, 54.0, 0.39),
        ("Nigeria", "NGA", "Africa", 1597, 52.7, 223.8, 0.6),
        ("South Africa", "ZAF", "Africa", 6766, 62.3, 60.4, 6.7),
        ("Egypt", "EGY", "Africa", 3512, 70.2, 112.7, 2.3),
        ("Ghana", "GHA", "Africa", 2260, 64.1, 34.1, 0.6),
        ("Rwanda", "RWA", "Africa", 1000, 66.1, 14.1, 0.11),
        ("Ethiopia", "ETH", "Africa", 1272, 65.0, 126.5, 0.15),
        ("Germany", "DEU", "Europe", 52746, 80.6, 84.5, 7.9),
        ("France", "FRA", "Europe", 44461, 82.3, 68.2, 4.6),
        ("United Kingdom", "GBR", "Europe", 48867, 80.7, 68.3, 4.7),
        ("Italy", "ITA", "Europe", 38373, 82.9, 58.9, 5.4),
        ("Spain", "ESP", "Europe", 32677, 83.1, 48.4, 4.9),
        ("Poland", "POL", "Europe", 22113, 77.6, 36.7, 7.8),
        ("United States", "USA", "Americas", 80412, 77.4, 334.9, 14.3),
        ("Canada", "CAN", "Americas", 53371, 81.3, 40.1, 14.2),
        ("Brazil", "BRA", "Americas", 10043, 73.4, 216.4, 2.2),
        ("Mexico", "MEX", "Americas", 13926, 75.0, 128.5, 3.3),
        ("Argentina", "ARG", "Americas", 13730, 76.1, 46.7, 4.2),
        ("China", "CHN", "Asia", 12614, 78.6, 1410.7, 8.9),
        ("India", "IND", "Asia", 2485, 72.0, 1428.6, 2.0),
        ("Japan", "JPN", "Asia", 33834, 84.5, 124.5, 8.5),
        ("Indonesia", "IDN", "Asia", 4941, 71.0, 277.5, 2.6),
        ("Vietnam", "VNM", "Asia", 4347, 74.6, 98.9, 3.5),
        ("Australia", "AUS", "Oceania", 64712, 83.1, 26.6, 14.9),
        ("New Zealand", "NZL", "Oceania", 48249, 82.1, 5.2, 6.2),
    ]
    return pd.DataFrame(
        data,
        columns=[
            "country",
            "iso3",
            "continent",
            "gdp_per_capita",
            "life_expectancy",
            "population_millions",
            "co2_per_capita",
        ],
    )


SAMPLES: tuple[Sample, ...] = (
    Sample(
        "retail_sales",
        "Retail sales",
        "Two years of orders by region, product and channel.",
        "Small business",
        retail_sales,
    ),
    Sample(
        "customer_churn",
        "Customer churn",
        "Subscribers, contracts and a churn model's scores.",
        "Enterprise",
        customer_churn,
    ),
    Sample(
        "website_traffic",
        "Website traffic",
        "Daily sessions and conversions by traffic source.",
        "Marketing",
        website_traffic,
    ),
    Sample(
        "house_prices",
        "House prices",
        "Homes with size, location and a price model's predictions.",
        "Analysts",
        house_prices,
    ),
    Sample(
        "stock_prices",
        "Stock prices",
        "Daily open/high/low/close for three tickers.",
        "Finance",
        stock_prices,
    ),
    Sample(
        "world_indicators",
        "World indicators",
        "GDP, life expectancy and emissions by country.",
        "Everyone",
        world_indicators,
    ),
)
SAMPLES_BY_ID = {sample.id: sample for sample in SAMPLES}


def sample_csv(sample_id: str) -> str:
    sample = SAMPLES_BY_ID[sample_id]
    buffer = io.StringIO()
    sample.build().to_csv(buffer, index=False)
    return buffer.getvalue()
