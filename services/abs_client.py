import httpx

from config import settings


def _base() -> str:
    return settings.ABS_BASE_URL.rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.ABS_API_TOKEN}"}


async def get_item(item_id: str) -> dict:
    """Fetches the full item JSON from Audiobookshelf."""
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(
            f"{_base()}/api/items/{item_id}", headers=_headers()
        )
        res.raise_for_status()
        return res.json()


def cover_url(item_id: str) -> str:
    return f"{_base()}/api/items/{item_id}/cover?token={settings.ABS_API_TOKEN}"


def audio_file_url(item_id: str, ino: str) -> str:
    return (f"{_base()}/api/items/{item_id}/file/{ino}"
            f"?token={settings.ABS_API_TOKEN}")


def extract_meta(item_json: dict) -> dict:
    """Normalizes an ABS item JSON into the pieces we need."""
    media = item_json.get("media", {})
    metadata = media.get("metadata", {})
    chapters = [
        {
            "id": c.get("id", i),
            "start": float(c.get("start", 0)),
            "end": float(c.get("end", 0)),
            "title": c.get("title", f"Chapter {i + 1}"),
        }
        for i, c in enumerate(media.get("chapters", []))
    ]
    audio_files = [
        {
            "ino": f.get("ino", ""),
            "duration": float(f.get("duration", 0)),
            "filename": f.get("metadata", {}).get("filename", ""),
        }
        for f in media.get("audioFiles", [])
    ]
    return {
        "item_id": item_json.get("id", ""),
        "title": metadata.get("title", "Unknown"),
        "author": metadata.get("authorName", ""),
        "duration": float(media.get("duration", 0)),
        "language": metadata.get("language", ""),
        "cover_url": cover_url(item_json.get("id", "")),
        "chapters": chapters,
        "audio_files": audio_files,
    }
