import httpx
from fastapi import APIRouter, HTTPException

import transcription_database as db
from transcription_models import BookMeta
from services import abs_client

router = APIRouter()


@router.get("/metadata/{abs_item_id}", response_model=BookMeta)
async def metadata(abs_item_id: str) -> BookMeta:
    """Book metadata, proxied from ABS and cached in the local DB."""
    item_json = await db.get_book(abs_item_id)
    if item_json is None:
        try:
            item_json = await abs_client.get_item(abs_item_id)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 401:
                raise HTTPException(
                    502, "ABS rejected the token — check ABS_API_TOKEN in .env"
                ) from e
            raise HTTPException(502, f"ABS returned HTTP {code}") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Cannot reach ABS: {e}") from e
        meta = abs_client.extract_meta(item_json)
        await db.upsert_book(abs_item_id, meta["title"], meta["author"],
                             len(meta["chapters"]), item_json)

    meta = abs_client.extract_meta(item_json)
    return BookMeta(
        item_id=meta["item_id"],
        title=meta["title"],
        author=meta["author"],
        cover_url=meta["cover_url"],
        duration=meta["duration"],
        chapters=[
            {
                "index": i,
                "title": c["title"],
                "start": c["start"],
                "end": c["end"],
                "duration": c["end"] - c["start"],
            }
            for i, c in enumerate(meta["chapters"])
        ],
    )
