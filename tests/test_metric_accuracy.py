"""Candidate recall and known dimension values for governed metric planning."""
import asyncio
import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from test_graph_gateway import (  # noqa: F401 — shared isolated fixtures
    configured,
    install_isolated_sqlite_execution,
    install_metric_catalog,
)

from apps.graph_gateway import service
from apps.graph_gateway.contracts import MetricCandidateRef
from apps.metrics.crud import metric as metric_crud
from apps.metrics.models.metric import (
    MetricDefinition,
    MetricDimensionValue,
    MetricVersion,
)
from apps.metrics.service import dimension_values, recall
from common.core.config import settings

ADMIN = SimpleNamespace(id=1, account="admin", oid=2)


def add_metric(session, metric_id, code, name, aliases=(), description=None, dimensions=("region",)):
    session.add(MetricDefinition(id=metric_id, oid=2, code=code, name=name, aliases=list(aliases),
                                 description=description, datasource_id=3, owner_user_id=7,
                                 status="published", current_version_id=metric_id * 10))
    session.add(MetricVersion(id=metric_id * 10, metric_id=metric_id, version=1, expression="amount",
                              aggregation="SUM", required_tables=["orders"],
                              dimensions=list(dimensions), status="published",
                              validation_status="approved", created_by=7))


@pytest.fixture
def catalog(configured):
    install_metric_catalog(configured)
    with Session(configured) as session:
        add_metric(session, 11, "buyer_count", "下单客户数", aliases=["买家数"])
        add_metric(session, 12, "gmv", "成交总额", aliases=["GMV"], description="已支付订单的原始金额合计")
        session.commit()
    return configured


def candidate_codes(session, question):
    return [item["metric_code"] for item in metric_crud.get_metric_candidates(
        session, question, 2, 3, limit=10, current_user=ADMIN)]


# --- Recall ---------------------------------------------------------------

