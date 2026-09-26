from typing import Optional

from fastapi import APIRouter, Query

from apps.metrics.crud.metric import (
    archive_metric,
    create_metric,
    create_metric_version,
    get_metric,
    list_metrics,
    publish_metric_version,
    restore_metric,
    update_metric,
)
from apps.metrics.schemas.metric import (
    MetricCreate,
    MetricDefinitionUpdate,
    MetricDimensionValueRead,
    MetricDimensionValuesUpdate,
    MetricPublish,
    MetricQueryPlanRead,
    MetricQueryPlanRequest,
    MetricVersionCreate,
)
from apps.metrics.service.dimension_values import (
    clear_manual_dimension_values,
    list_dimension_values,
    refresh_dimension_values,
    schedule_dimension_sampling,
    set_manual_dimension_values,
)
from apps.metrics.service.query_planner import preview_metric_query_plan
from apps.system.schemas.permission import SqlbotPermission, require_permissions
from common.core.deps import CurrentUser, SessionDep

router = APIRouter(tags=["Metric Catalog"], prefix="/system/metrics")


@router.get("/page/{current_page}/{page_size}")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def page(
    session: SessionDep,
    current_user: CurrentUser,
    current_page: int,
    page_size: int,
    keyword: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    datasource_id: Optional[int] = Query(default=None),
):
    return list_metrics(
        session,
        current_user.oid,
        current_page,
        page_size,
        keyword,
        status,
        datasource_id,
    )


@router.post("")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def create(session: SessionDep, current_user: CurrentUser, payload: MetricCreate):
    return create_metric(session, payload, current_user.oid, current_user.id)


@router.get("/{metric_id}")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def detail(session: SessionDep, current_user: CurrentUser, metric_id: int):
    return get_metric(session, metric_id, current_user.oid)


@router.put("/{metric_id}")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def edit(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    payload: MetricDefinitionUpdate,
):
    return update_metric(session, metric_id, payload, current_user.oid, current_user.id)


@router.post("/{metric_id}/versions")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def new_version(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    payload: MetricVersionCreate,
):
    return create_metric_version(session, metric_id, payload, current_user.oid, current_user.id)


@router.post("/{metric_id}/query-plan/preview", response_model=MetricQueryPlanRead)
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def preview_query_plan(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    payload: MetricQueryPlanRequest,
):
    return preview_metric_query_plan(
        session,
        metric_id,
        payload,
        current_user.oid,
        current_user,
    )


@router.post("/{metric_id}/versions/{version_id}/publish")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def publish(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    version_id: int,
    payload: MetricPublish,
):
    result = publish_metric_version(
        session,
        metric_id,
        version_id,
        payload.review_note,
        current_user.oid,
        current_user.id,
    )
    # Commit before the background sampler reads the newly published version.
    session.commit()
    schedule_dimension_sampling(version_id, current_user.id)
    return result


@router.get(
    "/{metric_id}/versions/{version_id}/dimension-values",
    response_model=list[MetricDimensionValueRead],
)
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def dimension_values(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    version_id: int,
):
    return list_dimension_values(session, metric_id, version_id, current_user.oid)


@router.post(
    "/{metric_id}/versions/{version_id}/dimension-values/refresh",
    response_model=list[MetricDimensionValueRead],
)
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def refresh_values(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    version_id: int,
):
    return await refresh_dimension_values(
        session, metric_id, version_id, current_user.oid, current_user.id
    )


@router.put(
    "/{metric_id}/versions/{version_id}/dimension-values/{dimension}",
    response_model=MetricDimensionValueRead,
)
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def set_values(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    version_id: int,
    dimension: str,
    payload: MetricDimensionValuesUpdate,
):
    return set_manual_dimension_values(
        session, metric_id, version_id, dimension,
        [item.model_dump() for item in payload.values], current_user.oid, current_user.id,
    )


@router.delete("/{metric_id}/versions/{version_id}/dimension-values/{dimension}")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def clear_values(
    session: SessionDep,
    current_user: CurrentUser,
    metric_id: int,
    version_id: int,
    dimension: str,
):
    clear_manual_dimension_values(session, metric_id, version_id, dimension, current_user.oid)
    return {"cleared": True}


@router.delete("/{metric_id}")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def archive(session: SessionDep, current_user: CurrentUser, metric_id: int):
    return archive_metric(session, metric_id, current_user.oid)


@router.post("/{metric_id}/restore")
@require_permissions(permission=SqlbotPermission(role=["ws_admin"]))
async def restore(session: SessionDep, current_user: CurrentUser, metric_id: int):
    return restore_metric(session, metric_id, current_user.oid)
