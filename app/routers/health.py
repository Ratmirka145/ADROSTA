from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse


router = APIRouter(
    tags=["system"],
)


@router.get("/health")
async def health_check():
    return {
        "status": "ok",
    }


@router.get("/ready")
def readiness_check(request: Request) -> JSONResponse:
    result = request.app.state.context.order_service.readiness()
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if result.ready
            else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={
            "status": "ready" if result.ready else "not_ready",
            "checks": result.checks,
        },
    )
