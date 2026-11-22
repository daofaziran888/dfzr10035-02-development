"""字段投产影响工作流接口：影响视图、确认提交、样本登记、订阅申请、投递领用/拉取、交付记录回查。

挂在通用资源路由之前注册，避免 /api/field-impact/* 被 /api/{module} 动态段抢先匹配。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.field_impact import FieldImpactService

router = APIRouter(prefix="/api/field-impact", tags=["字段投产影响"])


class ConfirmPayload(BaseModel):
    key: str = Field(min_length=1)


class SamplePayload(BaseModel):
    source: str = Field(min_length=1)


class ColumnPayload(BaseModel):
    field: str = Field(min_length=1)


class BatchPayload(BaseModel):
    subscription: str = Field(min_length=1)
    batch: str = Field(min_length=1)


@router.get("/{endpoint}/view")
def impact_view(endpoint: str) -> dict:
    """字段投产影响视图：可用列/待核列/涉及订阅/缺少样本来源/待交付版本。"""
    return FieldImpactService().build_impact_view(endpoint)


@router.post("/{endpoint}/confirm")
def submit_confirmation(endpoint: str, payload: ConfirmPayload) -> dict:
    """提交确认：样本不齐则不完成；重复提交同一确认只生效一次。"""
    record, message = FieldImpactService().submit_confirmation(payload.key, endpoint)
    return {"ok": True, "message": message, "confirmation": record}


@router.post("/diffs/{diff_key}/samples")
def provide_sample(diff_key: str, payload: SamplePayload) -> dict:
    """登记差异所需来源的可验证样本。"""
    diff, message = FieldImpactService().provide_sample(diff_key, payload.source)
    if diff is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "diff": diff}


@router.post("/subscriptions/{sub_key}/columns")
def request_column(sub_key: str, payload: ColumnPayload) -> dict:
    """订阅方申请字段（如试用订阅申请 tax）。"""
    sub, message = FieldImpactService().request_column(sub_key, payload.field)
    if sub is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "subscription": sub}


@router.post("/deliveries/next-batch")
def open_next_batch(payload: BatchPayload) -> dict:
    """为订阅开立下一批投递待办。"""
    task, message = FieldImpactService().open_next_batch(payload.subscription, payload.batch)
    if task is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "task": task}


@router.post("/deliveries/{task_key}/claim")
def claim_task(task_key: str) -> dict:
    """领用投递任务：按当前计划版本固定快照。"""
    task, message = FieldImpactService().claim_task(task_key)
    if task is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "task": task}


@router.post("/deliveries/{task_key}/pull")
def pull_task(task_key: str) -> dict:
    """拉取并交付：按任务快照版本组装文件，每份文件只用一个完整字段版本。"""
    record, message = FieldImpactService().pull_task(task_key)
    if record is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "record": record}


@router.get("/deliveries/records/{record_key}")
def delivery_record_trace(record_key: str) -> dict:
    """交付记录回查：文件所用的字段定义和原始差异。"""
    trace = FieldImpactService().delivery_record_trace(record_key)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"交付记录{record_key}不存在")
    return trace
