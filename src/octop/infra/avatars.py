"""Avatar validation shared by control-plane records and Agent workspaces."""

from __future__ import annotations

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils.image import sniff_image_media_type

MAX_AVATAR_BYTES = 5 * 1024 * 1024


def sniff_avatar_media_type(data: bytes) -> str:
    if not data:
        raise OctopError(ErrorCode.AVATAR_INVALID, "avatar file is empty")
    if len(data) > MAX_AVATAR_BYTES:
        raise OctopError(ErrorCode.AVATAR_TOO_LARGE, "avatar exceeds 5 MiB")
    media_type = sniff_image_media_type(data)
    if media_type is None:
        raise OctopError(ErrorCode.AVATAR_INVALID, "avatar content is not a valid image")
    return media_type
