"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import field_impact, resource

# field_impact 工作流路由带固定前缀 /api/field-impact，必须排在通用资源路由
# /api/{module} 之前，否则固定路径会被动态段抢先匹配。
ROUTERS = [field_impact, resource]
