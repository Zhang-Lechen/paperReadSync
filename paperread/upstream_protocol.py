# Extracted without changing protocol methods from GaviZhao/WeReadGears.
# Commit a3ab77bd767c73e6cca367e6ea95b4d9e98d465c; MIT, Copyright (c) 2024 GaviZhao.
# See third_party/WeReadGears-LICENSE.txt. No runtime/scheduler/browser imports.
import hashlib
import random
import time
import urllib.parse

KEY = '3c5c8717f3daf09iop3423zafeqoi'

class UpstreamPayload:

    def _encode_data(self, data: dict) -> str:
        pairs = [f'{k}={urllib.parse.quote(str(data[k]), safe='')}' for k in sorted(data.keys())]
        return '&'.join(pairs)

    def _calculate_hash(self, input_string: str) -> str:
        _7032f5 = 352654597
        _cc1055 = _7032f5
        length = len(input_string)
        _19094e = length - 1
        while _19094e > 0:
            char_code = ord(input_string[_19094e])
            shift_amount = (length - _19094e) % 30
            _7032f5 = 2147483647 & (_7032f5 ^ char_code << shift_amount)
            prev_char_code = ord(input_string[_19094e - 1])
            prev_shift_amount = _19094e % 30
            _cc1055 = 2147483647 & (_cc1055 ^ prev_char_code << prev_shift_amount)
            _19094e -= 2
        return hex(_7032f5 + _cc1055)[2:].lower()

    def _apply_user_identity(self):
        """重新注入 ps/pc(以及有的话 appId)。

        关键设计:
          - ps/pc 是会话级身份,captured 时绑定,后续每轮都从 captured 注入确保身份一致
          - appId 只在 captured 有时才设置,不强制(很多 curl 抓包没有 appId 字段也能跑)
          - 不触碰 b/c:ps/pc 必须跟 b/c 同源(同一次抓包),否则腾讯服务端判定异常

        对齐 upstream funnyzak/weread-bot:在 startup 时校验 ps/pc/appId 不是
        placeholder("app_id" 或空字符串),避免带着垃圾值跑 20 次空响应

        2026-06-28: 优先用 _last_refreshed_payload(每次切书从浏览器实时抓的,
        比 captured_payload 新鲜,sm/co/pr/ps/pc/appId 同源同截获),
        fallback 到 captured_payload。
        """
        cp = None
        if getattr(self, '_last_refreshed_payload', None):
            cp = self._last_refreshed_payload
        if not cp:
            ui = self.credentials.user_info or {}
            cp = ui.get('captured_payload', {}) or {}
        if cp.get('ps'):
            self.data['ps'] = cp['ps']
        if cp.get('pc'):
            self.data['pc'] = cp['pc']
        if cp.get('appId'):
            self.data['appId'] = cp['appId']
        if not self.data.get('ps'):
            self.data['ps'] = self.credentials.user_id or self.credentials.wr_vid or ''
        if not self.data.get('pc'):
            self.data['pc'] = self.credentials.user_id or self.credentials.wr_vid or ''

    def _prepare_payload(self, last_time: int):
        """准备单次阅读请求的payload(对齐 weread-bot 协议)

        关键修复:
          - 每轮重新计算 ts/rn/sg + 校验 _apply_user_identity
          - 注入 captured 里的 r/st/sc(浏览器真实数据)
          - ci 在主循环每轮被 _advance_chapter_index 推进过,这里直接用 self.last_chapter_index
        """
        self.data.pop('s', None)
        if self.last_book_id:
            self.data['b'] = self.last_book_id
        if self.last_chapter_id:
            self.data['c'] = self.last_chapter_id
        if self.last_chapter_index is not None:
            self.data['ci'] = self.last_chapter_index
        elif 'ci' not in self.data or self.data.get('ci') is None:
            self.data['ci'] = 0
        if self.last_book_id:
            cached = self._book_chapter_cache.get(self.last_book_id) or {}
            hb_ctx = cached.get('heartbeat_context') or {}
            if hb_ctx:
                sm_new = hb_ctx.get('sm', '')
                if sm_new and sm_new != self.data.get('sm'):
                    self.data['sm'] = sm_new
                co_new = hb_ctx.get('co', '')
                if co_new != '' and str(co_new) != str(self.data.get('co', '')):
                    self.data['co'] = co_new
                pr_new = hb_ctx.get('pr', '')
                if pr_new != '' and str(pr_new) != str(self.data.get('pr', '')):
                    self.data['pr'] = pr_new
        self._apply_user_identity()
        current_time = int(time.time())
        self.data['ct'] = current_time
        self.data['rt'] = current_time - last_time if last_time else 0
        self.data['ts'] = int(current_time * 1000) + random.randint(0, 1000)
        self.data['rn'] = random.randint(0, 1000)
        self.data['sg'] = hashlib.sha256(f'{self.data['ts']}{self.data['rn']}{KEY}'.encode()).hexdigest()
        self.data['s'] = self._calculate_hash(self._encode_data(self.data))
