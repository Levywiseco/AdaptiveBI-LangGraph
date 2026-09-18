from fastapi import APIRouter
from apps.graph_gateway.api import router as graph_gateway_router

from apps.chat.api import chat
from apps.dashboard.api import dashboard_api
from apps.data_training.api import data_training
from apps.datasource.api import datasource, table_relation, recommended_problem
from apps.mcp import mcp
from apps.metrics.api import metric
from apps.memory.api import memory
from apps.feedback.api import feedback
from apps.learning.api import learning
from apps.system.api import login, user, aimodel, workspace, assistant, parameter, apikey, variable_api, audit
from apps.terminology.api import terminology
from apps.settings.api import base
#from audit.api import audit_api


api_router = APIRouter()
api_router.include_router(graph_gateway_router)
api_router.include_router(login.router)
api_router.include_router(user.router)
api_router.include_router(workspace.router)
api_router.include_router(assistant.router)
api_router.include_router(aimodel.router)
api_router.include_router(base.router)
api_router.include_router(terminology.router)
api_router.include_router(data_training.router)
api_router.include_router(metric.router)
api_router.include_router(memory.router)
api_router.include_router(feedback.router)
api_router.include_router(learning.router)
api_router.include_router(datasource.router)
api_router.include_router(chat.router)
api_router.include_router(dashboard_api.router)
api_router.include_router(mcp.router)
api_router.include_router(table_relation.router)
api_router.include_router(parameter.router)
api_router.include_router(apikey.router)

api_router.include_router(recommended_problem.router)

api_router.include_router(variable_api.router)
api_router.include_router(audit.router)
