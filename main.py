# -*- coding: utf-8 -*-
import os
import sys
import json
import base64
import hashlib
import threading
import queue
import re
import time
from datetime import datetime
from urllib.parse import urlparse

# ---- Android SSL 证书处理（必须在 import requests 之前）----
from kivy.utils import platform
if platform == 'android':
    try:
        import certifi
        os.environ['SSL_CERT_FILE'] = certifi.where()
        os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
    except Exception:
        pass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.properties import (
    StringProperty, BooleanProperty, NumericProperty, ListProperty
)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.core.clipboard import Clipboard

# ==================== 配置 ====================
CUSTOM_SERVER_LABEL = "自定义..."
SERVER_CHOICES = [
    ("通道一（延迟5s） ★推荐", "https://privatebin.wildberries.ru/"),
    ("通道二（延迟10s）",       "https://paste.plus/"),
    ("通道三（较安全延迟60s）",  "https://privatebin.net/"),
    ("通道四（延迟10s）",       "https://anonpaste.org/"),
    (CUSTOM_SERVER_LABEL,      ""),
]
DEFAULT_SERVER_DISPLAY = SERVER_CHOICES[0][0]
DEFAULT_SERVER_URL = SERVER_CHOICES[0][1]
EXPIRE = "1day"
OPEN_DISCUSSION = 1
BURN_AFTER_READING = 0
FORMAT = "plaintext"
COMPRESSION = "none"
ITERATIONS = 100000
KEYSIZE = 256
TAGSIZE = 128
ALGO = "aes"
MODE = "gcm"
PASTE_CONTENT = "private隐私交流区"
REFRESH_INTERVAL = 3.0
REMIND_INTERVAL = 10
COMMENT_ATTEMPTS = 3

JOIN_COMMENT_TEXT = "新用户加入房间"
JOIN_WAIT_SECONDS = 10
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 25
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.35

LINK_OBFUSCATION_SEED = "PrivateBin-yuhg-Obfuscation-v2"
LINK_OBFUSCATION_PREFIX = "PBC1:"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# ---------------------------------------------------------------
# 每线程独立 Session
# ---------------------------------------------------------------
_thread_local = threading.local()


def _build_session() -> requests.Session:
    retry = Retry(
        total=RETRY_TOTAL, connect=RETRY_TOTAL, read=RETRY_TOTAL,
        status=RETRY_TOTAL, backoff_factor=RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry, pool_connections=10,
        pool_maxsize=10, pool_block=False,
    )
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


def get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = _build_session()
        _thread_local.session = s
    return s


def warmup(server: str) -> None:
    try:
        get_session().head(server, timeout=(5, 10), allow_redirects=True)
    except Exception:
        pass


# ---------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------
def base58encode(byte_arr: bytes) -> str:
    alphabet = ALPHABET
    alphabet_len = len(alphabet)
    pad = 0
    for b in byte_arr:
        if b == 0:
            pad += 1
        else:
            break
    n = int.from_bytes(byte_arr, "big")
    if n == 0:
        return alphabet[0] * pad
    result = ""
    while n > 0:
        n, rem = divmod(n, alphabet_len)
        result = alphabet[rem] + result
    return alphabet[0] * pad + result


def base58decode(s: str) -> bytes:
    alphabet = ALPHABET
    alphabet_len = len(alphabet)
    pad = 0
    for ch in s:
        if ch == alphabet[0]:
            pad += 1
        else:
            break
    n = 0
    for ch in s:
        n = n * alphabet_len + alphabet.index(ch)
    if n == 0:
        body = b""
    else:
        body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * pad + body


def _derive_obf_key() -> bytes:
    return hashlib.sha256(LINK_OBFUSCATION_SEED.encode("utf-8")).digest()


def obfuscate_link(url: str) -> str:
    key = _derive_obf_key()
    iv = get_random_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    ct, tag = cipher.encrypt_and_digest(url.encode("utf-8"))
    blob = iv + tag + ct
    b64 = base64.urlsafe_b64encode(blob).decode("utf-8").rstrip("=")
    return LINK_OBFUSCATION_PREFIX + b64


