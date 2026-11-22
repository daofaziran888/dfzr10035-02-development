"""字段投产影响清单领域服务：把端点字段差异与待交付版本关联，汇总差异对订阅投递的影响。

联动 schema_diff(字段差异)、data_version(字段版本)、subscriber(订阅申请)、
field_delivery(投递待办)、field_delivery_record(交付记录)、field_confirmation(确认记录)：

- 影响视图：可用列 / 待核列 / 涉及订阅 / 缺少样本来源 / 待交付版本，供发布前检查；
- 确认工作流：待改名主键需拿到全部来源的可验证样本才能确认完成，
  重复提交同一确认只生效一次（按确认键幂等）；
- 确认完成：数据版本保存已确认的字段范围和关联差异，确认结果同步到投递待办；
- 文件组装：每份交付文件只用一个完整字段版本；确认未完成时，
  已经领用的交付仍按原快照读取；文件所用的字段定义和原始差异可从交付记录回查。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

TODAY = "2026-11-22"

# 投递待办任务状态
TASK_PENDING = "待办"
TASK_CLAIMED = "已领用"
TASK_DELIVERED = "已交付"

# 确认记录状态
CONF_INCOMPLETE = "未完成"
CONF_COMPLETE = "已完成"


def _next_id(rows: list[dict]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _endpoint_tag(endpoint: str) -> str:
    return endpoint.replace("ENDP-", "").split("-")[0] or "ORD"


class FieldImpactService(BaseModuleService):
    MODULE = "field_impact"
    ENTITY = "影响清单"
    LIST_FIELDS = ["清单编号", "关联端点", "当前版本", "待交付版本", "可用列", "待核列",
                   "涉及订阅", "缺少样本来源", "关联差异", "检查结论", "生成时间"]
    REQUIRED_FIELDS = ["清单编号", "关联端点"]
    STATUS_ORDER = ["待检查", "样本待核", "可投产"]
    ACTION_RULES = {"提交确认": "可投产", "补齐样本": "待检查", "刷新清单": "待检查"}
    NEGATIVE_ACTIONS = []

    # ---------- 模块动作：清单上的发布前检查操作 ----------
    def run_action(self, entry_id: int, action: str):
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        endpoint = str(entry.get("关联端点", ""))
        if action == "提交确认":
            record, message = self.submit_confirmation(f"CONF-{entry.get('清单编号')}", endpoint)
            return entry, message
        if action == "补齐样本":
            arrived = self.provide_all_missing_samples(endpoint)
            if not arrived:
                return entry, "没有待补齐的样本来源"
            if entry["status"] == "样本待核":
                entry["status"] = "待检查"
            return entry, f"已登记样本来源：{'、'.join(arrived)}，请重新提交确认"
        if action == "刷新清单":
            self.refresh_impact_list(endpoint)
            return entry, "已按最新字段差异与投递待办刷新清单"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def create_entry(self, values: dict[str, Any]):
        entry, missing = super().create_entry(values)
        if entry is not None:
            # 登记清单后立即按端点现状计算可用列/待核列等检查内容
            self.refresh_impact_list(str(entry.get("关联端点", "")))
        return entry, missing

    # ---------- 基础读取 ----------
    def _diffs(self, endpoint: str) -> list[dict]:
        """端点的结构化字段差异（schema_diff 中带差异类型的记录）。"""
        return [
            row for row in store.rows("schema_diff")
            if row.get("关联端点") == endpoint and row.get("差异类型")
        ]

    def _pending_diffs(self, endpoint: str) -> list[dict]:
        return [d for d in self._diffs(endpoint) if d.get("status") not in ("已确认", "已忽略")]

    @staticmethod
    def _missing_sources(diff: dict) -> list[str]:
        received = set(diff.get("已收样本来源") or [])
        return [s for s in (diff.get("需要样本来源") or []) if s not in received]

    @classmethod
    def _verifiable(cls, diff: dict) -> bool:
        # 独立新增列无需样本；改名列必须拿到全部来源的可验证样本
        if diff.get("差异类型") == "add":
            return True
        return not cls._missing_sources(diff)

    def _versions(self, endpoint: str) -> list[dict]:
        rows = [
            row for row in store.rows("data_version")
            if row.get("关联端点") == endpoint and row.get("版本序号") is not None
        ]
        return sorted(rows, key=lambda row: int(row["版本序号"]))

    def _current_version(self, endpoint: str) -> dict | None:
        versions = [v for v in self._versions(endpoint) if v.get("status") == "可用"]
        return versions[-1] if versions else None

    def _subscribers(self, endpoint: str) -> list[dict]:
        return [s for s in store.rows("subscriber") if s.get("数据主题") == endpoint]

    def _tasks(self, endpoint: str) -> list[dict]:
        return [t for t in store.rows("field_delivery") if t.get("关联端点") == endpoint]

    # ---------- 字段投产影响视图 ----------
    def build_impact_view(self, endpoint: str) -> dict[str, Any]:
        """把端点字段差异与待交付版本关联，列出可用列、待核列、涉及订阅和缺少的样本来源。"""
        current = self._current_version(endpoint)
        pendings = self._pending_diffs(endpoint)
        scope = current.get("字段范围", []) if current else []
        pending_columns = []
        missing: dict[str, list[str]] = {}
        for diff in pendings:
            lack = self._missing_sources(diff)
            if lack:
                missing[diff["记录编号"]] = lack
            pending_columns.append({
                "列名": diff.get("新列名") or diff.get("变更字段"),
                "差异记录": diff.get("记录编号"),
                "差异类型": diff.get("差异类型"),
                "说明": diff.get("新定义"),
                "缺少样本来源": lack,
                "可提交确认": self._verifiable(diff),
            })
        subscriptions = []
        for sub in self._subscribers(endpoint):
            requested = sub.get("申请字段") or []
            related = [d for d in pendings if d.get("字段标识") in requested]
            tasks = [t for t in self._tasks(endpoint) if t.get("订阅编号") == sub.get("订阅编号")]
            subscriptions.append({
                "订阅编号": sub.get("订阅编号"),
                "订阅方": sub.get("订阅方"),
                "申请字段": list(requested),
                "涉及差异": [d.get("记录编号") for d in related],
                "待核列申请": [d.get("新列名") for d in related],
                "投递任务": [
                    {"任务编号": t.get("任务编号"), "状态": t.get("status"),
                     "计划版本": t.get("计划版本"), "快照版本": t.get("快照版本")}
                    for t in tasks
                ],
            })
        return {
            "关联端点": endpoint,
            "当前版本": None if current is None else {
                "版本编号": current.get("版本编号"),
                "版本序号": current.get("版本序号"),
                "字段范围": current.get("字段范围"),
            },
            "待交付版本": (int(current["版本序号"]) + 1) if (current and pendings) else None,
            "可用列": [f["name"] for f in scope],
            "待核列": pending_columns,
            "缺少样本来源": missing,
            "涉及订阅": subscriptions,
            "关联差异": [d.get("记录编号") for d in pendings],
        }

    def refresh_impact_list(self, endpoint: str) -> None:
        """把影响视图物化到清单行，供发布前检查。"""
        if not endpoint:
            return
        view = self.build_impact_view(endpoint)
        rows = [r for r in store.rows(self.MODULE) if r.get("关联端点") == endpoint]
        if not rows:
            rows = [{
                "id": _next_id(store.rows(self.MODULE)),
                "清单编号": f"IMPA-{_endpoint_tag(endpoint)}-0001",
                "关联端点": endpoint,
                "status": "待检查", "pending": True, "abnormal": False,
            }]
            store.rows(self.MODULE).extend(rows)
        current = view["当前版本"]
        missing = view["缺少样本来源"]
        for row in rows:
            row["当前版本"] = f"v{current['版本序号']}" if current else "-"
            row["待交付版本"] = f"v{view['待交付版本']}(待确认)" if view["待交付版本"] else "-"
            row["可用列"] = ",".join(view["可用列"])
            row["待核列"] = "，".join(c["列名"] for c in view["待核列"]) or "无"
            row["涉及订阅"] = "、".join(s["订阅方"] for s in view["涉及订阅"]) or "无"
            row["缺少样本来源"] = (
                "；".join(f"{k}缺{','.join(v)}" for k, v in missing.items()) or "无"
            )
            row["关联差异"] = ",".join(view["关联差异"]) or "无"
            if missing:
                row["检查结论"] = "缺少可验证样本"
            elif view["关联差异"]:
                row["检查结论"] = "样本齐全，待提交确认"
            else:
                row["检查结论"] = "差异已确认，可按当前版本投产"
            row["生成时间"] = TODAY
            row["abnormal"] = bool(missing)

    # ---------- 样本核对 ----------
    def provide_sample(self, diff_key: str, source: str) -> tuple[dict | None, str]:
        """登记差异所需来源的可验证样本。"""
        diff = next((d for d in store.rows("schema_diff") if d.get("记录编号") == diff_key), None)
        if diff is None:
            return None, f"差异记录{diff_key}不存在"
        if source not in (diff.get("需要样本来源") or []):
            return diff, f"样本来源{source}不在{diff_key}的核对清单内"
        received = diff.setdefault("已收样本来源", [])
        if source not in received:
            received.append(source)
        self.refresh_impact_list(str(diff.get("关联端点", "")))
        return diff, f"差异{diff_key}已登记样本来源{source}"

    def provide_all_missing_samples(self, endpoint: str) -> list[str]:
        arrived = []
        for diff in self._pending_diffs(endpoint):
            for source in self._missing_sources(diff):
                diff.setdefault("已收样本来源", []).append(source)
                arrived.append(f"{diff['记录编号']}:{source}")
        self.refresh_impact_list(endpoint)
        return arrived

    # ---------- 确认工作流（幂等） ----------
    def submit_confirmation(self, key: str, endpoint: str) -> tuple[dict, str]:
        """提交确认：样本不齐则不完成；重复提交同一确认只生效一次。"""
        rows = store.rows("field_confirmation")
        record = next((r for r in rows if r.get("确认键") == key), None)
        if record is not None and record.get("status") == CONF_COMPLETE:
            record["提交次数"] = int(record.get("提交次数", 1)) + 1
            record["最近提交"] = TODAY
            return record, f"确认{key}已生效过，重复提交不再产生新版本（仍为v{record['产生版本']}）"
        pendings = self._pending_diffs(endpoint)
        if record is None:
            record = {
                "id": _next_id(rows), "确认键": key, "关联端点": endpoint,
                "差异记录": [], "status": CONF_INCOMPLETE, "产生版本": None,
                "缺少样本": {}, "提交次数": 0, "最近提交": TODAY,
            }
            rows.append(record)
        record["差异记录"] = [d.get("记录编号") for d in pendings]
        record["提交次数"] = int(record.get("提交次数", 0)) + 1
        record["最近提交"] = TODAY
        missing = {d["记录编号"]: self._missing_sources(d) for d in pendings
                   if not self._verifiable(d)}
        if missing:
            record["status"] = CONF_INCOMPLETE
            record["缺少样本"] = missing
            self.refresh_impact_list(endpoint)
            self._mark_list_status(endpoint, complete=False)
            lack = "；".join(f"{d}缺{','.join(src)}" for d, src in missing.items())
            return record, f"确认{key}未完成：{lack}"
        version = self._create_confirmed_version(endpoint, pendings, key)
        record["status"] = CONF_COMPLETE
        record["缺少样本"] = {}
        record["产生版本"] = version["版本序号"]
        synced = self._sync_backlog(endpoint, int(version["版本序号"]))
        self.refresh_impact_list(endpoint)
        self._mark_list_status(endpoint, complete=True)
        return record, f"确认{key}已完成，生效版本v{version['版本序号']}，同步投递待办{synced}个"

    def _mark_list_status(self, endpoint: str, *, complete: bool) -> None:
        """确认结果同步到清单行状态，模块动作与工作流接口走同一条路径。"""
        for row in store.rows(self.MODULE):
            if row.get("关联端点") == endpoint:
                row["status"] = "可投产" if complete else "样本待核"
                row["pending"] = not complete
                row["abnormal"] = not complete

    def _create_confirmed_version(self, endpoint: str, diffs: list[dict], key: str) -> dict:
        """数据版本保存已确认的字段范围和关联差异。"""
        current = self._current_version(endpoint)
        scope = [dict(f) for f in (current.get("字段范围") if current else [])]
        order = [f["field_id"] for f in scope]
        by_id = {f["field_id"]: f for f in scope}
        for diff in diffs:
            fid = diff.get("字段标识")
            if diff.get("差异类型") == "rename":
                by_id[fid]["name"] = diff.get("新列名")
            else:  # add：独立新增列追加到字段范围末尾
                by_id[fid] = {"field_id": fid, "name": diff.get("新列名"), "role": "普通列"}
                order.append(fid)
        new_scope = [by_id[fid] for fid in order]
        new_no = int(current["版本序号"]) + 1 if current else 1
        rows = store.rows("data_version")
        version = {
            "id": _next_id(rows),
            "版本编号": f"DATA-{_endpoint_tag(endpoint)}-{new_no:04d}",
            "数据来源": endpoint,
            "行数统计": 0,
            "版本标签": f"field_v{new_no}",
            "Schema摘要": "、".join(f["name"] for f in new_scope),
            "创建原因": f"字段确认{key}生效",
            "依赖版本": current.get("版本编号") if current else "",
            "创建时间": TODAY,
            "status": "可用", "pending": False, "abnormal": False,
            "关联端点": endpoint,
            "版本序号": new_no,
            "字段范围": new_scope,
            "关联差异": [d.get("记录编号") for d in diffs],
        }
        rows.append(version)
        for diff in diffs:
            diff["status"] = "已确认"
            diff["pending"] = False
            diff["确认版本"] = new_no
        return version

    def _sync_backlog(self, endpoint: str, version_no: int) -> int:
        """确认结果同步到投递待办：待办任务计划版本前移；已领用任务保持原快照。"""
        synced = 0
        for task in self._tasks(endpoint):
            if task.get("status") == TASK_PENDING:
                task["计划版本"] = version_no
                synced += 1
        return synced

    # ---------- 订阅申请 ----------
    def request_column(self, sub_key: str, field_id: str) -> tuple[dict | None, str]:
        sub = next((s for s in store.rows("subscriber") if s.get("订阅编号") == sub_key), None)
        if sub is None:
            return None, f"订阅{sub_key}不存在"
        requested = sub.setdefault("申请字段", [])
        if field_id not in requested:
            requested.append(field_id)
        self.refresh_impact_list(str(sub.get("数据主题", "")))
        return sub, f"订阅{sub_key}已申请字段{field_id}"

    # ---------- 投递待办：领用与拉取 ----------
    def open_next_batch(self, sub_key: str, batch: str) -> tuple[dict | None, str]:
        sub = next((s for s in store.rows("subscriber") if s.get("订阅编号") == sub_key), None)
        if sub is None:
            return None, f"订阅{sub_key}不存在"
        endpoint = str(sub.get("数据主题", ""))
        current = self._current_version(endpoint)
        rows = store.rows("field_delivery")
        task = {
            "id": _next_id(rows),
            "任务编号": f"DELV-{_endpoint_tag(endpoint)}-{_next_id(rows):04d}",
            "关联端点": endpoint,
            "订阅编号": sub_key,
            "批次": batch,
            "计划版本": int(current["版本序号"]) if current else 1,
            "快照版本": None,
            "交付记录": "",
            "status": TASK_PENDING, "pending": True, "abnormal": False,
        }
        rows.append(task)
        self.refresh_impact_list(endpoint)
        return task, f"已为订阅{sub_key}开立批次{batch}的投递待办"

    def _find_task(self, task_key: str) -> dict | None:
        return next((t for t in store.rows("field_delivery") if t.get("任务编号") == task_key), None)

    def claim_task(self, task_key: str) -> tuple[dict | None, str]:
        """领用任务：按当前计划版本固定快照，之后按原快照读取。"""
        task = self._find_task(task_key)
        if task is None:
            return None, f"投递任务{task_key}不存在"
        if task.get("status") == TASK_CLAIMED:
            return task, f"任务{task_key}已领用，快照版本v{task.get('快照版本')}"
        if task.get("status") != TASK_PENDING:
            return None, f"任务{task_key}已交付，不能重复领用"
        task["status"] = TASK_CLAIMED
        task["快照版本"] = task.get("计划版本")
        self.refresh_impact_list(str(task.get("关联端点", "")))
        return task, f"任务{task_key}已领用，快照版本v{task['快照版本']}"

    def pull_task(self, task_key: str) -> tuple[dict | None, str]:
        """拉取并交付：按任务快照版本组装文件，每份文件只用一个完整字段版本。"""
        task = self._find_task(task_key)
        if task is None:
            return None, f"投递任务{task_key}不存在"
        if task.get("status") == TASK_DELIVERED:
            record = self._find_record(str(task.get("交付记录", "")))
            return record, f"任务{task_key}已交付，返回原交付记录"
        if task.get("status") == TASK_PENDING:
            self.claim_task(task_key)
        endpoint = str(task.get("关联端点", ""))
        version = next(
            (v for v in self._versions(endpoint) if int(v["版本序号"]) == int(task["快照版本"])),
            None,
        )
        if version is None:
            return None, f"任务{task_key}的快照版本v{task['快照版本']}不存在"
        sub = next((s for s in store.rows("subscriber") if s.get("订阅编号") == task.get("订阅编号")), None)
        requested = set((sub or {}).get("申请字段") or [])
        scope = version.get("字段范围") or []
        # 每份文件只用一个完整字段版本：列全部取自该版本的字段范围
        columns = [f["name"] for f in scope if f["field_id"] in requested]
        records = store.rows("field_delivery_record")
        record = {
            "id": _next_id(records),
            "记录编号": f"RECV-{_endpoint_tag(endpoint)}-{_next_id(records):04d}",
            "任务编号": task["任务编号"],
            "订阅编号": task.get("订阅编号"),
            "订阅方": (sub or {}).get("订阅方", ""),
            "关联端点": endpoint,
            "版本编号": version["版本编号"],
            "版本序号": version["版本序号"],
            "文件列": columns,
            "字段定义": [dict(f) for f in scope],
            "原始差异": [self._diff_snapshot(k) for k in version.get("关联差异", [])],
            "交付时间": TODAY,
            "status": "已交付", "pending": False, "abnormal": False,
        }
        records.append(record)
        task["status"] = TASK_DELIVERED
        task["pending"] = False
        task["交付记录"] = record["记录编号"]
        self.refresh_impact_list(endpoint)
        return record, f"任务{task_key}已按{version['版本编号']}组装文件并交付"

    @staticmethod
    def _diff_snapshot(diff_key: str) -> dict:
        diff = next((d for d in store.rows("schema_diff") if d.get("记录编号") == diff_key), None)
        if diff is None:
            return {"记录编号": diff_key}
        return {
            "记录编号": diff.get("记录编号"),
            "差异类型": diff.get("差异类型"),
            "变更字段": diff.get("变更字段"),
            "字段标识": diff.get("字段标识"),
            "旧定义": diff.get("旧定义"),
            "新定义": diff.get("新定义"),
            "新列名": diff.get("新列名"),
        }

    @staticmethod
    def _find_record(record_key: str) -> dict | None:
        return next(
            (r for r in store.rows("field_delivery_record") if r.get("记录编号") == record_key),
            None,
        )

    def delivery_record_trace(self, record_key: str) -> dict | None:
        """交付记录回查：文件所用的字段定义和原始差异。"""
        record = self._find_record(record_key)
        if record is None:
            return None
        return {
            "记录编号": record["记录编号"],
            "任务编号": record["任务编号"],
            "订阅方": record["订阅方"],
            "关联端点": record["关联端点"],
            "版本编号": record["版本编号"],
            "版本序号": record["版本序号"],
            "文件列": record["文件列"],
            "字段定义": record["字段定义"],
            "原始差异": record["原始差异"],
            "交付时间": record["交付时间"],
        }
