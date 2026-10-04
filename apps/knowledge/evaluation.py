"""Retrieval-quality benchmark over a versioned evaluation dataset.

Each case supplies documents, a query and the labels of the documents that
should be retrieved. The benchmark ingests the documents into a temporary,
tenant-owned collection through the real pipeline (chunking, the configured
embedding provider, pgvector, full-text search, the configured reranker), runs
retrieval, and reports recall@k and MRR against the configured thresholds. The
temporary collection is always deleted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rag_regression_cases.json"


@dataclass
class CaseResult:
    name: str
    recall: float
    reciprocal_rank: float
    retrieved_labels: list[str]
    expected_labels: list[str]


@dataclass
class BenchmarkReport:
    cases: list[CaseResult] = field(default_factory=list)
    top_k: int = 5

    @property
    def mean_recall(self) -> float:
        return sum(case.recall for case in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def mrr(self) -> float:
        return sum(case.reciprocal_rank for case in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def passed(self) -> bool:
        return self.mean_recall >= settings.RAG_EVAL_MIN_RECALL and self.mrr >= settings.RAG_EVAL_MIN_MRR

    def as_dict(self) -> dict[str, Any]:
        return {
            "topK": self.top_k,
            "meanRecall": round(self.mean_recall, 4),
            "mrr": round(self.mrr, 4),
            "minRecall": settings.RAG_EVAL_MIN_RECALL,
            "minMrr": settings.RAG_EVAL_MIN_MRR,
            "passed": self.passed,
            "cases": [case.__dict__ for case in self.cases],
        }


def load_cases(path: Path | str | None = None) -> list[dict[str, Any]]:
    cases = json.loads(Path(path or DEFAULT_DATASET).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("The evaluation dataset must be a non-empty JSON list.")
    for case in cases:
        if not {"name", "query", "documents", "expected_labels"} <= set(case):
            raise ValueError(f"Case {case.get('name', '?')!r} is missing required keys.")
    return cases


def run_benchmark(
    cases: list[dict[str, Any]], *, organization: Any, user: Any, top_k: int = 5
) -> BenchmarkReport:
    """Ingest every case into a throwaway collection and score retrieval."""
    from apps.knowledge.models import Collection, Source
    from apps.knowledge.retrieval import embed_query_or_none, hybrid_retrieve
    from apps.knowledge.tasks import sync_source

    report = BenchmarkReport(top_k=top_k)
    for case in cases:
        from apps.knowledge.embeddings import get_embedding_provider

        provider = get_embedding_provider()
        collection = Collection.objects.create(
            organization=organization,
            name=f"rag-eval-{uuid.uuid4().hex[:8]}",
            description=f"Temporary evaluation collection for {case['name']}",
            embedding_provider=provider.provider_name,
            embedding_model=provider.model_name,
            embedding_dimensions=settings.VECTOR_EMBEDDING_DIMENSIONS,
            chunk_size=settings.RAG_CHUNK_SIZE,
            chunk_overlap=min(settings.RAG_CHUNK_OVERLAP, settings.RAG_CHUNK_SIZE // 2),
            created_by=user,
        )
        try:
            source_labels: dict[str, str] = {}
            for item in case["documents"]:
                source = Source.objects.create(
                    collection=collection,
                    source_type=Source.SourceType.TEXT,
                    name=item["label"],
                    config={"text": item["content"], "title": item["label"]},
                    created_by=user,
                )
                source_labels[str(source.id)] = item["label"]
                sync_source.run(str(source.id))
            query_vector, _reason = embed_query_or_none(case["query"])
            retrieval = hybrid_retrieve(
                case["query"],
                query_vector,
                collection_ids=[collection.id],
                organization_id=organization.id,
                user=user,
                top_k=top_k,
            )
            labels: list[str] = []
            for item in retrieval.results:
                label = source_labels.get(str(item.get("source_id")))
                if label and label not in labels:
                    labels.append(label)
            expected = list(case["expected_labels"])
            hits = len(set(expected) & set(labels))
            reciprocal = next((1.0 / rank for rank, label in enumerate(labels, 1) if label in expected), 0.0)
            report.cases.append(
                CaseResult(
                    name=case["name"],
                    recall=hits / len(expected) if expected else 1.0,
                    reciprocal_rank=reciprocal,
                    retrieved_labels=labels,
                    expected_labels=expected,
                )
            )
        finally:
            collection.delete()
    return report