def deobfuscate_link(text: str) -> str:
    text = text.strip()
    if not text.startswith(LINK_OBFUSCATION_PREFIX):
        return text
    data = text[len(LINK_OBFUSCATION_PREFIX):]
    data += "=" * ((-len(data)) % 4)
    try:
        blob = base64.urlsafe_b64decode(data)
    except Exception:
        raise ValueError("混淆链接格式无效")
    if len(blob) < 12 + 16 + 1:
        raise ValueError("混淆链接内容过短")
    iv, tag, ct = blob[:12], blob[12:28], blob[28:]
    cipher = AES.new(_derive_obf_key(), AES.MODE_GCM, nonce=iv, mac_len=16)
    try:
        plaintext = cipher.decrypt_and_verify(ct, tag)
    except Exception:
        raise ValueError("混淆链接校验失败，可能被篡改或版本不匹配")
    return plaintext.decode("utf-8")


def _post(server: str, payload: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "JSONHttpRequest",
        "Accept": "application/json",
    }
    resp = get_session().post(
        server, json=payload, headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != 0:
        raise RuntimeError(f"服务器返回错误: {result.get('message', '未知错误')}")
    return result


def _get_paste(server: str, paste_id: str) -> dict:
    headers = {
        "X-Requested-With": "JSONHttpRequest",
        "Accept": "application/json",
    }
    url = f"{server}?pasteid={paste_id}"
    resp = get_session().get(
        url, headers=headers,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != 0:
        raise RuntimeError(f"获取失败: {data.get('message', '未知错误')}")
    return data


# ---------------------------------------------------------------
# 创建剪贴板（主端）
# ---------------------------------------------------------------
def create_paste(server: str, plaintext: str, nickname: str = "") -> dict:
    salt = get_random_bytes(8)
    iv = get_random_bytes(16)
    paste_key = get_random_bytes(32)
    aes_key = hashlib.pbkdf2_hmac(
        "sha256", paste_key, salt, ITERATIONS, KEYSIZE // 8
    )
    adata = [
        [
            base64.b64encode(iv).decode("utf-8"),
            base64.b64encode(salt).decode("utf-8"),
            ITERATIONS, KEYSIZE, TAGSIZE, ALGO, MODE, COMPRESSION,
        ],
        FORMAT, OPEN_DISCUSSION, BURN_AFTER_READING,
    ]
    adata_json = json.dumps(adata, separators=(",", ":"))
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv, mac_len=TAGSIZE // 8)
    cipher.update(adata_json.encode("utf-8"))
    paste_data = {"paste": plaintext}
    if nickname:
        paste_data["nickname"] = nickname
    paste_json = json.dumps(paste_data, separators=(",", ":"))
    ciphertext, tag = cipher.encrypt_and_digest(paste_json.encode("utf-8"))
    payload = {
        "v": 2, "adata": adata,
        "ct": base64.b64encode(ciphertext + tag).decode("utf-8"),
        "meta": {"expire": EXPIRE},
    }
    result = _post(server, payload)
    paste_id = result["id"]
    key_fragment = base58encode(paste_key)
    return {
        "server": server, "id": paste_id,
        "full_url": f"{server}?{paste_id}#{key_fragment}",
        "deletetoken": result.get("deletetoken", ""),
        "key_fragment": key_fragment,
    }


def create_paste_with_fallback(plaintext: str, nickname: str = "",
                               server: str = None) -> dict:
    primary = server or DEFAULT_SERVER_URL
    last_err = None
    try:
        return create_paste(primary, plaintext, nickname)
    except Exception as e:
        last_err = e
    raise last_err if last_err else RuntimeError("没有可用的服务器")


# ---------------------------------------------------------------
# 删除剪贴板
# ---------------------------------------------------------------
def delete_paste(paste_info: dict) -> dict:
    server = paste_info.get("server", "")
    paste_id = paste_info.get("id", "")
    token = paste_info.get("deletetoken", "")
    if not server or not paste_id:
        raise RuntimeError("缺少服务器或剪贴板 ID，无法删除")
    if not token:
        raise RuntimeError("缺少删除令牌（deletetoken），无法删除")
    payload = {"v": 2, "pasteid": paste_id, "deletetoken": token}
    return _post(server, payload)


# ---------------------------------------------------------------
# URL 解析（客端）
# ---------------------------------------------------------------
def parse_paste_url(url: str) -> dict:
    url = url.strip()
    if not url:
        raise ValueError("URL 不能为空")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("URL 格式无效")
    server = f"{parsed.scheme}://{parsed.netloc}/"
    paste_id = ""
    if parsed.query:
        if "=" in parsed.query:
            for kv in parsed.query.split("&"):
                k, _, v = kv.partition("=")
                if k.lower() in ("pasteid", "id"):
                    paste_id = v
                    break
        else:
            paste_id = parsed.query
    if not paste_id:
        raise ValueError("URL 中未找到聊天室 ID")
    key_fragment = parsed.fragment
    if not key_fragment:
        raise ValueError("URL 中未找到密钥片段（# 后面的部分），无法解密消息")
    if not re.match(r"^[a-zA-Z0-9]+$", paste_id):
        raise ValueError(f"剪贴板 ID 格式无效: {paste_id}")
    if not re.match(r"^[1-9A-HJ-NP-Za-km-z]+$", key_fragment):
        raise ValueError(f"密钥片段格式无效: {key_fragment}")
    return {
        "server": server, "id": paste_id,
        "key_fragment": key_fragment, "full_url": url,
    }


# ---------------------------------------------------------------
# 发送评论
# ---------------------------------------------------------------
_comment_send_lock = threading.Lock()


def add_comment(paste_info: dict, comment_text: str, nickname: str = "") -> dict:
    paste_id = paste_info["id"]
    server = paste_info["server"]
    paste_key = base58decode(paste_info["key_fragment"])
    salt = get_random_bytes(8)
    iv = get_random_bytes(16)
    aes_key = hashlib.pbkdf2_hmac(
        "sha256", paste_key, salt, ITERATIONS, KEYSIZE // 8
    )
    comment_adata = [
        base64.b64encode(iv).decode("utf-8"),
        base64.b64encode(salt).decode("utf-8"),
        ITERATIONS, KEYSIZE, TAGSIZE, ALGO, MODE, COMPRESSION,
    ]
    adata_json = json.dumps(comment_adata, separators=(",", ":"))
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv, mac_len=TAGSIZE // 8)
    cipher.update(adata_json.encode("utf-8"))
    comment_data = {"comment": comment_text}
    if nickname:
        comment_data["nickname"] = nickname
    comment_json = json.dumps(comment_data, separators=(",", ":"))
    ciphertext, tag = cipher.encrypt_and_digest(comment_json.encode("utf-8"))
    payload = {
        "v": 2, "adata": comment_adata,
        "ct": base64.b64encode(ciphertext + tag).decode("utf-8"),
        "pasteid": paste_id, "parentid": paste_id,
    }
    return _post(server, payload)


def add_comment_with_retry(paste_info: dict, comment_text: str,
                           nickname: str = "",
                           attempts: int = COMMENT_ATTEMPTS) -> dict:
    last_err = None
    for i in range(attempts):
        try:
            return add_comment(paste_info, comment_text, nickname)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.6 * (2 ** i))
    raise last_err


# ---------------------------------------------------------------
# 获取并解密评论
# ---------------------------------------------------------------
def _decrypt_comment(comment_obj: dict, paste_key: bytes) -> dict:
    adata = comment_obj["adata"]
    params = adata[0] if isinstance(adata[0], list) else adata
    iv = base64.b64decode(params[0])
    salt = base64.b64decode(params[1])
    iterations = params[2]
    keysize = params[3]
    tagsize = params[4]
    aes_key = hashlib.pbkdf2_hmac(
        "sha256", paste_key, salt, iterations, keysize // 8
    )
    ct = base64.b64decode(comment_obj["ct"])
    tag_len = tagsize // 8
    tag = ct[-tag_len:]
    ciphertext = ct[:-tag_len]
    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv, mac_len=tag_len)
    adata_json = json.dumps(adata, separators=(",", ":"))
    cipher.update(adata_json.encode("utf-8"))
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return json.loads(plaintext.decode("utf-8"))


