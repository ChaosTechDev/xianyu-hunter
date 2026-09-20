from fastapi import APIRouter, HTTPException, Query

from src.services.official_search_service import search_official_items


router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/items")
async def search_items(
    keyword: str = Query(min_length=1, max_length=100),
    account_path: str = Query(min_length=1),
    page: int = Query(default=1, ge=1, le=50),
    page_size: int = Query(default=30, ge=1, le=50),
):
    try:
        return await search_official_items(keyword.strip(), account_path, page, page_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"官方搜索失败: {exc}") from exc