def test_character_bigrams_recall_chinese_paraphrases_only_for_governed_planning(catalog, monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_ENABLED", False)
    question = "上个月下单的客户有多少"
    with Session(catalog) as session:
        assert candidate_codes(session, question) == ["buyer_count"]
        # The legacy SQL prompt keeps its exact-containment behavior.
        assert metric_crud._rank_published_metrics(session, question, 2, 3, 10, ADMIN) == []


def test_exact_alias_still_outranks_fuzzy_matches(catalog, monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_ENABLED", False)
    with Session(catalog) as session:
        ranked = metric_crud.get_metric_candidates(session, "上个月买家数和下单客户", 2, 3,
                                                   limit=10, current_user=ADMIN)
    assert ranked[0]["metric_code"] == "buyer_count" and ranked[0]["score"] >= 500


class FakeEmbeddings:
    """Two-dimensional vectors: 'sales' questions point at GMV's description."""

    def __init__(self):
        self.documents = 0

    def embed_documents(self, texts):
        self.documents += len(texts)
        return [[1.0, 0.0] if "原始金额" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, text):
        return [0.95, 0.31] if "卖了多少钱" in text else [0.0, 1.0]


@pytest.fixture
def embeddings(monkeypatch):
    from apps.ai_model.embedding import EmbeddingModelCache
    model = FakeEmbeddings()
    monkeypatch.setattr(settings, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(EmbeddingModelCache, "get_model", staticmethod(lambda *args, **kwargs: model))
    monkeypatch.setattr(recall, "_disabled_until", 0.0)
    recall._cache.clear()
    return model


def test_embedding_recall_finds_metrics_without_lexical_overlap(catalog, embeddings):
    with Session(catalog) as session:
        assert "gmv" in candidate_codes(session, "八月卖了多少钱")
        # Metric texts are embedded once and cached per version and text.
        documents = embeddings.documents
        candidate_codes(session, "八月卖了多少钱")
    assert embeddings.documents == documents


def test_embedding_scores_stay_below_alias_matches_and_respect_threshold(catalog, embeddings, monkeypatch):
    with Session(catalog) as session:
        ranked = metric_crud.get_metric_candidates(session, "买家数卖了多少钱", 2, 3,
                                                   limit=10, current_user=ADMIN)
        assert [item["metric_code"] for item in ranked][:2] == ["buyer_count", "gmv"]
        monkeypatch.setattr(settings, "GRAPH_METRIC_VECTOR_MIN_SIMILARITY", 0.99)
        assert "gmv" not in candidate_codes(session, "八月卖了多少钱")


def test_embedding_failure_falls_back_to_lexical_and_backs_off(catalog, monkeypatch):
    from apps.ai_model.embedding import EmbeddingModelCache
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise OSError("model files missing")

    monkeypatch.setattr(settings, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(EmbeddingModelCache, "get_model", staticmethod(broken))
    monkeypatch.setattr(recall, "_disabled_until", 0.0)
    with Session(catalog) as session:
        assert candidate_codes(session, "上个月买家数") == ["buyer_count"]
        assert candidate_codes(session, "上个月买家数") == ["buyer_count"]
    assert len(calls) == 1


# --- Dimension values ------------------------------------------------------

def sample(session, version_id=27, values=None, error=None):
    def execute(_datasource, sql):
        assert "GROUP BY" in sql and "COUNT(*) DESC" in sql
        if error:
            raise error
        return values

    return dimension_values.sample_version_dimension_values(session, version_id, 1, execute=execute)


def test_sampling_stores_json_values_and_skips_unusable_ones(catalog):
    with Session(catalog) as session:
        [row] = sample(session, values=["华东", Decimal("3"), Decimal("2.5"),
                                        datetime.date(2026, 8, 1), None, "x" * 65])
        session.commit()
        assert row.status == "ok" and row.source == "sampled"
        assert [item["value"] for item in row.values] == ["华东", 3, 2.5, "2026-08-01"]


def test_sampling_marks_high_cardinality_and_failures(catalog, monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_DIMENSION_VALUE_LIMIT", 3)
    with Session(catalog) as session:
        [row] = sample(session, values=["a", "b", "c", "d"])
        assert row.status == "high_cardinality" and row.values == []
        [row] = sample(session, error=RuntimeError("connection refused"))
        assert row.status == "failed" and row.values == []
        assert len(session.exec(select(MetricDimensionValue)).all()) == 1  # upserted, not duplicated


def test_sampling_runs_the_real_read_only_path(catalog, monkeypatch, tmp_path):
    install_isolated_sqlite_execution(monkeypatch, tmp_path, regions=3, stub_compiler=False)
    with Session(catalog) as session:
        [row] = dimension_values.sample_version_dimension_values(session, 27, 1)
    assert row.status == "ok"
    assert sorted(item["value"] for item in row.values) == ["region-0", "region-1", "region-2"]


def test_manual_values_survive_sampling_and_can_be_cleared(catalog):
    with Session(catalog) as session:
        read = dimension_values.set_manual_dimension_values(
            session, 9, 27, "REGION", [{"value": "east", "label": "东部"}], 2, 7)
        assert read["dimension"] == "region" and read["source"] == "manual"
        [row] = sample(session, values=["east", "west"])
        assert row.source == "manual" and row.values == [{"value": "east", "label": "东部"}]
        dimension_values.clear_manual_dimension_values(session, 9, 27, "region", 2)
        assert dimension_values.list_dimension_values(session, 9, 27, 2) == []


@pytest.mark.parametrize("metric_id,version_id,dimension,status", [
    (9, 27, "customer_id", 422),  # not a declared dimension
    (11, 27, "region", 404),      # version belongs to another metric
    (99, 27, "region", 404),
])
def test_manual_values_are_bound_to_the_workspace_version(catalog, metric_id, version_id, dimension, status):
    with Session(catalog) as session:
        with pytest.raises(HTTPException) as error:
            dimension_values.set_manual_dimension_values(
                session, metric_id, version_id, dimension, [{"value": "x", "label": None}], 2, 7)
    assert error.value.status_code == status


def test_manual_value_payload_validation():
    from pydantic import ValidationError

    from apps.metrics.schemas.metric import MetricDimensionValuesUpdate
    assert MetricDimensionValuesUpdate.model_validate(
        {"values": [{"value": "east", "label": " 东部 "}, {"value": 3}]}
    ).values[0].label == "东部"
    for bad in ({"values": []}, {"values": [{"value": True}]}, {"values": [{"value": ""}]},
                {"values": [{"value": "x", "extra": 1}]}):
        with pytest.raises(ValidationError):
            MetricDimensionValuesUpdate.model_validate(bad)


def test_refresh_samples_in_its_own_session(catalog, monkeypatch):
    monkeypatch.setattr(dimension_values, "engine", catalog)
    monkeypatch.setattr(dimension_values, "_default_execute", lambda _ds, _sql: ["华东"])
    with Session(catalog) as session:
        rows = asyncio.run(dimension_values.refresh_dimension_values(session, 9, 27, 2, 7))
    assert rows[0]["values"] == [{"value": "华东", "label": None}]


def candidate(version_id=27, tables=("orders",)):
    return {"metric_id": version_id // 3, "metric_version_id": version_id,
            "required_tables": list(tables), "dimensions": ["region"]}


def test_planning_values_are_withheld_from_row_restricted_users(catalog, monkeypatch):
    import apps.datasource.crud.permission as permission
    with Session(catalog) as session:
        dimension_values.set_manual_dimension_values(
            session, 9, 27, "region", [{"value": "east", "label": "东部"}, {"value": "west", "label": None}], 2, 7)
        session.commit()
        monkeypatch.setattr(permission, "get_row_permission_filters", lambda **kwargs: [])
        assert dimension_values.planning_dimension_values(session, [candidate()], ADMIN, 3) == {
            27: {"region": [{"value": "east", "label": "东部"}, "west"]}}
        monkeypatch.setattr(permission, "get_row_permission_filters", lambda **kwargs: [
            {"table": "orders", "filter": "(\"region\" = 'east')"}])
        assert dimension_values.planning_dimension_values(session, [candidate()], ADMIN, 3) == {}


def test_unsampled_versions_are_queued_once_for_background_sampling(catalog, monkeypatch):
    import apps.datasource.crud.permission as permission
    from common.utils import embedding_threads
    submitted = []
    monkeypatch.setattr(permission, "get_row_permission_filters", lambda **kwargs: [])
    monkeypatch.setattr(embedding_threads.executor, "submit", lambda *args: submitted.append(args[1:]))
    monkeypatch.setattr(dimension_values, "_last_attempt", {})
    monkeypatch.setattr(dimension_values, "_inflight", set())
    with Session(catalog) as session:
        for _ in range(2):
            assert dimension_values.planning_dimension_values(session, [candidate()], ADMIN, 3) == {}
    assert submitted == [(27, None)]
    # Automatic sampling touches customer databases only when the experiment is on.
    monkeypatch.setattr(settings, "GRAPH_EXPERIMENT_ENABLED", False)
    assert dimension_values.schedule_dimension_sampling(28) is False


def test_planning_prompt_lists_known_values(catalog, monkeypatch):
    from test_graph_gateway import metric_candidate

    import apps.datasource.crud.permission as permission
    from apps.ai_model.model_factory import LLMConfig
    captured = []
    monkeypatch.setattr(permission, "get_row_permission_filters", lambda **kwargs: [])
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])
    with Session(catalog) as session:
        dimension_values.set_manual_dimension_values(
            session, 9, 27, "region", [{"value": "east", "label": "东部"}], 2, 7)
        session.commit()

    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test")

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(content="{}", usage_metadata=None)

    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    asyncio.run(service.invoke_metric_model(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"},
        "八月东部净销售额", 3, [MetricCandidateRef(metric_id=9, metric_version_id=27)]))
    prompt = captured[0].content
    assert '"dimension_values":{"region":[{"value":"east","label":"东部"}]}' in prompt
    assert "must be copied exactly" in prompt
