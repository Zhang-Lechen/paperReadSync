import asyncio
import json
from types import SimpleNamespace

import pytest

from paperread.book_routes import approved_reader_link, validate_captured_mapping
from paperread.browser_session import CaptureGuard, browser_request_allowed
from paperread.errors import AppError
from paperread.models import Book

LINK="https://weread.qq.com/web/reader/route-A"


def test_canonical_id_and_web_route_are_explicitly_distinct():
    assert validate_captured_mapping("10001",LINK,LINK,{"b":"route-A","c":"chapter-A","sm":"同章内容"},LINK)=="route-A"


@pytest.mark.parametrize("link,final,payload,referer",[
    (LINK,LINK.replace("route-A","route-B"),{"b":"route-B","c":"c","sm":"s"},LINK),
    (LINK,LINK,{"b":"10001","c":"c","sm":"s"},LINK),
    (LINK,LINK,{"b":"route-A","c":"c","sm":"s"},LINK.replace("route-A","route-B")),
])
def test_other_book_page_or_heartbeat_cannot_be_mapped(link,final,payload,referer):
    with pytest.raises(AppError): validate_captured_mapping("10001",link,final,payload,referer)


@pytest.mark.parametrize("url",["http://weread.qq.com/web/reader/A","https://weread.qq.com.evil.test/web/reader/A",
    "https://weread.qq.com@evil.test/web/reader/A","https://weread.qq.com/web/shelf", "file:///tmp/test"])
def test_official_link_is_limited_to_real_reader_origin(url):
    with pytest.raises(AppError): approved_reader_link(url)


class Route:
    def __init__(self, *, payload=None, method="POST", url="https://weread.qq.com/web/book/read", referer=LINK):
        async def headers(): return {"referer":referer,"origin":"https://weread.qq.com","Cookie":"synthetic-private"}
        self.request=SimpleNamespace(url=url,method=method,post_data=json.dumps(payload or {"b":"route-A","c":"c","sm":"s"}),
                                     frame=SimpleNamespace(url=LINK),all_headers=headers)
        self.forwarded=self.aborted=0
    async def abort(self): self.aborted+=1
    async def continue_(self): self.forwarded+=1


def test_context_capture_aborts_valid_heartbeat_before_network():
    guard=CaptureGuard(Book(book_id="10001",title="合成书",author="作者",reader_link=LINK))
    route=Route()
    asyncio.run(guard(route))
    assert guard.captured[2]=="route-A"
    assert route.aborted==1 and route.forwarded==0 and guard.blocked==1


def test_login_and_wrong_book_also_abort_without_capturing():
    guard=CaptureGuard()
    route=Route()
    asyncio.run(guard(route))
    assert guard.captured is None and route.aborted==1 and route.forwarded==0
    guard=CaptureGuard(Book(book_id="10001",title="合成书",author="作者",reader_link=LINK))
    route=Route(payload={"b":"route-B","c":"c","sm":"s"})
    asyncio.run(guard(route))
    assert guard.captured is None and route.aborted==1 and route.forwarded==0


@pytest.mark.parametrize("url,method,allowed",[
    ("https://weread.qq.com/web/book/read","GET",False),
    ("https://weread.qq.com/web/login/renewal","POST",True),
    ("https://weread.qq.com/web/shelf/add","POST",False),
    ("https://telemetry.example/collect","POST",False),
    ("https://weread.qq.com/web/book/chapterInfos","POST",True),
])
def test_browser_only_allows_auth_and_read_only_posts(url,method,allowed):
    assert browser_request_allowed(url,method)==allowed
