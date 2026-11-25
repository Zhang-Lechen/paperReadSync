"""Browser failures must identify the failing stage without leaking raw logs."""
from threading import Event

import pytest

from paperread import browser_session
from paperread.errors import AppError


@pytest.mark.parametrize('stage,label', [
    ('PREFLIGHT', '打开浏览器前校验已保存登录'),
    ('LAUNCH', '启动 chrome'), ('RESTORE', '恢复浏览器会话'),
    ('GUARD', '安装阅读请求拦截'), ('PAGE', '打开授权页面'),
])
def test_step_failure_is_actionable_and_never_discloses_exception(stage, label):
    with pytest.raises(AppError) as caught:
        with browser_session.browser_step(stage, label):
            raise RuntimeError('synthetic-private-cookie and private request URL')
    assert caught.value.code == 'BROWSER_' + stage
    assert label in caught.value.message
    assert 'synthetic-private' not in str(caught.value)


def test_expected_session_errors_are_preserved():
    error = AppError('SESSION_EXPIRED', '请重新扫码')
    with pytest.raises(AppError) as caught:
        with browser_session.browser_step('PREFLIGHT', '会话校验'):
            raise error
    assert caught.value is error


def test_unexpected_authorization_failure_retains_safe_location(monkeypatch, tmp_path):
    async def failed(*args, **kwargs):
        raise TypeError('synthetic-private-token')
    monkeypatch.setattr(browser_session, 'authorize', failed)
    with pytest.raises(AppError) as caught:
        browser_session.run_authorization('default', cancel_event=Event(), progress=lambda _: None,
                                          lock_path=tmp_path/'writer.lock')
    assert caught.value.code == 'BROWSER_FAILED'
    assert 'TypeError' in caught.value.message
    assert 'browser_session.py:' in caught.value.message
    assert 'synthetic-private' not in str(caught.value)
