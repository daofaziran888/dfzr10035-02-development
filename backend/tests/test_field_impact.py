"""字段投产影响清单场景测试。

覆盖需求描述的完整验收流程：
1. 影响视图列出可用列/待核列/涉及订阅/缺少样本来源，旧主键投递已领用；
2. 对账订阅投递只认 order_id 的文件，试用订阅申请 tax；
3. 未拿到新主键可验证样本时提交确认 → 未完成，已领用交付仍按原快照读取；
4. 补齐样本后继续确认 → 数据版本保存已确认字段范围与关联差异，同步投递待办；
5. 重复提交同一确认只生效一次；
6. 两个订阅各自的列、原先那份文件的版本、交付记录回查字段定义与原始差异。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.field_impact import FieldImpactService
from app.store import store

ENDPOINT = "ENDP-ORD-0006"
DIFF_RENAME = "SCHE-ORD-0007"  # order_id -> order_no，主键改名
DIFF_ADD = "SCHE-ORD-0008"     # tax，独立新增列
RECON = "SUBS-ORD-0004"        # 对账订阅
TRIAL = "SUBS-ORD-0005"        # 试用订阅
TASK_RECON = "DELV-ORD-0001"   # 对账订阅批次01（已领用，旧主键快照）
TASK_TRIAL_1 = "DELV-ORD-0002"  # 试用订阅批次01（已领用，旧主键快照）
TASK_TRIAL_2 = "DELV-ORD-0003"  # 试用订阅批次02（待办）
CONF_KEY = "CONF-IMPA-ORD-0001"


@pytest.fixture(autouse=True)
def fresh_store():
    """每个用例前后都重建内存仓库，互不影响。"""
    store.__init__()
    yield
    store.__init__()


@pytest.fixture
def svc() -> FieldImpactService:
    return FieldImpactService()


def _versions(endpoint: str = ENDPOINT) -> list[dict]:
    return [v for v in store.rows("data_version") if v.get("关联端点") == endpoint]


def _task(task_key: str) -> dict:
    return next(t for t in store.rows("field_delivery") if t["任务编号"] == task_key)


# ---------- 影响视图 ----------

def test_impact_view_lists_columns_subscriptions_and_missing_samples(svc):
    view = svc.build_impact_view(ENDPOINT)
    # 可用列来自当前已确认版本 v1
    assert view["当前版本"]["版本序号"] == 1
    assert view["可用列"] == ["order_id", "amount"]
    # 待核列：主键改名 order_no + 独立新增列 tax，关联到待交付版本 v2
    assert [c["列名"] for c in view["待核列"]] == ["order_no", "tax"]
    assert view["待交付版本"] == 2
    assert view["关联差异"] == [DIFF_RENAME, DIFF_ADD]
    # 待改名主键仍缺上游ERP样本；独立新增列不需要样本
    assert view["缺少样本来源"] == {DIFF_RENAME: ["上游ERP样本"]}
    rename_col = next(c for c in view["待核列"] if c["列名"] == "order_no")
    add_col = next(c for c in view["待核列"] if c["列名"] == "tax")
    assert rename_col["可提交确认"] is False
    assert add_col["可提交确认"] is True
    # 涉及订阅：两个订阅都受主键改名影响；tax 尚未被申请
    subs = {s["订阅方"]: s for s in view["涉及订阅"]}
    assert set(subs) == {"对账订阅", "试用订阅"}
    assert subs["对账订阅"]["涉及差异"] == [DIFF_RENAME]
    assert subs["试用订阅"]["涉及差异"] == [DIFF_RENAME]
    # 使用旧主键的投递已经领用，快照固定在 v1
    recon_tasks = {t["任务编号"]: t for t in subs["对账订阅"]["投递任务"]}
    assert recon_tasks[TASK_RECON]["状态"] == "已领用"
    assert recon_tasks[TASK_RECON]["快照版本"] == 1


# ---------- 完整场景 ----------

def test_full_confirmation_and_delivery_scenario(svc):
    # 1) 对账订阅投递一份只认 order_id 的文件（已领用任务按原快照 v1 读取）
    recon_record, _ = svc.pull_task(TASK_RECON)
    assert recon_record["版本序号"] == 1
    assert recon_record["文件列"] == ["order_id", "amount"]
    assert "order_no" not in recon_record["文件列"]
    assert "tax" not in recon_record["文件列"]

    # 2) 试用订阅申请 tax
    sub, _ = svc.request_column(TRIAL, "tax")
    assert sub["申请字段"] == ["order_pk", "amount", "tax"]
    view = svc.build_impact_view(ENDPOINT)
    trial = next(s for s in view["涉及订阅"] if s["订阅方"] == "试用订阅")
    assert trial["待核列申请"] == ["order_no", "tax"]

    # 3) 未拿到新主键的可验证样本时提交确认 → 未完成，不产生新版本
    record, message = svc.submit_confirmation(CONF_KEY, ENDPOINT)
    assert record["status"] == "未完成"
    assert record["缺少样本"] == {DIFF_RENAME: ["上游ERP样本"]}
    assert "未完成" in message
    assert [v["版本序号"] for v in _versions()] == [1]
    assert _task(TASK_TRIAL_2)["计划版本"] == 1  # 未同步投递待办

    # 4) 确认尚未完成，试用订阅实际拉取下一批：已领用的交付仍按原快照读取
    trial_record_1, _ = svc.pull_task(TASK_TRIAL_1)
    assert trial_record_1["版本序号"] == 1
    assert "tax" not in trial_record_1["文件列"]
    # 对照视图核对：可投产列不含 tax，文件实际也不含 tax
    view = svc.build_impact_view(ENDPOINT)
    assert "tax" not in view["可用列"]
    assert "tax" in [c["列名"] for c in view["待核列"]]
    assert set(trial_record_1["文件列"]) <= set(view["可用列"])

    # 5) 补齐样本后继续确认 → 完成：版本保存已确认字段范围与关联差异，并同步投递待办
    svc.provide_sample(DIFF_RENAME, "上游ERP样本")
    assert svc.build_impact_view(ENDPOINT)["缺少样本来源"] == {}
    record, message = svc.submit_confirmation(CONF_KEY, ENDPOINT)
    assert record["status"] == "已完成"
    assert record["产生版本"] == 2
    versions = _versions()
    assert [v["版本序号"] for v in versions] == [1, 2]
    v2 = next(v for v in versions if v["版本序号"] == 2)
    assert [f["name"] for f in v2["字段范围"]] == ["order_no", "amount", "tax"]
    assert v2["关联差异"] == [DIFF_RENAME, DIFF_ADD]
    assert v2["依赖版本"] == "DATA-ORD-0001"
    assert _task(TASK_TRIAL_2)["计划版本"] == 2  # 确认结果同步到投递待办
    assert _task(TASK_TRIAL_1)["快照版本"] == 1  # 已领用任务保持原快照

    # 6) 重复提交同一确认只生效一次
    again, message = svc.submit_confirmation(CONF_KEY, ENDPOINT)
    assert again["status"] == "已完成"
    assert again["产生版本"] == 2
    assert "不再产生新版本" in message
    assert [v["版本序号"] for v in _versions()] == [1, 2]  # 没有第三版

    # 7) 试用订阅拉取下一批（已同步到 v2 的待办）→ 文件含 tax
    trial_record_2, _ = svc.pull_task(TASK_TRIAL_2)
    assert trial_record_2["版本序号"] == 2
    assert trial_record_2["文件列"] == ["order_no", "amount", "tax"]

    # 8) 终检：两个订阅各自的列、原先那份文件的版本、交付记录回查
    assert recon_record["文件列"] == ["order_id", "amount"]
    assert recon_record["版本序号"] == 1  # 原先那份文件仍是 v1
    trace_recon = svc.delivery_record_trace(recon_record["记录编号"])
    assert [f["name"] for f in trace_recon["字段定义"]] == ["order_id", "amount"]
    assert trace_recon["原始差异"] == []  # 基线版本无关联差异
    trace_trial = svc.delivery_record_trace(trial_record_2["记录编号"])
    assert [f["name"] for f in trace_trial["字段定义"]] == ["order_no", "amount", "tax"]
    assert [d["记录编号"] for d in trace_trial["原始差异"]] == [DIFF_RENAME, DIFF_ADD]
    rename_diff = next(d for d in trace_trial["原始差异"] if d["记录编号"] == DIFF_RENAME)
    assert rename_diff["旧定义"] == "order_id:string:主键(在用)"
    assert rename_diff["新列名"] == "order_no"

    # 9) 清单已刷新：tax 投产，待核列清空
    view = svc.build_impact_view(ENDPOINT)
    assert view["可用列"] == ["order_no", "amount", "tax"]
    assert view["待核列"] == []
    row = next(r for r in store.rows("field_impact") if r["关联端点"] == ENDPOINT)
    assert row["可用列"] == "order_no,amount,tax"
    assert row["缺少样本来源"] == "无"


# ---------- 专项行为 ----------

def test_claimed_delivery_reads_original_snapshot_even_after_confirmation(svc):
    """确认尚未完成时已经领用的交付按原快照读取；确认完成后也不漂移到新版本。"""
    svc.claim_task(TASK_TRIAL_2)  # 在 v1 时领用，快照固定 v1
    svc.provide_all_missing_samples(ENDPOINT)
    svc.submit_confirmation(CONF_KEY, ENDPOINT)  # 确认完成，产生 v2
    record, _ = svc.pull_task(TASK_TRIAL_2)
    assert record["版本序号"] == 1
    assert record["文件列"] == ["order_id", "amount"]


def test_each_file_uses_exactly_one_complete_field_version(svc):
    svc.provide_all_missing_samples(ENDPOINT)
    svc.submit_confirmation(CONF_KEY, ENDPOINT)
    record, _ = svc.pull_task(TASK_TRIAL_2)
    version = next(v for v in _versions() if v["版本序号"] == record["版本序号"])
    version_columns = {f["name"] for f in version["字段范围"]}
    # 文件列全部取自同一个完整字段版本，不与其他版本混用
    assert set(record["文件列"]) <= version_columns
    assert record["版本编号"] == version["版本编号"]
    other = next(v for v in _versions() if v["版本序号"] != record["版本序号"])
    assert not set(record["文件列"]) <= {f["name"] for f in other["字段范围"]}


def test_add_column_diff_is_independent_and_needs_no_sample(svc):
    diff = next(d for d in store.rows("schema_diff") if d["记录编号"] == DIFF_ADD)
    assert diff["差异类型"] == "add"
    assert svc._verifiable(diff) is True  # 独立新增列无需样本
    view = svc.build_impact_view(ENDPOINT)
    assert DIFF_ADD not in view["缺少样本来源"]


def test_module_action_submit_confirmation_is_idempotent(svc):
    svc.provide_all_missing_samples(ENDPOINT)
    entry = store.rows("field_impact")[0]
    _, message_1 = svc.run_action(entry["id"], "提交确认")
    assert "已完成" in message_1
    assert entry["status"] == "可投产"
    _, message_2 = svc.run_action(entry["id"], "提交确认")
    assert "不再产生新版本" in message_2
    assert [v["版本序号"] for v in _versions()] == [1, 2]
    confirmations = store.rows("field_confirmation")
    assert len(confirmations) == 1
    assert confirmations[0]["提交次数"] == 2  # 重复提交被接收但只生效一次


def test_module_action_flow_on_impact_list(svc):
    entry = store.rows("field_impact")[0]
    _, message = svc.run_action(entry["id"], "提交确认")
    assert "未完成" in message
    assert entry["status"] == "样本待核"
    _, message = svc.run_action(entry["id"], "补齐样本")
    assert "上游ERP样本" in message
    _, message = svc.run_action(entry["id"], "提交确认")
    assert "已完成" in message
    assert entry["status"] == "可投产"


# ---------- HTTP 工作流冒烟 ----------

def test_workflow_api_smoke():
    client = TestClient(app)
    view = client.get(f"/api/field-impact/{ENDPOINT}/view").json()
    assert view["可用列"] == ["order_id", "amount"]
    assert view["缺少样本来源"] == {DIFF_RENAME: ["上游ERP样本"]}

    resp = client.post(f"/api/field-impact/{ENDPOINT}/confirm", json={"key": "CONF-API-1"}).json()
    assert resp["confirmation"]["status"] == "未完成"

    client.post(f"/api/field-impact/diffs/{DIFF_RENAME}/samples", json={"source": "上游ERP样本"})
    client.post(f"/api/field-impact/subscriptions/{TRIAL}/columns", json={"field": "tax"})
    resp = client.post(f"/api/field-impact/{ENDPOINT}/confirm", json={"key": "CONF-API-1"}).json()
    assert resp["confirmation"]["status"] == "已完成"
    resp = client.post(f"/api/field-impact/{ENDPOINT}/confirm", json={"key": "CONF-API-1"}).json()
    assert "不再产生新版本" in resp["message"]

    resp = client.post(f"/api/field-impact/deliveries/{TASK_TRIAL_2}/pull").json()
    assert resp["record"]["文件列"] == ["order_no", "amount", "tax"]
    trace = client.get(f"/api/field-impact/deliveries/records/{resp['record']['记录编号']}").json()
    assert [d["记录编号"] for d in trace["原始差异"]] == [DIFF_RENAME, DIFF_ADD]

    # 通用资源路由仍然接管 field_impact 模块的清单列表
    listing = client.get("/api/field_impact").json()
    assert listing["total"] == 1
    assert listing["items"][0]["清单编号"] == "IMPA-ORD-0001"
