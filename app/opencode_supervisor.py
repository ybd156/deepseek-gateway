"""OpenCode 运行时托管：把 `opencode serve` 当成网关的子进程来管。

能力：
  - 查找可用的 opencode 可执行文件（配置路径 → 应用私有安装 → PATH）
  - 启动 / 停止 / 探活 / 拉日志
  - 看门狗：意外退出后按退避自动重启
  - 一键安装：把 Node + opencode 装进应用私有目录（不污染系统）

只用标准库。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time


class OpenCodeSupervisor:
    # 应用以专用用户运行时 PATH 往往很干净，显式兜底这些目录
    EXTRA_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin",
                      "/usr/local/sbin", "/usr/sbin", "/sbin",
                      "/opt/bin", "/snap/bin", "/home/linuxbrew/.linuxbrew/bin")

    def __init__(self, data_dir: str, get_opts) -> None:
        """get_opts() 返回 dict：bin / port / workspace / autostart / enabled。"""
        self.data_dir = data_dir
        self.get_opts = get_opts
        self.proc: subprocess.Popen | None = None
        self.started_at: float = 0.0
        self.restarts: int = 0
        self.last_error: str = ""
        self._lock = threading.Lock()
        self._stop_watch = False
        self.log_path = os.path.join(data_dir, "opencode.log")
        self._ver_cache: dict[str, str] = {}

    # ── 可执行文件查找 ──
    @classmethod
    def which(cls, name: str) -> str:
        found = shutil.which(name)
        if found:
            return found
        for d in cls.EXTRA_BIN_DIRS:
            cand = os.path.join(d, name)
            if os.path.exists(cand) and os.access(cand, os.X_OK):
                return cand
        return ""

    def _version(self, exe: str, flag: str = "-v") -> str:
        key = exe + flag
        if key in self._ver_cache:
            return self._ver_cache[key]
        out = ""
        try:
            out = subprocess.run([exe, flag], capture_output=True, text=True,
                                 timeout=6).stdout.strip().splitlines()[0]
        except Exception:
            out = ""
        self._ver_cache[key] = out
        return out

    # ── 路径与探活 ──
    @property
    def private_bin(self) -> str:
        base = os.path.join(self.data_dir, "npm", "bin")
        for n in ("opencode2", "opencode"):
            p = os.path.join(base, n)
            if os.path.exists(p):
                return p
        return os.path.join(base, "opencode2")

    # v2（@opencode/cli）装出来的可执行文件叫 opencode2，v1（opencode-ai）叫 opencode
    BIN_NAMES = ("opencode2", "opencode")

    def _private_bins(self) -> list:
        base = os.path.join(self.data_dir, "npm", "bin")
        return [os.path.join(base, n) for n in self.BIN_NAMES]

    def resolve_bin(self) -> str:
        opts = self.get_opts() or {}
        cand = str(opts.get("bin") or "").strip()
        if cand and os.path.exists(cand):
            return cand
        # 应用内安装的优先（新版在前）
        for p in self._private_bins():
            if os.path.exists(p):
                return p
        for n in self.BIN_NAMES:
            hit = self.which(n)
            if hit:
                return hit
        return ""

    def bin_source(self) -> str:
        opts = self.get_opts() or {}
        cand = str(opts.get("bin") or "").strip()
        if cand and os.path.exists(cand):
            return "手动指定"
        for p in self._private_bins():
            if os.path.exists(p):
                return "应用内安装"
        for n in self.BIN_NAMES:
            if self.which(n):
                return "系统"
        return "无"

    def port_open(self) -> bool:
        port = int((self.get_opts() or {}).get("port") or 4096)
        s = socket.socket()
        s.settimeout(0.6)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except Exception:
            return False
        finally:
            s.close()

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @staticmethod
    def _strip_ansi(text: str) -> str:
        out, i = [], 0
        while i < len(text):
            ch = text[i]
            if ch == chr(27):
                j = i + 1
                if j < len(text) and text[j] == "[":
                    j += 1
                    while j < len(text) and not ("@" <= text[j] <= "~"):
                        j += 1
                    i = j + 1
                    continue
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _drop_csi_leftovers(text: str) -> str:
        """ESC 被吃掉后常剩下 [39m / [7m 这种残渣，一并清掉。"""
        out, i = [], 0
        while i < len(text):
            ch = text[i]
            if ch == "[":
                j = i + 1
                while j < len(text) and (text[j].isdigit() or text[j] == ";"):
                    j += 1
                if j > i + 1 and j < len(text) and text[j].isalpha():
                    i = j + 1
                    continue
            # 裸的 28m / 7m 这种（ESC 和 [ 都没了）
            if ch.isdigit() and (i == 0 or text[i - 1] in " [;"):
                j = i
                while j < len(text) and text[j].isdigit():
                    j += 1
                if j - i <= 3 and j < len(text) and text[j] in "mM":
                    i = j + 1
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    @classmethod
    def _clean_pty(cls, raw: str) -> str:
        """TUI 会逐字符重绘，这里把 ANSI 去掉、把碎行拼回去。"""
        text = cls._strip_ansi(raw)
        text = cls._drop_csi_leftovers(text)
        text = text.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
        lines = text.split(chr(10))
        short = sum(1 for ln in lines if len(ln.strip()) == 1)
        if lines and short > len(lines) // 2:
            return "".join(lines)
        return chr(10).join(ln for ln in lines)

    STOP_CHARS = (" ", chr(9), chr(10), chr(13), chr(34), chr(39),
                  "<", ">", chr(65289), chr(65292), chr(12290), chr(65307))

    @classmethod
    def _find_urls(cls, text: str) -> list:
        urls, i = [], 0
        while True:
            i = text.find("https://", i)
            if i < 0:
                break
            j = i
            while j < len(text) and text[j] not in cls.STOP_CHARS:
                j += 1
            urls.append(text[i:j])
            i = j
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def login_state(self) -> dict:
        """把 PTY 的原始输出翻译成「第几步、该做什么」。"""
        raw = ""
        try:
            raw = open(self.login_log_path(), "rb").read().decode("utf-8", "ignore")
        except Exception:
            pass
        text = self._clean_pty(raw)
        low = text.lower()
        urls = self._find_urls(text)
        scr = " ".join(text.split()) if text.strip() else ""
        # 去掉开头残留的 SGR 片段（如 39m_）和光标占位符
        for _ in range(8):
            if not scr:
                break
            j = 0
            while j < len(scr) and (scr[j].isdigit() or scr[j] in "[;"):
                j += 1
            if 0 < j < len(scr) and scr[j] in "mM":
                scr = scr[j + 1:].lstrip("_ ")
                continue
            break
        scr = scr.replace("_", " ")
        screen = scr[-180:]
        st = {"state": "idle", "screen": screen, "title": "还没开始登录",
              "hint": "点下面的「登录 OpenCode」开始，全程三步。",
              "actions": [], "url": "", "raw": text[-3000:]}
        if not text.strip():
            return st

        if "select integration" in low or "connect an integration" in low:
            st.update(state="pick_integration",
                      title="第 1 步 · 选择登录到哪个服务",
                      hint="免费模型和按量付费模型都在「OpenCode Console」里，选它。",
                      actions=[{"id": "pick-console", "label": "选 OpenCode Console"},
                               {"id": "pick-go", "label": "选 OpenCode Go（订阅制）"}])
            return st

        if ("select method" in low or "select a method" in low
                or "authentication method" in low or "login method" in low):
            st.update(state="pick_method",
                      title="第 2 步 · 选择登录方式",
                      hint="选带浏览器 / OAuth 的那一项（一般第一项就是）。",
                      actions=[{"id": "enter", "label": "选第一项（回车）"}])
            return st

        if urls:
            st.update(state="authorize",
                      title="第 3 步 · 在浏览器里点同意",
                      hint="复制这个链接，在电脑或手机上打开并授权，然后回来看结果。",
                      url=urls[0],
                      actions=[{"id": "login-log", "label": "我已授权，刷新结果"}])
            return st

        for kw in ("logged in", "login successful", "connected", "authenticated",
                   "account added", "success", "done"):
            if kw in low:
                if "opencode go" in low:
                    st.update(state="done", title="已连接 OpenCode Go（订阅制）",
                              hint="Go 是按订阅计费的。如果你没有订阅，请重新登录并选 "
                                   "「OpenCode Console」。先点下面验证一下。",
                              actions=[{"id": "ping", "label": "试一句验证"},
                                       {"id": "login", "label": "重新登录（改选 Console）"}])
                elif "opencode console" in low:
                    st.update(state="done", title="已连接 OpenCode Console",
                              hint="点下面验证一下，通了就把上游启用。",
                              actions=[{"id": "ping", "label": "试一句验证"},
                                       {"id": "login", "label": "重新登录"}])
                else:
                    st.update(state="done", title="登录成功",
                              hint="点下面验证一下，通了就把上游启用。",
                              actions=[{"id": "ping", "label": "试一句验证"}])
                return st

        for kw in ("error", "failed", "invalid", "denied", "expired"):
            if kw in low:
                st.update(state="error", title="登录失败",
                          hint="展开下面的原始输出看看原因，或者重新点「登录 OpenCode」。")
                return st

        st.update(state="waiting", title="等待你的操作…",
                  hint="如果画面一直不动，点一下「发送回车」试试。",
                  actions=[{"id": "enter", "label": "发送回车"}])
        return st

    def login_urls(self) -> list:
        try:
            raw = open(self.login_log_path(), "rb").read().decode("utf-8", "ignore")
        except Exception:
            return []
        return self._find_urls(self._clean_pty(raw))

    def login_log_path(self) -> str:
        return os.path.join(self.data_dir, "opencode-login.log")

    def login_log(self, lines: int = 80) -> str:
        try:
            with open(self.login_log_path(), "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                raw = fh.read().decode("utf-8", "ignore")
            text = self._clean_pty(raw)
            return chr(10).join(text.splitlines()[-lines:])
        except FileNotFoundError:
            return "（还没有登录输出）"
        except Exception as exc:                    # noqa: BLE001
            return f"（读取失败：{exc}）"

    def start_login(self, provider: str = "opencode") -> tuple:
        """在真 PTY 里跑 `opencode auth login`：能看输出，也能给它按键。"""
        bin_path = self.resolve_bin()
        if not bin_path:
            return False, "没找到 opencode，先点「一键安装」"
        _, ws, env = self.cli_context()
        # NO_PROXY_LOGIN：登录走直连，避免代理抖动导致 Transport error
        for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                   'ALL_PROXY', 'all_proxy'):
            env.pop(_k, None)
        env['NO_PROXY'] = '*'
        log_path = self.login_log_path()
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("===== 开始登录 opencode =====" + chr(10))
        except Exception as exc:                    # noqa: BLE001
            return False, f"写日志失败：{exc}"

        cmd = [bin_path, "auth", "login"]
        if provider:
            cmd.append(provider)

        try:
            import pty
        except Exception:                           # Windows 没有 pty
            pty = None

        if pty is not None:
            try:
                master, slave = pty.openpty()
                try:
                    import fcntl as _fcntl
                    import struct as _struct
                    import termios as _termios
                    _ws = _struct.pack('HHHH', 45, 160, 0, 0)
                    _fcntl.ioctl(master, _termios.TIOCSWINSZ, _ws)
                    _fcntl.ioctl(slave, _termios.TIOCSWINSZ, _ws)
                except Exception:
                    pass
                proc = subprocess.Popen(cmd, stdin=slave, stdout=slave,
                                        stderr=slave, env=env, cwd=ws,
                                        start_new_session=True)
                os.close(slave)
                self._login = {"fd": master, "proc": proc}
                threading.Thread(target=self._pump_login, args=(master,),
                                 daemon=True).start()
                return True, "已启动登录（PTY）。看「登录输出」，按提示点「发送回车/输入」"
            except Exception as exc:                # noqa: BLE001
                return False, f"启动 PTY 失败：{exc}"

        # 没有 pty（Windows 开发机）：退化成普通后台进程
        try:
            with open(log_path, "ab") as lf:
                subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=env, cwd=ws,
                                 start_new_session=True)
            return True, "已启动登录（无 PTY，可能无法交互）"
        except Exception as exc:                    # noqa: BLE001
            return False, f"启动失败：{exc}"

    def _pump_login(self, master) -> None:
        """把 PTY 输出写进日志，直到进程结束。"""
        log_path = self.login_log_path()
        try:
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                with open(log_path, "ab") as fh:
                    fh.write(data)
        except Exception:
            pass
        finally:
            try:
                os.close(master)
            except Exception:
                pass
            st = getattr(self, "_login", None) or {}
            if st.get("fd") == master:
                self._login = {}

    def send_login(self, text: str) -> tuple:
        """往登录的 PTY 里写按键；如果进程已经结束就自动重开一次。"""
        st = getattr(self, "_login", None) or {}
        fd = st.get("fd")
        proc = st.get("proc")
        dead = (fd is None) or (proc is not None and proc.poll() is not None)
        if dead:
            ok, msg = self.start_login("")
            if not ok:
                return False, msg
            time.sleep(1.8)
            st = getattr(self, "_login", None) or {}
            fd = st.get("fd")
            if fd is None:
                return False, "登录进程没起来，看下面的原始输出"
        try:
            os.write(fd, text.encode("utf-8"))
            return True, "已发送：" + repr(text)
        except OSError as exc:
            self._login = {}
            return False, f"登录进程已结束（{exc}），已重置，请再点一次「登录 OpenCode」"
        except Exception as exc:                    # noqa: BLE001
            return False, f"发送失败：{exc}"

    def cli_context(self, mode: str = "chat") -> tuple:
        # 子进程用干净环境。网关自身的环境变量（PORT、代理、PYTHON* 等）
        # 会被 opencode 误用 —— 实测会表现为连 opencode.ai 时 ECONNREFUSED，
        # 而同样的命令在干净环境下手动跑完全正常。
        bin_path = self.resolve_bin()
        opts = self.get_opts() or {}
        base_ws = str(opts.get("workspace")
                      or os.path.join(self.data_dir, "opencode-workspace"))
        # 两套目录：chat 禁工具，agent 放工具。分开就不会互相覆盖配置。
        mode = "agent" if str(mode).lower() == "agent" else "chat"
        ws = os.path.join(base_ws, mode)
        home = os.path.join(self.data_dir, "home")
        for d in (ws, home, os.path.join(home, ".config"),
                  os.path.join(home, ".local", "share"),
                  os.path.join(self.data_dir, "npm-cache")):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        # 网关是「聊天转发」，不是「编码代理」。
        # 如果放任它用工具，模型会去 ls / git log 探索工作目录，一条「你好」
        # 也要跑几十条命令，最后卡到超时。这里三重把关：
        #   1. opencode.json 里把工具权限设成 deny
        #   2. tools 显式关闭
        #   3. AGENTS.md 里用自然语言再说一遍
        cfg_path = os.path.join(ws, "opencode.json")
        if mode == "agent":
            cfg_want = {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"edit": "allow", "bash": "allow",
                               "webfetch": "allow"},
            }
        else:
            cfg_want = {
                "$schema": "https://opencode.ai/config.json",
                "permission": {"edit": "deny", "bash": "deny",
                               "webfetch": "deny"},
                "tools": {"bash": False, "edit": False, "write": False,
                          "webfetch": False, "patch": False, "read": False},
            }
        try:
            old_cfg = {}
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path, encoding="utf-8") as fh:
                        old_cfg = json.load(fh) or {}
                except Exception:
                    old_cfg = {}
            if old_cfg != cfg_want:
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump(cfg_want, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

        agents_md = os.path.join(ws, "AGENTS.md")
        if mode == "agent":
            agents_text = (
                "你可以使用工具（bash / read / edit / write）来完成任务。\n"
                "先看清楚需求再动手；改文件前先读一遍；做完简要说明改了什么。\n"
                "当前目录就是你唯一的工作区，不要越界到上级目录。\n"
            )
        else:
            agents_text = (
                "你是通过 API 网关转发的纯文本对话助手。\n\n"
                "- 不要调用任何工具（bash / read / edit / write / webfetch / patch）。\n"
                "- 不要查看、搜索或修改工作目录里的任何文件。\n"
                "- 不要执行命令，也不要探索项目结构。\n"
                "- 直接根据用户消息用文字回答。\n"
            )
        try:
            cur = ""
            if os.path.exists(agents_md):
                with open(agents_md, encoding="utf-8") as fh:
                    cur = fh.read()
            if cur != agents_text:
                with open(agents_md, "w", encoding="utf-8") as fh:
                    fh.write(agents_text)
        except Exception:
            pass
        path_parts = []
        if bin_path:
            path_parts.append(os.path.dirname(bin_path))
        node = self.which("node")
        if node:
            path_parts.append(os.path.dirname(node))
        path_parts.extend(self.EXTRA_BIN_DIRS)
        env = {
            "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
            "XDG_CACHE_HOME": os.path.join(self.data_dir, "npm-cache"),
            "PATH": ":".join(path_parts),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "TMPDIR": "/tmp",
        }
        return bin_path, ws, env

    def own_logs(self, lines: int = 150) -> str:
        """opencode 自己写的日志（provider 报错都在这里）。"""
        dirs = [
            os.path.join(self.data_dir, "home", ".local", "share", "opencode", "log"),
            os.path.join(self.data_dir, "home", ".config", "opencode", "log"),
            os.path.expanduser("~/.local/share/opencode/log"),
        ]
        cands = []
        for d in dirs:
            try:
                if not os.path.isdir(d):
                    continue
                for fn in os.listdir(d):
                    if fn.endswith(".log"):
                        fp = os.path.join(d, fn)
                        cands.append((os.path.getmtime(fp), fp))
            except Exception:
                continue
        if not cands:
            return "（没找到 opencode 自己的日志，可能还没写过）"
        cands.sort(reverse=True)
        fp = cands[0][1]
        try:
            with open(fp, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 200 * 1024))
                text = fh.read().decode("utf-8", "ignore")
            return "[" + fp + "]" + chr(10) + chr(10).join(text.splitlines()[-lines:])
        except Exception as exc:                    # noqa: BLE001
            return f"（读取失败：{exc}）"

    def status(self) -> dict:
        opts = self.get_opts() or {}
        bin_path = self.resolve_bin()
        node = self.which("node")
        npm = self.which("npm")
        return {
            "installed": bool(bin_path),
            "bin": bin_path,
            "bin_source": self.bin_source(),
            "bin_version": self._version(bin_path) if bin_path else "",
            "private_bin": self.private_bin,
            "node": node,
            "node_version": self._version(node) if node else "",
            "npm": npm,
            "npm_version": self._version(npm) if npm else "",
            "system_node": bool(node),
            "running": self.is_running(),
            "pid": self.proc.pid if self.is_running() else None,
            "port": int(opts.get("port") or 4096),
            "port_open": self.port_open(),
            "workspace": str(opts.get("workspace") or ""),
            "autostart": bool(opts.get("autostart", True)),
            "oc_logs": self.own_logs(120),
            "login_log": self.login_log(60),
            "login_urls": self.login_urls(),
            "login_state": self.login_state(),
            "uptime": int(time.time() - self.started_at) if self.is_running() else 0,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "log": self.tail_log(40),
        }

    def tail_log(self, lines: int = 40) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                data = fh.read()[-20000:]
            text = data.decode("utf-8", "ignore")
            return "\n".join(text.splitlines()[-lines:])
        except Exception:
            return ""

    # ── 生命周期 ──
    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return True, "已经在运行"
            if self.port_open():
                return True, "端口已有服务在监听（可能是你手动起的），直接复用"

            bin_path = self.resolve_bin()
            if not bin_path:
                self.last_error = "没找到 opencode 可执行文件"
                return False, "没找到 opencode：先点「一键安装」，或在设置里填二进制路径"

            opts = self.get_opts() or {}
            port = str(int(opts.get("port") or 4096))
            ws = str(opts.get("workspace") or os.path.join(self.data_dir, "opencode-workspace"))
            try:
                os.makedirs(ws, exist_ok=True)
            except Exception as exc:                # noqa: BLE001
                return False, f"工作目录不可用：{exc}"

            # headless 场景最怕卡在"工具权限确认"：在 workspace 放一份放行配置
            cfg_path = os.path.join(ws, "opencode.json")
            if not os.path.exists(cfg_path):
                try:
                    with open(cfg_path, "w", encoding="utf-8") as fh:
                        json.dump({
                            "$schema": "https://opencode.ai/config.json",
                            "permission": {"edit": "allow", "bash": "allow",
                                           "webfetch": "allow"},
                        }, fh, ensure_ascii=False, indent=2)
                except Exception:
                    pass

            os.makedirs(self.data_dir, exist_ok=True)
            log = open(self.log_path, "ab")
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 opencode serve =====\n".encode())
            log.flush()

            # opencode 是个 node 脚本（shebang 走 env node），PATH 里必须能找到 node
            env = dict(os.environ)
            extra = []
            node = self.which("node")
            if node:
                extra.append(os.path.dirname(node))
            extra.extend(self.EXTRA_BIN_DIRS)
            extra.append(os.path.dirname(bin_path))
            env["PATH"] = ":".join(extra + [env.get("PATH", "")])

            # opencode 会往 HOME 写配置和认证，指到应用数据目录（那里可写）
            home = os.path.join(self.data_dir, "home")
            for d in (home,
                      os.path.join(home, ".config"),
                      os.path.join(home, ".local", "share"),
                      os.path.join(self.data_dir, "npm-cache")):
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception:
                    pass
            env["HOME"] = home
            env["XDG_CONFIG_HOME"] = os.path.join(home, ".config")
            env["XDG_DATA_HOME"] = os.path.join(home, ".local", "share")
            env["XDG_CACHE_HOME"] = os.path.join(self.data_dir, "npm-cache")

            try:
                self.proc = subprocess.Popen(
                    [bin_path, "serve", "--port", port, "--hostname", "127.0.0.1"],
                    cwd=ws, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True, env=env)
            except Exception as exc:                # noqa: BLE001
                self.last_error = f"启动失败：{exc}"
                return False, self.last_error
            finally:
                try:
                    log.close()
                except Exception:
                    pass

            self.started_at = time.time()
            # 等端口起来（最多 ~8 秒）
            for _ in range(40):
                time.sleep(0.2)
                if self.port_open():
                    self.last_error = ""
                    return True, f"已启动（pid {self.proc.pid}）"
                if self.proc.poll() is not None:
                    break
            self.last_error = "启动后端口没起来，看日志"
            return False, self.last_error

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            if not self.is_running():
                return True, "本来就没在运行"
            try:
                self.proc.terminate()
                for _ in range(25):
                    time.sleep(0.2)
                    if self.proc.poll() is not None:
                        return True, "已停止"
                self.proc.kill()
                return True, "已强制停止"
            except Exception as exc:                # noqa: BLE001
                return False, f"停止失败：{exc}"
            finally:
                self.proc = None

    # ── 一键安装 ──
    def install(self) -> tuple[bool, str]:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "install-opencode.sh")
        if not os.path.exists(script):
            return False, f"找不到安装脚本：{script}"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.log_path, "ab") as log:
                log.write("\n===== 开始安装 Node + opencode =====\n".encode("utf-8"))
                log.flush()
                rc = subprocess.call(["bash", script, self.data_dir],
                                     stdout=log, stderr=subprocess.STDOUT)
            if rc != 0:
                return False, f"安装脚本退出码 {rc}，看日志"
            found = ""
            for n in self.BIN_NAMES:
                p = os.path.join(self.data_dir, "npm", "bin", n)
                if os.path.exists(p):
                    found = p
                    break
            if not found:
                return False, "安装完成但没找到 opencode2/opencode，看日志"
            ver = self._version(found)
            self._ver_cache.pop(found + "-v", None)
            ver = self._version(found)
            return True, (f"安装完成：{os.path.basename(found)} {ver}".strip()
                          + "　请点「停止」再「启动」，让服务用上新版本")
        except Exception as exc:                    # noqa: BLE001
            return False, f"安装失败：{exc}"

    # ── 看门狗 ──
    def ensure(self) -> None:
        """请求前调用：没在跑就试着拉起来（失败不影响主链路）。"""
        opts = self.get_opts() or {}
        if not opts.get("enabled"):
            return
        if self.is_running() or self.port_open():
            return
        try:
            self.start()
        except Exception:
            pass

    def watch(self) -> None:
        """后台线程：崩溃自动重启，带退避。"""
        while not self._stop_watch:
            time.sleep(10)
            try:
                opts = self.get_opts() or {}
                if not opts.get("enabled") or not opts.get("autostart", True):
                    continue
                if not self.resolve_bin():
                    continue
                if self.is_running() or self.port_open():
                    if self.is_running():
                        self.restarts = 0
                    continue
                if self.restarts >= 5:
                    self.last_error = "连续重启失败 5 次，已暂停自动拉起"
                    continue
                self.restarts += 1
                time.sleep(min(self.restarts * 2, 20))
                self.start()
            except Exception:
                continue

    def shutdown(self) -> None:
        self._stop_watch = True
        self.stop()
