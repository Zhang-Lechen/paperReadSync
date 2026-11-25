"""Native login Keychain storage; secrets never enter subprocess arguments/files.

The generic-password API is available on both Intel and Apple Silicon macOS.
Only errSecItemNotFound is treated as absence; locked/denied storage fails closed.
"""

import ctypes as C
from functools import lru_cache
import hashlib
from pathlib import Path

from paperread.errors import AppError

SERVICE = b"PaperReadSync"
NOT_FOUND = -25300


@lru_cache(maxsize=1)
def _libraries():
    security = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer, uint = C.c_void_p, C.c_uint32
    signatures = {
        "SecKeychainFindGenericPassword": [pointer, uint, pointer, uint, pointer,
                                            C.POINTER(uint), C.POINTER(pointer), C.POINTER(pointer)],
        "SecKeychainAddGenericPassword": [pointer, uint, pointer, uint, pointer,
                                           uint, pointer, C.POINTER(pointer)],
        "SecKeychainItemModifyAttributesAndData": [pointer, pointer, uint, pointer],
        "SecKeychainItemFreeContent": [pointer, pointer],
    }
    for name, arguments in signatures.items():
        function = getattr(security, name)
        function.argtypes = arguments
        function.restype = C.c_int32
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None
    return security, core


def _check(status):
    if status:
        raise AppError("PRIVATE_STORAGE", "无法访问 macOS 钥匙串，请解锁登录钥匙串并允许本机应用访问")


def access(path: Path, *, value: bytes | None = None, exists=False):
    # Include the directory to isolate explicit profiles and test stores.
    account = hashlib.sha256(str(path.resolve()).encode()).hexdigest().encode("ascii")
    try:
        security, core = _libraries()
    except OSError:
        raise AppError("PRIVATE_STORAGE", "无法加载 macOS 钥匙串服务") from None
    length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None, len(SERVICE), SERVICE, len(account), account,
        C.byref(length) if value is None and not exists else None,
        C.byref(data) if value is None and not exists else None, C.byref(item))
    try:
        if status == NOT_FOUND:
            if exists:
                return False
            if value is None:
                raise FileNotFoundError("No local Keychain credential")
            _check(security.SecKeychainAddGenericPassword(
                None, len(SERVICE), SERVICE, len(account), account, len(value), value, None))
            return
        _check(status)
        if exists:
            return True
        if value is not None:
            _check(security.SecKeychainItemModifyAttributesAndData(item, None, len(value), value))
            return
        if length.value > 2_100_000:
            raise AppError("PRIVATE_STORAGE", "授权数据超过本地存储上限")
        return C.string_at(data, length.value)
    finally:
        if data.value:
            security.SecKeychainItemFreeContent(None, data)
        if item.value:
            core.CFRelease(item)
