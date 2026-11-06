"""代理支持：HTTP(S) 用 urllib 自带能力，SOCKS5 用标准库手写隧道。

为什么不用 PySocks：本项目坚持零第三方依赖（fnOS 上只保证有 python3）。
所以 SOCKS5 的握手（RFC 1928）自己实现，然后套到 http.client 的连接上。
"""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.parse
import urllib.request

DEFAULT_PORTS = {"http": 8080, "https": 8080, "socks5": 1080, "socks5h": 1080}


def parse_proxy(url: str):
    """把代理地址解析成 (scheme, host, port)；空或非法返回 None。

    支持：192.168.1.10:7890 / http://host:port / socks5://host:port / socks5h://...
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        p = urllib.parse.urlparse(raw)
        scheme = (p.scheme or "http").lower()
        host = p.hostname
        if not host:
            return None
        port = p.port or DEFAULT_PORTS.get(scheme, 8080)
        return scheme, host, int(port)
    except Exception:
        return None


# ─────────────────────────── SOCKS5 ───────────────────────────
def _socks5_tunnel(proxy_host: str, proxy_port: int, dst_host: str,
                   dst_port: int, timeout: float | None) -> socket.socket:
    """建立到 dst 的 SOCKS5 隧道（无认证 / 用户名密码不支持）。"""
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")                      # VER=5, NMETHODS=1, NO AUTH
        resp = sock.recv(2)
        if len(resp) < 2 or resp[0] != 0x05:
            raise OSError("SOCKS5 握手失败（不是 SOCKS5 代理？）")
        if resp[1] != 0x00:
            raise OSError(f"SOCKS5 要求认证（method={resp[1]}），本网关只支持无认证代理")

        host_bytes = dst_host.encode("idna")
        req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes \
              + int(dst_port).to_bytes(2, "big")
        sock.sendall(req)

        head = sock.recv(4)
        if len(head) < 4 or head[0] != 0x05:
            raise OSError("SOCKS5 应答异常")
        if head[1] != 0x00:
            codes = {1: "一般失败", 2: "规则不允许", 3: "网络不可达",
                     4: "主机不可达", 5: "连接被拒", 6: "TTL 超时",
                     7: "不支持的命令", 8: "不支持的地址类型"}
            raise OSError(f"SOCKS5 连接失败：{codes.get(head[1], head[1])}")

        atyp = head[3]
        if atyp == 0x01:                                   # IPv4
            sock.recv(4)
        elif atyp == 0x03:                                 # 域名
            ln = sock.recv(1)
            if ln:
                sock.recv(ln[0])
        elif atyp == 0x04:                                 # IPv6
            sock.recv(16)
        sock.recv(2)                                       # 端口
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


class _SocksHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, *, proxy=("", 0), timeout=None):
        super().__init__(host, port, timeout=timeout)
        self._proxy = proxy

    def connect(self):
        self.sock = _socks5_tunnel(self._proxy[0], self._proxy[1],
                                   self.host, self.port, self.timeout)


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, *, proxy=("", 0), timeout=None, context=None):
        super().__init__(host, port, timeout=timeout, context=context)
        self._proxy = proxy

    def connect(self):
        raw = _socks5_tunnel(self._proxy[0], self._proxy[1],
                             self.host, self.port, self.timeout)
        ctx = self._context if self._context is not None else ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)


class _SocksHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    def __init__(self, proxy_host: str, proxy_port: int, timeout: float | None = None):
        urllib.request.HTTPHandler.__init__(self)
        urllib.request.HTTPSHandler.__init__(self)
        self._p = (proxy_host, proxy_port)
        self._t = timeout

    def http_open(self, req):
        # 注意：do_open 自己会传 timeout，这里不能再传，否则关键字冲突
        return self.do_open(
            lambda host, **kw: _SocksHTTPConnection(host, proxy=self._p, **kw), req)

    def https_open(self, req):
        return self.do_open(
            lambda host, **kw: _SocksHTTPSConnection(host, proxy=self._p, **kw), req)


# ─────────────────────────── 对外 ───────────────────────────
def build_opener(proxy_url: str, timeout: float | None = None):
    """按代理地址返回一个 opener；没配代理返回 None（此时用默认 urlopen，会读环境变量）。"""
    parsed = parse_proxy(proxy_url)
    if not parsed:
        return None
    scheme, host, port = parsed
    if scheme in ("socks5", "socks5h", "socks"):
        # 我们始终在本地解析域名（socks5h 的差别在于由代理解析，
        # 这里统一用远端解析会更稳，所以 socks5h 也走域名方式）
        return urllib.request.build_opener(_SocksHandler(host, port, timeout))
    handler = urllib.request.ProxyHandler({"http": f"http://{host}:{port}",
                                           "https": f"http://{host}:{port}"})
    return urllib.request.build_opener(handler)
