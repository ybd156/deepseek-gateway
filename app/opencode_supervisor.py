"""OpenCode 运行时托管：把 `opencode serve` 当成网关的子进程来管。

能力：
  - 查找可用的 opencode 可执行文件（配置路径 → 应用私有安装 → PATH）
  - 启动 / 停止 / 探活 / 拉日志
  - 看门狗：意外退出后按退避自动重启
  - 一键安装：把 Node + opencode 装进应用私有目录（不污染系统）

只用标准库。
"""
from __future__ import annotations

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
        return os.path.join(self.data_dir, "npm", "bin", "opencode")

    def resolve_bin(self) -> str:
        opts = self.get_opts() or {}
        cand = str(opts.get("bin") or "").strip()
        if cand and os.path.exists(cand):
            return cand
        if os.path.exists(self.private_bin):
            return self.private_bin
        return self.which("opencode")

    def bin_source(self) -> str:
        opts = self.get_opts() or {}
        cand = str(opts.get("bin") or "").strip()
        if cand and os.path.exists(cand):
            return "手动指定"
        if os.path.exists(self.private_bin):
            return "应用内安装"
        if self.which("opencode"):
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

    def status(self) -> dict:
        opts = self.get_opts() or {}
        bin_path = self.resolve_bin()
        node = self.which("node")
        npm = self.which("npm")
        return {
            "installed": bool(bin_path),
            "bin": bin_path,
            "bin_source": self.bin_source(),
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
            if not os.path.exists(self.private_bin):
                return False, "安装完成但没找到 opencode，看日志"
            return True, "安装完成"
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
