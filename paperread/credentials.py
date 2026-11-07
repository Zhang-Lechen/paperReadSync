"""Local-only credentials. Windows blobs use current-user DPAPI and a private ACL."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from pydantic import Field, SecretStr

from paperread.errors import AppError
from paperread.models import Model


class WriterCredentials(Model):
    wr_skey: SecretStr = Field(repr=False)
    wr_vid: SecretStr = Field(repr=False)
    captured_headers: dict[str, str] = Field(repr=False)
    captured_payload: dict = Field(repr=False)
    captured_cookies: dict[str, SecretStr] = Field(default_factory=dict, repr=False)
    api_book_id: str | None = None
    web_book_id: str | None = None
    context_captured_at: str | None = None
    mapping_source: str | None = None

    @property
    def identity_hash(self):
        return hashlib.sha256(self.wr_vid.get_secret_value().encode()).hexdigest()


def app_directory() -> Path:
    if os.name == "nt":
        location = os.environ.get("LOCALAPPDATA")
        if not location:
            raise AppError("PRIVATE_STORAGE", "未找到本地受保护存储位置")
        return Path(location) / "PaperReadSync"
    return Path.home() / ".local" / "share" / "paperread-sync"


def _private_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
        return
    try:
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True, timeout=10)
        match = re.search(r"S-1-[0-9-]+", result.stdout)
        if not match:
            raise ValueError()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        "*" + match[0] + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                       capture_output=True, check=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise AppError("PRIVATE_STORAGE", "无法设置当前用户的私有凭证目录") from None


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise AppError("PLATFORM", "真实写入凭证存储目前只支持 Windows DPAPI")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination)):
        raise AppError("PRIVATE_STORAGE", "凭证加密或解密失败")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(destination.pbData)


class CredentialStore:
    def __init__(self, account_id: str, directory: Path | None = None, *, book_id: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
            raise AppError("ACCOUNT_INVALID", "账户别名无效")
        self.directory = directory or app_directory() / "secrets"
        suffix = ".book-" + hashlib.sha256(book_id.encode()).hexdigest() if book_id else ""
        self.path = self.directory / (account_id + suffix + ".dpapi")
        self.account_id = account_id
        self.book_id = book_id

    def save(self, value: WriterCredentials):
        _private_directory(self.directory)
        payload = value.model_dump(mode="json")
        payload["wr_skey"] = value.wr_skey.get_secret_value()
        payload["wr_vid"] = value.wr_vid.get_secret_value()
        payload["captured_cookies"] = {key:item.get_secret_value() for key,item in value.captured_cookies.items()}
        encrypted = _dpapi(json.dumps(payload, ensure_ascii=True).encode(), decrypt=False)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_bytes(encrypted)
            temporary.replace(self.path)
        except OSError:
            raise AppError("PRIVATE_STORAGE", "无法保存加密凭证") from None

    def load(self) -> WriterCredentials:
        try:
            if not self.path.exists() and self.book_id:
                legacy = CredentialStore(self.account_id, self.directory).load()
                # Legacy profiles are valid only when their actual payload ID is
                # already the canonical ID. Never infer a missing route mapping.
                if legacy.captured_payload.get("b") != self.book_id:
                    raise AppError("CONTEXT_REQUIRED", "请为这本书点击准备同步上下文")
                return legacy
            payload = _dpapi(self.path.read_bytes(), decrypt=True)
            return WriterCredentials.model_validate_json(payload)
        except AppError:
            raise
        except Exception:
            raise AppError("WRITER_LOGIN_REQUIRED", "未找到有效的本地加密写入凭证") from None


def import_upstream_file(path: Path) -> WriterCredentials:
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        info = value.get("user_info", {})
        result = WriterCredentials(
            wr_skey=value["wr_skey"], wr_vid=value["wr_vid"],
            captured_headers=info["captured_headers"], captured_payload=info["captured_payload"],
        )
        if not result.wr_skey.get_secret_value() or not result.wr_vid.get_secret_value():
            raise ValueError()
        return result
    except Exception:
        raise AppError("CREDENTIAL_FORMAT", "凭证文件无效；请仅在本地准备已授权的 WeReadGears JSON") from None


class PrivateJsonStore:
    """Shared current-user DPAPI storage for browser sessions and read API keys."""

    def __init__(self, account_id: str, kind: str, directory: Path | None = None):
        if kind not in ("api", "browser") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id):
            raise AppError("PRIVATE_STORAGE", "本地凭证名称无效")
        self.directory = directory or app_directory() / "secrets"
        self.path = self.directory / f"{account_id}.{kind}.dpapi"

    def exists(self):
        return self.path.is_file()

    def save(self, value: dict):
        _private_directory(self.directory)
        raw = json.dumps(value, ensure_ascii=True).encode()
        if len(raw) > 2_000_000:
            raise AppError("PRIVATE_STORAGE", "授权数据超过本地存储上限")
        encrypted = _dpapi(raw, decrypt=False)
        temporary = self.path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except OSError:
            raise AppError("PRIVATE_STORAGE", "无法保存加密授权数据") from None

    def load(self):
        try:
            if self.path.stat().st_size > 2_100_000:
                raise ValueError()
            value = json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except Exception:
            raise AppError("LOCAL_LOGIN_REQUIRED", "请先在本机连接授权") from None


class ApiKeyStore(PrivateJsonStore):
    def __init__(self, account_id="default", directory=None):
        super().__init__(account_id, "api", directory)

    def save_key(self, value: str):
        if not re.fullmatch(r"wrk-[A-Za-z0-9_-]{12,200}", value.strip()):
            raise AppError("KEY_FORMAT", "官方 Key 格式无效")
        self.save({"value": value.strip()})

    def load_key(self):
        value = self.load().get("value")
        if not isinstance(value, str) or not value.startswith("wrk-"):
            raise AppError("READ_LOGIN_REQUIRED", "请重新连接官方只读 Key")
        return value


def official_key(account_id="default") -> str:
    value = os.environ.get("WEREAD_API_KEY", "").strip()
    if not value:
        store = ApiKeyStore(account_id)
        if store.exists():
            return store.load_key()
        raise AppError("READ_LOGIN_REQUIRED", "请在本机界面连接官方只读 Key")
    return value


def read_key_status(account_id="default") -> dict:
    """Use the same resolver as official requests, without returning any key value."""
    source = "environment" if os.environ.get("WEREAD_API_KEY", "").strip() else "encrypted"
    try:
        official_key(account_id)
    except AppError as exc:
        return {"available": False, "source": None, "error_code": exc.code}
    return {"available": True, "source": source, "error_code": None}
