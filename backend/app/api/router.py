from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.auth.router import router as auth_router
from app.api.orgs.router import router as orgs_router
from app.api.projects.router import router as projects_router
from app.api.test_cases.router import router as test_cases_router
from app.api.test_runs.router import router as test_runs_router
from app.api.intelligence.router import router as intelligence_router
from app.api.release_validations.router import router as release_validations_router
from app.api.capture.router import router as capture_router
from app.api.dashboard.router import router as dashboard_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router)
api_router.include_router(orgs_router)
api_router.include_router(projects_router)
api_router.include_router(test_cases_router)
api_router.include_router(test_runs_router)
api_router.include_router(intelligence_router)
api_router.include_router(release_validations_router)
api_router.include_router(capture_router)
api_router.include_router(dashboard_router)
