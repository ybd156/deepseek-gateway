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
              "app/gateway.py", "app/service.sh", "app/providers.json"]:
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

    print("== 4. 上游配置结构 ==")
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
    except Exception as exc:                           # noqa: BLE001
        check(False, "providers.json 解析", str(exc))

    print("== 5. 入口与端口一致性 ==")
    uidir = mf.get("desktop_uidir", "ui")
    check(os.path.isdir(os.path.join("app", uidir)), f"app/{uidir}/ 存在")
    try:
        ui = json.load(open(os.path.join("app", uidir, "config"), encoding="utf-8"))
        entry = mf.get("desktop_applaunchname", "")
        check(entry in ui.get(".url", {}), "desktop_applaunchname 指向已定义入口", entry)
        if entry in ui.get(".url", {}):
            check(ui[".url"][entry].get("port") == mf.get("service_port"),
                  "入口 port == manifest.service_port",
                  f"{ui['.url'][entry].get('port')} vs {mf.get('service_port')}")
            icon = ui[".url"][entry].get("icon", "")
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
