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
              "app/opencode_upstream.py",
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

    print("== 7b. 前端文件与语法 ==")
    try:
        import subprocess as _sp
        import tempfile as _tf
        web = os.path.join("app", "web")
        for name in ("admin.html", "app.css", "app.js"):
            check(os.path.exists(os.path.join(web, name)), f"存在 web/{name}")
        html = open(os.path.join(web, "admin.html"), encoding="utf-8").read()
        check("static/app.css" in html and "static/app.js" in html,
              "页面引用独立静态文件")
        js = open(os.path.join(web, "app.js"), encoding="utf-8").read()
        with _tf.NamedTemporaryFile("w", suffix=".js", delete=False,
                                    encoding="utf-8") as fh:
            fh.write(js)
            tmp = fh.name
        try:
            with open(os.devnull, "w") as devnull:
                rc = _sp.call(["node", "--check", tmp], stdout=devnull,
                              stderr=devnull)
            check(rc == 0, "app.js 语法正确（node --check）")
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        check("__pycache__" not in js, "app.js 里没有异常内容")
    except Exception as exc:                           # noqa: BLE001
        check(False, "前端检查", str(exc))

    print("== 7c. 设置页收集范围（防误收集非上游卡片）==")
    try:
        js = open(os.path.join("app", "web", "app.js"), encoding="utf-8").read()
        check("querySelectorAll('.provcard')" in js,
              "上游卡片用专属类 .provcard 收集")
        check("provcard" in js and "readCard(" in js,
              "有独立的 readCard 读取函数")
        check("名称不能为空" in js or "名称为空" in js,
              "保存前有名称自检")
    except Exception as exc:                           # noqa: BLE001
        check(False, "收集范围检查", str(exc))

    print("== 7d. 换行符（Linux 上 CRLF 会让 shell 脚本直接崩）==")
    try:
        import os as _os
        bad = []
        for _root, _dirs, _files in _os.walk("."):
            _dirs[:] = [d for d in _dirs
                        if d not in (".git", "__pycache__", ".testtmp", "dist")]
            for _fn in _files:
                if _fn.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".fpk", ".zip")):
                    continue
                _p = _os.path.join(_root, _fn)
                try:
                    if bytes([13, 10]) in open(_p, "rb").read():
                        bad.append(_p)
                except Exception:
                    pass
        check(not bad, "文本文件都是 LF 换行", ", ".join(bad[:5]))
    except Exception as exc:                           # noqa: BLE001
        check(False, "换行符检查", str(exc))

    print("== 7e. 桌面图标只保留一个 ==")
    try:
        import json as _json
        ui = _json.load(open(os.path.join("app", "ui", "config"), encoding="utf-8"))
        entries = list((ui.get(".url") or {}).keys())
        check(len(entries) == 1, "只注册一个桌面入口", str(entries))
        main = (ui.get(".url") or {}).get("deepseek-gateway.main") or {}
        check(bool(main.get("gatewayPrefix")), "主入口走统一网关")
        check("direct" not in " ".join(entries), "没有重复的直连入口", str(entries))
    except Exception as exc:                           # noqa: BLE001
        check(False, "桌面入口检查", str(exc))

    print("== 7f. 应用信息（开发者 / 发布者）==")
    try:
        mf2 = {}
        for line in open("manifest", encoding="utf-8").read().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                mf2[k.strip()] = v.strip()
        dev = mf2.get("maintainer", "")
        dev_url = mf2.get("maintainer_url", "")
        pub = mf2.get("distributor", "")
        pub_url = mf2.get("distributor_url", "")
        check(bool(dev) and not dev.startswith("http"),
              "开发者是名称而不是网址", dev)
        check(dev_url.startswith("http"), "开发者带可点击链接", dev_url)
        check(bool(pub) and not pub.startswith("http"),
              "发布者是名称而不是网址", pub)
        check(pub_url.startswith("http"), "发布者带可点击链接", pub_url)
    except Exception as exc:                           # noqa: BLE001
        check(False, "应用信息检查", str(exc))

    print("== 7g. 记账口径（三种路径都要记客户端点名）==")
    try:
        gw = open(os.path.join("app", "gateway.py"), encoding="utf-8").read()
        check('payload.get("_echo_model") or p["model"]' in gw,
              "非流式按客户端点名记账")
        check("record(p[\"name\"], echo or p[\"model\"]" in gw,
              "兜底流式按客户端点名记账")
        check(gw.count('"upstream_model"') >= 3, "三条路径都记上游实际模型",
              str(gw.count('"upstream_model"')))
        check('"source": "probe"' in gw, "自检有单独标记")
    except Exception as exc:                           # noqa: BLE001
        check(False, "记账口径检查", str(exc))

    print("== 7h. opencode 当聊天用（禁止跑工具）==")
    try:
        sup = open(os.path.join("app", "opencode_supervisor.py"), encoding="utf-8").read()
        check('"edit": "deny"' in sup and '"bash": "deny"' in sup,
              "配置里把工具权限设为 deny")
        check('"tools"' in sup and '"bash": False' in sup,
              "tools 显式关闭")
        check("AGENTS.md" in sup, "写 AGENTS.md 约束模型")
        gw = open(os.path.join("app", "gateway.py"), encoding="utf-8").read()
        check("spawning process" in gw, "超时能识别「在跑工具」")
    except Exception as exc:                           # noqa: BLE001
        check(False, "opencode 聊天化检查", str(exc))

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
