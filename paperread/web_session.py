"""Official web session validation and a single normal login renewal. No reading POST."""

import httpx
from pydantic import SecretStr

from paperread.errors import AppError

CONFIG_URL = "https://weread.qq.com/web/getConfig"
RENEW_URL = "https://weread.qq.com/web/login/renewal"


def config_token(value):
    if not isinstance(value, dict):
        raise AppError("SESSION_SCHEMA", "网页登录校验未返回有效对象，未发送阅读请求")
    code = value.get("errCode", 0)
    if code in (-2010, -2012, -2013):
        raise AppError("SESSION_EXPIRED", "微信读书网页登录已失效，请在本机重新扫码；未发送阅读请求")
    if type(code) is not int or code != 0:
        raise AppError("SESSION_REJECTED", "微信读书拒绝会话校验，已停止；未自动刷新或重试")
    token = value.get("token")
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise AppError("SESSION_SCHEMA", "缺少当前网页登录会话配置，未发送阅读请求")
    return SecretStr(token)


def session_cookies(profile):
    cookies = {key:value.get_secret_value() for key,value in profile.captured_cookies.items()}
    cookies.update(wr_skey=profile.wr_skey.get_secret_value(), wr_vid=profile.wr_vid.get_secret_value())
    return cookies


def validate_session(profile, *, client=None):
    owned = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                                   transport=httpx.HTTPTransport(retries=0))
    headers = {key:value for key,value in profile.captured_headers.items()
               if key.lower() in {"user-agent", "referer", "accept", "accept-language"}}
    try:
        response = client.send(httpx.Request("GET", CONFIG_URL, headers=headers,
                                             cookies=session_cookies(profile)), follow_redirects=False)
        if response.status_code == 401:
            raise AppError("SESSION_EXPIRED", "微信读书网页登录已失效，请重新扫码；未发送阅读请求")
        if response.status_code == 403:
            raise AppError("SESSION_REJECTED", "微信读书拒绝会话校验，已停止；未尝试续期")
        if response.status_code != 200:
            raise AppError("SESSION_HTTP", "网页登录校验返回非成功状态，已停止且未重试")
        try:
            return config_token(response.json())
        except ValueError:
            raise AppError("SESSION_SCHEMA", "网页登录校验响应无效，未发送阅读请求") from None
    except httpx.HTTPError:
        raise AppError("SESSION_NETWORK", "网页登录校验网络失败，未发送阅读请求或自动重试") from None
    finally:
        if owned:
            client.close()


def renew_session(profile, *, client=None):
    """Return validated credentials and cookie metadata; never persist on failure."""
    owned = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                                   transport=httpx.HTTPTransport(retries=0))
    headers = {key:value for key,value in profile.captured_headers.items()
               if key.lower() in {"user-agent", "referer", "accept", "accept-language", "origin"}}
    headers.setdefault("origin", "https://weread.qq.com")
    headers.setdefault("referer", "https://weread.qq.com/")
    try:
        response = client.send(httpx.Request("POST", RENEW_URL, headers=headers,
            cookies=session_cookies(profile), json={"rq":"%2Fweb%2Fbook%2Fread", "ql":False}),
            follow_redirects=False)
        if response.status_code != 200:
            raise AppError("SESSION_RENEWAL_FAILED", "登录续期未获通过，请重新扫码；未发送阅读请求")
        try:
            body = response.json()
        except ValueError:
            body = None  # Some valid renewal responses only carry Set-Cookie.
        if isinstance(body, dict) and body.get("errCode", 0) != 0:
            raise AppError("SESSION_RENEWAL_FAILED", "登录续期未获通过，请重新扫码；未发送阅读请求")
        changes = {}
        for cookie in response.cookies.jar:
            if cookie.domain.lstrip(".") != "weread.qq.com" or cookie.path != "/":
                continue
            if cookie.name in changes and changes[cookie.name]["value"] != cookie.value:
                raise AppError("SESSION_RENEWAL_FAILED", "登录续期返回冲突凭证，请重新扫码")
            changes[cookie.name] = {"value":cookie.value, "expires":cookie.expires or -1}
        if not changes.get("wr_skey", {}).get("value"):
            raise AppError("SESSION_RENEWAL_FAILED", "登录续期未返回有效凭证，请重新扫码；未发送阅读请求")
        if changes.get("wr_vid", {}).get("value", profile.wr_vid.get_secret_value()) != profile.wr_vid.get_secret_value():
            raise AppError("SESSION_ACCOUNT_MISMATCH", "续期账号与原授权不一致，未保存新凭证")
        cookies = session_cookies(profile)
        cookies.update({name:item["value"] for name,item in changes.items()})
        updated = profile.model_copy(update={"wr_skey":SecretStr(cookies["wr_skey"]),
            "captured_cookies":{name:SecretStr(value) for name,value in cookies.items()}})
        token = validate_session(updated, client=client)
        return updated, token, changes
    except httpx.HTTPError:
        raise AppError("SESSION_NETWORK", "登录续期网络失败，已停止；未重试或发送阅读请求") from None
    finally:
        if owned:
            client.close()
