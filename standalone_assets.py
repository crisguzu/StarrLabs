"""Original neutral fallbacks for intentionally unbundled decorative images."""
from pathlib import PurePosixPath
from starlette.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

class StandaloneStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or PurePosixPath(path).suffix.lower() not in {".png",".webp",".jpg",".jpeg",".ico"}:
                raise
            # Missing JS/CSS/fonts remain real errors; no remote requests occur.
            name="neutral-emblem.svg"
            lower=path.lower()
            if "lab-" in lower or "background" in lower:
                name="neutral-lab.svg"
            elif any(part in lower for part in ("cream-enamel","teal-metal","crt-glass")):
                name="neutral-texture.svg"
            response=await super().get_response("starrlabs/"+name,scope)
            response.headers["X-StarrLabs-Asset"]="original-neutral-fallback"
            return response
