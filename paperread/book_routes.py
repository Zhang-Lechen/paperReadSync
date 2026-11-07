"""Canonical API identifiers remain distinct from web reader route identifiers."""

import re
from urllib.parse import urlparse

from paperread.errors import AppError


def reader_route(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "weread.qq.com":
        return None
    match = re.fullmatch(r"/web/reader/([A-Za-z0-9_-]{1,128})/?", parsed.path)
    return match[1] if match else None


def approved_reader_link(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise AppError("READER_LINK", "官方阅读链接无效") from None
    if (parsed.scheme != "https" or parsed.netloc != "weread.qq.com" or parsed.fragment or
        not (reader_route(url) or re.fullmatch(r"/r/[A-Za-z0-9_-]{1,256}/?", parsed.path))):
        raise AppError("READER_LINK", "官方未提供可验证的微信读书 HTTPS 阅读链接，已停止")
    return url


def validate_captured_mapping(api_book_id: str, official_link: str, final_url: str, payload: dict, referer: str):
    approved_reader_link(official_link)
    final_route = reader_route(final_url)
    expected_route = reader_route(official_link)
    if not final_route or (expected_route and expected_route != final_route):
        raise AppError("BOOK_ROUTE_MISMATCH", "阅读页面与官方所选版本的路由不一致")
    if not isinstance(payload, dict) or payload.get("b") != final_route or reader_route(referer) != final_route:
        raise AppError("BOOK_ROUTE_MISMATCH", "捕获的章节、页面路由与所选书籍不一致")
    if not api_book_id or not payload.get("c") or not payload.get("sm"):
        raise AppError("CHAPTER_CONTEXT", "缺少同源章节上下文")
    return final_route
