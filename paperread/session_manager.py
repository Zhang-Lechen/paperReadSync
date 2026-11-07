"""Maintain same-account DPAPI authorization. Callers hold the global writer lock.

No timers, reading requests, ledger changes, or retry of an uncertain reading POST.
"""

from copy import deepcopy

from pydantic import SecretStr

from paperread.credentials import CredentialStore, PrivateJsonStore, WriterCredentials
from paperread.errors import AppError
from paperread.web_session import renew_session, session_cookies, validate_session


def browser_cookies(state):
    result = {}
    for cookie in state.get("cookies", []):
        if (cookie.get("domain", "").lstrip(".") != "weread.qq.com" or
                cookie.get("path") != "/"):
            continue
        name, value = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name in result and result[name] != value:
            raise AppError("SESSION_ACCOUNT_MISMATCH", "本机授权含冲突 Cookie，请重新扫码")
        result[name] = value
    return result


def profile_from_browser(state):
    cookies = browser_cookies(state)
    if not cookies.get("wr_skey") or not cookies.get("wr_vid"):
        raise AppError("LOCAL_LOGIN_REQUIRED", "请先在本机扫码登录微信读书")
    return WriterCredentials(wr_skey=cookies["wr_skey"], wr_vid=cookies["wr_vid"],
        captured_cookies=cookies, captured_payload={}, captured_headers={})


class SessionManager:
    def __init__(self, account_id, *, directory=None, book_id=None):
        self.browser = PrivateJsonStore(account_id, "browser", directory)
        self.book = CredentialStore(account_id, directory, book_id=book_id) if book_id else None

    def ensure(self, profile, *, client=None):
        state = self.browser.load() if self.browser.exists() else None
        candidate = profile
        # Check shared identity even if the older per-book session still works.
        if state is not None:
            shared = profile_from_browser(state)
            if shared.identity_hash != profile.identity_hash:
                raise AppError("SESSION_ACCOUNT_MISMATCH", "本书授权与当前网页登录账号不同，请重新准备本书上下文")
        try:
            return profile, validate_session(profile, client=client), "VALID"
        except AppError as exc:
            if exc.code != "SESSION_EXPIRED":
                raise
        # A different book may have already renewed the shared authorization.
        if state is not None and session_cookies(shared) != session_cookies(profile):
            cookies = session_cookies(profile)
            cookies.update(session_cookies(shared))
            candidate = profile.model_copy(update={"wr_skey":shared.wr_skey,
                "captured_cookies":{name:SecretStr(value) for name,value in cookies.items()}})
            try:
                token = validate_session(candidate, client=client)
            except AppError as exc:
                if exc.code != "SESSION_EXPIRED":
                    raise
            else:
                if self.book:
                    self.book.save(candidate)
                return candidate, token, "REUSED"
        updated, token, changes = renew_session(candidate, client=client)
        if state is not None:
            refreshed = deepcopy(state)
            # Replace auth cookie metadata too: old expiry can make Playwright
            # silently discard a newly renewed value on the next preparation.
            refreshed["cookies"] = [item for item in refreshed.get("cookies", [])
                if not (item.get("domain", "").lstrip(".") == "weread.qq.com" and
                        item.get("name") in changes)]
            for name, item in changes.items():
                refreshed["cookies"].append({"name":name, "value":item["value"],
                    "domain":".weread.qq.com", "path":"/", "expires":item["expires"],
                    "httpOnly":True, "secure":True, "sameSite":"Lax"})
            self.browser.save(refreshed)
        if self.book:
            self.book.save(updated)
        return updated, token, "RENEWED"

    def ensure_browser(self, *, expected_identity=None, client=None):
        profile = profile_from_browser(self.browser.load())
        if expected_identity and profile.identity_hash != expected_identity:
            raise AppError("RECOVERY_ACCOUNT_MISMATCH", "保存的网页登录与未决写入账号不同，未更新授权")
        return self.ensure(profile, client=client)[2]