def fetch_comments(paste_info: dict) -> list:
    paste_key = base58decode(paste_info["key_fragment"])
    data = _get_paste(paste_info["server"], paste_info["id"])
    comments_raw = data.get("comments", [])
    if isinstance(comments_raw, dict):
        comment_list = list(comments_raw.values())
    else:
        comment_list = comments_raw
    result = []
    for c in comment_list:
        posttime = c.get("meta", {}).get("posttime") or c.get("posttime")
        try:
            dec = _decrypt_comment(c, paste_key)
            result.append({
                "nickname": dec.get("nickname", ""),
                "comment": dec.get("comment", ""),
                "posttime": posttime, "id": c.get("id", ""),
                "error": False,
            })
        except Exception as e:
            err = str(e)
            result.append({
                "nickname": "",
                "comment": f"[解密失败: {err}]",
                "posttime": posttime, "id": c.get("id", ""),
                "error": True,
            })
    result.sort(key=lambda x: x.get("posttime") or 0)
    return result


def format_time(posttime) -> str:
    if not posttime:
        return ""
    try:
        return datetime.fromtimestamp(int(posttime)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ""


# ==================== Kivy RootWidget ====================
class RootWidget(BoxLayout):
    """根组件，与 privatebin.kv 中的 <RootWidget> 对应。"""
    pass


# ==================== Kivy App ====================
class PrivateBinApp(App):
    # UI 绑定属性
    mode = StringProperty('client')
    status_text = StringProperty('')
    reminder_text = StringProperty('准备中...')
    link_text = StringProperty('')
    comments_text = StringProperty('')
    is_host = BooleanProperty(False)
    is_client = BooleanProperty(True)
    is_connected = BooleanProperty(False)
    server_display = StringProperty(DEFAULT_SERVER_DISPLAY)
    custom_server = StringProperty('')

    # 供 KV 使用的下拉列表
    server_names = ListProperty([name for name, _ in SERVER_CHOICES])
    custom_label = StringProperty(CUSTOM_SERVER_LABEL)

    # 内部状态
    paste_info = None
    last_comment_count = -1
    countdown = REMIND_INTERVAL
    _timers_running = False
    _ui_queue = queue.Queue()
    _creating = False
    _deleting = False
    _connecting = False
    _join_sending = False
    _join_waiting = False
    _join_wait_seconds = 0
    _current_server_url = DEFAULT_SERVER_URL

    def build(self):
        self.title = '隐私通讯程式'
        if platform not in ('android', 'ios'):
            from kivy.core.window import Window
            Window.size = (900, 760)
        self.root_widget = RootWidget()
        return self.root_widget

    def on_start(self):
        Clock.schedule_interval(self._pump_ui_queue, 0.05)
        self._switch_mode('client')

    def on_stop(self):
        # 退出时清理主端房间
        info = self.paste_info
        self.paste_info = None
        if info and info.get("deletetoken"):
            self._delete_room_async(info)

    # ============ UI 队列 ============
    def _post_ui(self, callback):
        try:
            self._ui_queue.put_nowait(callback)
        except Exception:
            pass

    def _pump_ui_queue(self, dt):
        processed = 0
        while processed < 100:
            try:
                cb = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb()
            except Exception:
                pass
            processed += 1

    # ============ 模式切换 ============
    def _switch_mode(self, mode):
        if mode == self.mode:
            return
        self._cleanup_before_leaving_host()
        self._disconnect(silent=True)
        self.mode = mode
        self.is_host = (mode == 'host')
        self.is_client = (mode == 'client')
        if mode == 'host':
            self.reminder_text = '正在创建聊天室...'
            self.status_text = ''
            self._show_welcome()
            Clock.schedule_once(lambda dt: self._create_paste_async(), 0.1)
        else:
            self.reminder_text = '请粘贴主端分享的链接并点击「连接」'
            self.status_text = ''
            self._show_welcome()

    def _show_welcome(self):
        welcome = (
            "欢迎使用隐私通讯程式\n\n"
            "· 客端：粘贴主端分享的混淆文本（PBC1: 开头）或原始链接，点击「连接」\n"
            "    连接成功后会自动发送「新用户加入房间」，10 秒后即可开始评论\n"
            "· 主端：在「创建聊天室（主端）」模式下，可选择服务器或自定义实例\n"
            "    切换服务器会自动删除旧房间并用新实例重建\n\n"
            "本程式旨在隐私通讯，请勿泄露聊天室链接，请勿违反当地法律法规\n"
        )
        self.comments_text = welcome

    # ============ 服务器选择 ============
    def _normalize_server_url(self, url):
        url = (url or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url
        if not url.endswith("/"):
            url += "/"
        return url

    def _get_selected_server(self):
        name = self.server_display
        if name == CUSTOM_SERVER_LABEL:
            return self._normalize_server_url(self.custom_server)
        for n, url in SERVER_CHOICES:
            if n == name and url:
                return url
        return DEFAULT_SERVER_URL

    def on_server_display(self, instance, value):
        if not self.is_host:
            return
        if value == CUSTOM_SERVER_LABEL:
            self.status_text = "请输入自定义 PrivateBin 实例地址"
            return
        new_url = self._get_selected_server()
        if new_url == self._current_server_url:
            return
        if self._creating:
            return
        self._retry_create()

    def apply_custom_server(self):
        url = self._normalize_server_url(self.custom_server)
        if not url:
            self._popup('提示', '请输入自定义 PrivateBin 实例地址')
            return
        if not re.match(r"^https?://[^\s/]+", url, re.IGNORECASE):
            self._popup('地址无效',
                        '请输入有效的网址，例如：\n'
                        '  https://my-privatebin.example.com/\n'
                        '  my-bin.example.com')
            return
        if self._creating:
            return
        threading.Thread(target=warmup, args=(url,), daemon=True).start()
        self._retry_create()

    # ============ 旧聊天室清理 ============
    def _delete_room_async(self, info):
        if not info or not info.get("deletetoken"):
            return

        def worker():
            try:
                delete_paste(info)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _cleanup_before_leaving_host(self):
        info = self.paste_info
        if info and info.get("deletetoken") and not self._deleting:
            self._delete_room_async(info)

    # ============ 主端：创建 ============
    def _create_paste_async(self):
        if self._creating or not self.is_host:
            return
        self._creating = True
        threading.Thread(target=self._create_paste_thread, daemon=True).start()

    def _create_paste_thread(self):
        server = self._get_selected_server()
        if not server:
            self._post_ui(lambda: self._on_create_failed(
                "请先填入自定义 PrivateBin 实例地址"))
            return
        try:
            info = create_paste_with_fallback(PASTE_CONTENT, server=server)
        except Exception as e:
            err_msg = str(e)
            self._post_ui(lambda msg=err_msg: self._on_create_failed(msg))
            return
        self._post_ui(lambda info=info: self._on_create_done(info))

    def _on_create_failed(self, err):
        self._creating = False
        if not self.is_host:
            return
        self.link_text = '创建失败'
        self.reminder_text = f'聊天室创建失败: {err}'
        self.status_text = ('可在顶部切换其他服务器、检查自定义实例地址，'
                            '或点击「重新创建」再试')

    def _on_create_done(self, info):
        self._creating = False
        if not self.is_host:
            self._delete_room_async(info)
            return
        self.paste_info = info
        self._current_server_url = info.get("server", DEFAULT_SERVER_URL)
        try:
            display = obfuscate_link(info["full_url"])
        except Exception:
            display = info["full_url"]
        self.link_text = display
        self.status_text = (f'聊天室已就绪（{self._current_server_url}），'
                            f'请把混淆链接分享给客端')
        self.reminder_text = '已创建，正在加载聊天内容...'
        self.last_comment_count = -1
        self.countdown = REMIND_INTERVAL
        self._start_timers()

    def _retry_create(self):
        if self._creating or not self.is_host:
            return
        old_info = self.paste_info
        if old_info and old_info.get("deletetoken"):
            self._delete_room_async(old_info)
        self.link_text = '正在创建聊天室...'
        self.reminder_text = '正在重新创建...'
        self.status_text = ''
        self.paste_info = None
        self.last_comment_count = -1
        self._create_paste_async()

    # ============ 主端：删除 ============
    def _delete_room(self):
        if self._deleting or self._creating or not self.paste_info:
            return
        if not self.paste_info.get("deletetoken"):
            self._popup('无法删除', '当前聊天室没有可用的删除令牌。')
            return
        content = BoxLayout(orientation='vertical', spacing=10, padding=10)
        content.add_widget(Label(
            text='删除后所有聊天记录将被永久销毁，客端将无法再访问该聊天室，'
                 '且无法恢复。\n\n确定要删除吗？'))
        btn_box = BoxLayout(size_hint_y=None, height='48dp', spacing=10)
        from kivy.uix.button import Button
        yes_btn = Button(text='删除', background_color=(0.8, 0.2, 0.2, 1))
        no_btn = Button(text='取消')
        btn_box.add_widget(yes_btn)
        btn_box.add_widget(no_btn)
        content.add_widget(btn_box)
        popup = Popup(title='确认删除聊天室', content=content,
                      size_hint=(0.85, 0.45))
        yes_btn.bind(on_release=lambda x: (popup.dismiss(),
                                            self._do_delete_room()))
        no_btn.bind(on_release=lambda x: popup.dismiss())
        popup.open()

    def _do_delete_room(self):
        self._deleting = True
        self.status_text = '正在删除聊天室...'
        self.reminder_text = '正在删除聊天室...'
        paste_info = self.paste_info
        threading.Thread(target=self._delete_room_worker,
                         args=(paste_info,), daemon=True).start()

    def _delete_room_worker(self, paste_info):
        try:
            delete_paste(paste_info)
        except Exception as e:
            err_msg = str(e)
            self._post_ui(lambda msg=err_msg: self._on_delete_failed(msg))
            return
        self._post_ui(self._on_delete_done)

    def _on_delete_done(self):
        self._deleting = False
        self.paste_info = None
        self.last_comment_count = -1
        if not self.is_host:
            return
        self.link_text = ''
        self.status_text = '聊天室已删除'
        self.reminder_text = '聊天室已删除，可点击「重新创建」再建一个新的'
        self.comments_text = (
            '聊天室已删除。\n\n'
            '所有历史评论已从服务器销毁，客端将无法再访问。\n'
            '点击「重新创建」可建立一个全新的聊天室。\n')

    def _on_delete_failed(self, err):
        self._deleting = False
        if not self.is_host:
            return
        self.status_text = f'删除失败: {err}'
        self.reminder_text = f'删除失败: {err}'

    # ============ 客端：连接 / 断开 ============
    def _connect_client(self, url_text):
        if self._connecting or not self.is_client:
            return
        raw = (url_text or '').strip()
        if not raw:
            self._popup('提示', '请先粘贴主端分享的链接')
            return
        try:
            url = deobfuscate_link(raw)
        except ValueError as e:
            self._popup('混淆文本无效', str(e))
            return
        try:
            info = parse_paste_url(url)
        except ValueError as e:
            self._popup('链接无效', str(e))
            return
        self._connecting = True
        self.status_text = f"正在连接: {info['id']}"
        threading.Thread(target=warmup,
                         args=(info["server"],), daemon=True).start()
        self.paste_info = info
        self.last_comment_count = -1
        self._join_sending = False
        self._join_waiting = True
        self._join_wait_seconds = 0
        self.is_connected = True
        self.status_text = f"已连接: {info['id']}"
        self.reminder_text = '已连接，正在发送加入通知...'
        self.countdown = REMIND_INTERVAL
        self._start_timers()
        Clock.schedule_once(lambda dt: self._connect_done(), 0.3)

    def _connect_done(self):
        self._connecting = False
        self._send_join_comment_async()

    def _disconnect(self, silent=False):
        was_client = self.is_client
        self.paste_info = None
        self.last_comment_count = -1
        self._connecting = False
        self._creating = False
        self._deleting = False
        self._join_sending = False
        self._join_waiting = False
        self._join_wait_seconds = 0
        self.is_connected = False
        if silent:
            return
        if was_client:
            self.reminder_text = '已断开，请输入新链接'
            self.status_text = ''
            self._show_welcome()

    # ============ 客端：加入通知 ============
    def _send_join_comment_async(self):
        if not self.is_client or not self.paste_info:
            return
        paste_info = self.paste_info
        self._join_sending = True
        self._join_waiting = True
        self.status_text = '正在发送「新用户加入房间」...'
        self.reminder_text = '正在发送「新用户加入房间」...'
        threading.Thread(target=self._send_join_comment_worker,
                         args=(paste_info,), daemon=True).start()

    def _send_join_comment_worker(self, paste_info):
        try:
            with _comment_send_lock:
                if self.paste_info is not paste_info:
                    return
                add_comment_with_retry(paste_info, JOIN_COMMENT_TEXT, "")
        except Exception as e:
            err_msg = str(e)
            self._post_ui(lambda msg=err_msg, info=paste_info:
                          self._on_join_comment_failed(msg, info))
            return
        self._post_ui(lambda info=paste_info:
                      self._on_join_comment_sent(info))

    def _on_join_comment_sent(self, paste_info):
        self._join_sending = False
        if self.paste_info is not paste_info or not self.is_client:
            self._join_waiting = False
            return
        self.status_text = f'已发送加入通知，{JOIN_WAIT_SECONDS} 秒后可开始发消息'
        self.reminder_text = f'{JOIN_WAIT_SECONDS} 秒后可开始发消息...'
        self._refresh_comments_async()
        self._join_waiting = True
        self._join_wait_seconds = JOIN_WAIT_SECONDS
        Clock.schedule_once(lambda dt: self._tick_join_wait(paste_info), 1.0)

    def _on_join_comment_failed(self, err, paste_info):
        self._join_sending = False
        if self.paste_info is not paste_info or not self.is_client:
            self._join_waiting = False
            return
        self._disconnect()
        self.reminder_text = '加入通知发送失败，已断开，请重新连接'
        self.status_text = f'加入通知发送失败: {err}'
        self.comments_text = (
            f'加入通知发送失败，已断开与该聊天室的连接。\n\n'
            f'错误信息：{err}\n\n'
            '可能原因：网络不稳定、服务器暂时不可用、或聊天室已被主端删除。\n\n'
            '请确认网络后，重新点击「连接」按钮重试。')

    def _tick_join_wait(self, paste_info):
        if self.paste_info is not paste_info or not self.is_client:
            self._join_waiting = False
            return
        if self._join_wait_seconds > 0:
            self.reminder_text = f'{self._join_wait_seconds} 秒后可开始发消息...'
            self._join_wait_seconds -= 1
            Clock.schedule_once(lambda dt: self._tick_join_wait(paste_info), 1.0)
            return
        self._join_waiting = False
        self.status_text = '现在可以开始发消息了'
        self.reminder_text = '可以开始发消息了'

    # ============ 定时器 ============
    def _start_timers(self):
        self._refresh_comments_async()
        if not self._timers_running:
            self._timers_running = True
            Clock.schedule_interval(self._refresh_tick, REFRESH_INTERVAL)
            Clock.schedule_interval(self._tick_reminder, 1.0)

    def _refresh_tick(self, dt):
        if self.paste_info is not None and not self._deleting:
            self._refresh_comments_async()

    def _refresh_comments_async(self):
        if not self.paste_info:
            return
        paste_info = self.paste_info
        threading.Thread(target=self._refresh_comments_thread,
                         args=(paste_info,), daemon=True).start()

    def _refresh_comments_thread(self, paste_info):
        try:
            comments = fetch_comments(paste_info)
        except Exception as e:
            err_msg = str(e)
            if self.paste_info is paste_info:
                self._post_ui(lambda msg=err_msg:
                              self._set_status(f'刷新失败: {msg}'))
            return
        if self.paste_info is paste_info:
            self._post_ui(lambda c=comments:
                          self._update_comments_display(c))

    def _tick_reminder(self, dt):
        if (self.paste_info is not None and not self._deleting
                and not self._join_waiting):
            self.countdown -= 1
            if self.countdown <= 0:
                self.reminder_text = '可以发送内容了！'
                Clock.schedule_once(self._reset_reminder, 2.5)
                self.countdown = REMIND_INTERVAL
            else:
                self.reminder_text = f'距下次提醒还有 {self.countdown} 秒'

    def _reset_reminder(self, dt):
        self.reminder_text = ''

    # ============ 评论显示 ============
    def _update_comments_display(self, comments):
        if len(comments) == self.last_comment_count:
            self.status_text = f'已同步 ({len(comments)} 条消息)'
            return
        self.last_comment_count = len(comments)
        lines = []
        if not comments:
            lines.append('（暂无对话内容）')
        else:
            for i, c in enumerate(comments, 1):
                time_str = format_time(c.get("posttime"))
                nickname = c.get("nickname") or "匿名"
                lines.append(f'[{i}] {nickname}  {time_str}')
                lines.append(c["comment"])
                lines.append('')
        self.comments_text = '\n'.join(lines)
        self.status_text = f'已同步 ({len(comments)} 条消息)'
        # 滚动到底部
        try:
            sv = self.root_widget.ids.get('comments_scroll')
            if sv is not None:
                Clock.schedule_once(lambda dt: setattr(sv, 'scroll_y', 0), 0.05)
        except Exception:
            pass

    # ============ 发送评论 ============
    def send_comment(self, text, nickname):
        if not self.paste_info or self._deleting:
            return
        if self.is_client and self._join_waiting:
            self.status_text = (f'请稍候，{max(self._join_wait_seconds, 0)} '
                                f'秒后可开始发消息')
            return
        text = (text or '').strip()
        if not text:
            self.status_text = '发送内容不能为空'
            return
        self.status_text = '正在发送...'
        paste_info = self.paste_info
        threading.Thread(target=self._send_comment_worker,
                         args=(paste_info, text, nickname),
                         daemon=True).start()

    def _send_comment_worker(self, paste_info, text, nickname):
        with _comment_send_lock:
            if self.paste_info is not paste_info:
                return
            try:
                add_comment_with_retry(paste_info, text, nickname)
            except Exception as e:
                err_msg = str(e)
                self._post_ui(lambda msg=err_msg: self._on_send_fail(msg))
                return
            self._post_ui(lambda: self._on_send_ok(paste_info))

    def _on_send_ok(self, paste_info):
        if self.paste_info is not paste_info:
            return
        self.status_text = '消息已发送'
        Clock.schedule_once(lambda dt: self._refresh_comments_async(), 1.0)

    def _on_send_fail(self, err):
        self.status_text = f'发送失败: {err}'

    # ============ 杂项 ============
    def _set_status(self, text):
        self.status_text = text

    def copy_link(self):
        if not self.paste_info:
            return
        Clipboard.copy(self.link_text)
        self.status_text = '已复制混淆链接（可分享给客端）'

    def copy_raw_link(self):
        if not self.paste_info:
            return
        raw = self.paste_info.get("full_url", "")
        if not raw:
            return
        Clipboard.copy(raw)
        self.status_text = '已复制原始链接（请勿公开分享）'

    def _popup(self, title, msg):
        popup = Popup(title=title, content=Label(text=msg),
                      size_hint=(0.85, 0.35))
        popup.open()


if __name__ == '__main__':
    PrivateBinApp().run()
