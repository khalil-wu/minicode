"""Serve bundled web assets with the same media types as workspace previews."""
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from backend.media_types import media_type_for_path


class WebAssetFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if isinstance(response, FileResponse):
            media_type = media_type_for_path(full_path)
            response.media_type = media_type
            response.headers["content-type"] = (
                f"{media_type}; charset={response.charset}" if media_type.startswith("text/") else media_type
            )
        return response
