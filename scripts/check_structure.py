#!/usr/bin/env python3
"""打包前结构自检（原生应用版，纯标准库）

按飞牛 fpk 规范校验，问题在 CI 阶段就暴露：
  - 根目录 manifest / ICON.PNG / ICON_256.PNG / app/ / cmd/ / wizard/
  - config/privilege、config/resource 为合法 JSON
  - app/{desktop_uidir}/ 存在
  - manifest 必填字段齐全，desktop_applaunchname 与 ui 入口一致
  - 入口 port 与 manifest.service_port 一致
  - 图标尺寸 64 / 256
  - 网关脚本可编译、providers.json 结构正确
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                  # noqa: BLE001
        pass

FAILS: list[str] = []


def check(cond: bool, msg: str, detail: str = "") -> None:
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg} {detail}")


def read_manifest() -> dict:
    mf: dict[str, str] = {}
    with open("manifest", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            mf[k.strip()] = v.strip()
    return mf


def main() -> int:
    print("== 1. 必需路径 ==")
    for p in ["manifest", "ICON.PNG", "ICON_256.PNG", "app", "cmd",
              "wizard", "config/privilege", "config/resource",
              "app/gateway.py", "app/service.sh", "app/providers.json",
              "app/opencode_upstream.py", "app/opencode-serve.sh",
              "app/opencode_supervisor.py", "app/install-opencode.sh",
              "app/proxy_support.py"]:
        check(os.path.exists(p), p)

    print("== 2. manifest 字段 ==")
    mf = read_manifest()
    for k in ["appname", "version", "display_name", "desc", "source",
              "platform", "desktop_uidir", "desktop_applaunchname", "service_port"]:
        check(bool(mf.get(k)), k, "缺失")
    check(mf.get("source") == "thirdparty", "source=thirdparty", mf.get("source", ""))
    check(mf.get("platform") in ("x86", "arm", "all"), "platform 合法", mf.get("platform", ""))

    print("== 3. JSON 配置 ==")
    for f in ["config/privilege", "config/resource", "app/ui/config",
              "wizard/install", "wizard/config", "app/providers.json"]:
        try:
            json.load(open(f, encoding="utf-8"))
            check(True, f)
        except Exception as exc:                       # noqa: BLE001
            check(False, f, str(exc))

    print("== 3b. 安装向导里不应有输入项 ==")
    INPUT_TYPES = {"password", "text", "select", "switch", "radio", "checkbox"}
    for f in ["wizard/install", "wizard/config"]:
        try:
            steps = json.load(open(f, encoding="utf-8"))
            types = [str(it.get("type")) for st in steps
                     for it in (st.get("items") or [])]
            bad = [t for t in types if t in INPUT_TYPES]
            check(not bad, f"{f} 只有说明文字（配置都在面板里）", str(bad))
        except Exception as exc:                       # noqa: BLE001
            check(False, f"{f} 解析", str(exc))

    print("== 4. 上游配置结构 ==")
    check(os.path.exists("app/providers.d/README.md"), "app/providers.d/README.md（新增上游的模板）")
    try:
        cfg = json.load(open("app/providers.json", encoding="utf-8"))
        provs = cfg.get("providers") or []
        check(len(provs) > 0, "至少一个上游", str(len(provs)))
        bad = [p.get("name") for p in provs
               if not all(k in p for k in ("name", "base_url", "model"))]
        check(not bad, "每个上游都有 name/base_url/model", str(bad))
        names = [p["name"] for p in provs]
        check(len(names) == len(set(names)), "上游 name 不重复")
        check(all(not str(p["base_url"]).endswith("/") for p in provs),
              "base_url 不带结尾斜杠（避免拼出 //chat/completions）")

        # 免费优先：默认配置里不允许出现「启用的付费上游」
        no_flag = [p["name"] for p in provs if "free_only" not in p]
        check(not no_flag, "每个上游都显式声明 free_only", str(no_flag))
        paid_on = [p["name"] for p in provs
                   if p.get("enabled") and not p.get("free_only")]
        check(not paid_on, "默认没有启用付费上游（免费优先）", str(paid_on))
        free_on = [p["name"] for p in provs
                   if p.get("enabled") and p.get("free_only")]
        check(bool(free_on), "至少启用一个免费上游", str(free_on))
        bad_quota = [p["name"] for p in provs
                     if p.get("free_only") and not isinstance(p.get("free_quota_tokens"), int)]
        check(not bad_quota, "免费上游都声明了 free_quota_tokens", str(bad_quota))

        bad_type = [p["name"] for p in provs
                    if str(p.get("type", "http")).lower() not in ("http", "opencode")]
        check(not bad_type, "type 只能是 http / opencode", str(bad_type))
        oc = [p for p in provs if str(p.get("type", "")).lower() == "opencode"]
        check(all(p.get("model") for p in oc), "opencode 上游必须声明 model", str([p["name"] for p in oc]))
    except Exception as exc:                           # noqa: BLE001
        check(False, "providers.json 解析", str(exc))

    print("== 5. 入口与端口一致性 ==")
    uidir = mf.get("desktop_uidir", "ui")
    check(os.path.isdir(os.path.join("app", uidir)), f"app/{uidir}/ 存在")
    try:
        ui = json.load(open(os.path.join("app", uidir, "config"), encoding="utf-8"))
        urls = ui.get(".url", {})
        entry = mf.get("desktop_applaunchname", "")
        check(entry in urls, "desktop_applaunchname 指向已定义入口", entry)
        if entry in urls:
            e = urls[entry]
            if e.get("gatewayPrefix"):
                check(True, "主入口走统一网关（SSO，不需要 port）")
                check(str(e.get("gatewaySocket", "")).endswith(".sock"),
                      "gatewaySocket 声明了 socket 文件名", str(e.get("gatewaySocket")))
            else:
                check(e.get("port") == mf.get("service_port"),
                      "入口 port == manifest.service_port",
                      f"{e.get('port')} vs {mf.get('service_port')}")
            icon = e.get("icon", "")
            for size in ("64", "256"):
                base = os.path.join("app", uidir, icon.replace("{0}", size))
                check(os.path.exists(base), f"入口图标 icon_{size} 存在", base)

        # 必须保留一个端口直连入口：统一网关出问题时还能进得去
        direct = [k for k, v in urls.items() if v.get("port")]
        check(bool(direct), "保留了端口直连入口（网关不可用时的兜底）", str(direct))
        for k in direct:
            check(urls[k].get("port") == mf.get("service_port"),
                  f"{k} 的 port 与 manifest 一致", str(urls[k].get("port")))
    except Exception as exc:                           # noqa: BLE001
        check(False, "读取 app/ui/config", str(exc))

    print("== 6. 生命周期脚本 ==")
    for s in ["main", "install_init", "install_callback", "upgrade_init",
              "upgrade_callback", "uninstall_init", "uninstall_callback",
              "config_init", "config_callback"]:
        check(os.path.exists(os.path.join("cmd", s)), f"cmd/{s}")

    print("== 7. 网关脚本可编译 ==")
    try:
        src = open("app/gateway.py", encoding="utf-8").read()
        compile(src, "app/gateway.py", "exec")
        check(True, "app/gateway.py 语法正确")
    except Exception as exc:                           # noqa: BLE001
        check(False, "app/gateway.py 语法", str(exc))

    print("== 7b. 页面内联 JS 语法（node --check）==")
    node = shutil.which("node")
    if not node:
        print("  SKIP 本机没有 node，跳过（CI 上有）")
    else:
        try:
            sys.path.insert(0, os.path.join(os.getcwd(), "app"))
            import importlib.util
            spec = importlib.util.spec_from_file_location("gw_js_check", "app/gateway.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for name in ("STATUS_HTML", "SETTINGS_HTML"):
                html = getattr(mod, name, "")
                parts = html.split("<script>")
                js = parts[1].split("</script>")[0] if len(parts) > 1 else ""
                tmp_js = os.path.join(os.getcwd(), f".jscheck_{name}.js")
                with open(tmp_js, "w", encoding="utf-8") as fh:
                    fh.write(js)
                out_path = os.path.join(os.getcwd(), f".jscheck_{name}.out")
                with open(out_path, "w", encoding="utf-8") as ofh:
                    rc_code = subprocess.call([node, "--check", tmp_js],
                                              stdout=ofh, stderr=subprocess.STDOUT)
                detail = ""
                if rc_code != 0:
                    try:
                        txt = open(out_path, encoding="utf-8", errors="replace").read()
                        lines = [ln for ln in txt.strip().splitlines() if ln.strip()]
                        detail = lines[-1] if lines else "语法错误"
                    except Exception:
                        detail = "语法错误"
                for f2 in (tmp_js, out_path):
                    try:
                        os.unlink(f2)
                    except Exception:
                        pass
                rc = type("R", (), {"returncode": rc_code})()
                check(rc.returncode == 0, f"{name} 内联 JS 语法正确", detail)
        except Exception as exc:                       # noqa: BLE001
            check(False, "页面 JS 语法检查", str(exc))

    print("== 8. 图标尺寸 ==")
    try:
        from PIL import Image
        for f, want in [("ICON.PNG", 64), ("ICON_256.PNG", 256)]:
            im = Image.open(f)
            check(im.size == (want, want), f"{f} = {want}x{want}", str(im.size))
    except ImportError:
        print("  SKIP 未安装 Pillow，跳过尺寸校验（fnpack 会自己校验）")

    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项不通过：")
        for m in FAILS:
            print("  -", m)
        return 1
    print(f"全部通过 - appname={mf.get('appname')} version={mf.get('version')} "
          f"port={mf.get('service_port')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
