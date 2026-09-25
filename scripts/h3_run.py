#!/usr/bin/env python3
"""
h3_run.py — RunningHub H3 工作流一体化 runner（NDJSON 事件流，供 Agent/CI 以子进程方式消费）。

零第三方依赖，纯 Python 标准库（urllib），不调用外部命令（系统通知除外，且经 argv 传参）。

模式：
  submit  提交工作流任务（先上传 --file 素材，再 create），随后默认进入轮询
  watch   对已存在的 taskId 轮询（补挂/断线重连用）

事件契约（stdout 每行一个 JSON 对象，永远以 done / failed / error 三者之一结尾）：
  {"ts":..., "event":"uploaded", "node":"27", "fileName":"api/xx.mp4"}
  {"ts":..., "event":"submitted", "taskId":"..."}
  {"ts":..., "event":"progress", "status":"RUNNING", "elapsed":83.2}
  {"ts":..., "event":"done", "file":"/abs/out.mp4", "costCoins":48, "costMoney":null, "gpuSeconds":237}
  {"ts":..., "event":"failed", "node":"27", "node_name":"VHS_LoadVideo", "exception":"...", "taskId":"..."}
  {"ts":..., "event":"error", "reason":"SUBMIT_FAILED", "detail":{...}}
  {"ts":..., "event":"param_confirm", "project":"...", "unconfirmed":[...]}  ← 画面比例/尺寸待核对（Agent 应转述给用户，勿擅自确认）
  {"ts":..., "event":"param_mismatch", "diff":[...]}                         ← 与项目档案不一致的告警（可忽略或改回）

密钥与站点：env RUNNINGHUB_API_KEY / RUNNINGHUB_HOST 优先，其次 ~/.config/runninghub/config.json
（{"apiKey": "...", "host": "..."}，权限建议 600）。密钥不落日志。
本脚本完全自包含：仅依赖 Python 标准库，不引用任何外部项目代码或配置。

安全约定：
  - 仅允许 http/https；
  - 每次请求前对目标 host 做 DNS 解析，解析结果必须全部为全球可路由地址
    （拒绝环回/内网/链路本地/保留/组播等），以抵御 SSRF 与 DNS rebinding 的常见形态；
  - 禁用自动重定向（重定向会导致二次请求绕过上述校验）。
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

POLL_INTERVAL_DEFAULT = 5
MAX_SECONDS_DEFAULT = 1200

# 任务注册表（可选）：面板与 CLI 共用的任务登记文件；为 None 时不写注册表。
# 面板 server 启动时会把它指向 ~/.runninghub-panel/tasks.json。
REGISTRY_PATH = None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 密钥配置：环境变量优先，其次本 skill 专属配置文件（不依赖任何外部项目）
_CONFIG_PATH = Path.home() / ".config" / "runninghub" / "config.json"


def load_cfg() -> tuple[str, str]:
    host = os.environ.get("RUNNINGHUB_HOST", "").strip().rstrip("/")
    key = os.environ.get("RUNNINGHUB_API_KEY", "").strip()
    try:
        entry = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        entry = {}
    if not host:
        host = str(entry.get("host", "")).strip().rstrip("/")
    if not key:
        k = entry.get("apiKey")
        if isinstance(k, str) and k.strip():
            key = k.strip()
    if not host:
        host = "https://www.runninghub.ai"
    if not key:
        emit("error", reason="NO_API_KEY",
             detail='未找到密钥：设置 RUNNINGHUB_API_KEY，或写入 ~/.config/runninghub/config.json'
                    '（内容 {"apiKey": "...", "host": "..."}，权限 600）')
        sys.exit(1)
    return host, key


def registry_update(tid: str, patch: dict) -> None:
    """把任务状态写入共享注册表（存在同 taskId 条目则合并，否则新建）。
    双入口对等：面板按钮与 Agent CLI 各自带 source，记录同一张表。"""
    if not REGISTRY_PATH:
        return
    try:
        items = json.loads(Path(REGISTRY_PATH).read_text(encoding="utf-8"))
    except Exception:
        items = []
    patch = dict(patch)
    entry = next((it for it in items if it.get("taskId") == tid), None)
    if entry is None:
        entry = {"taskId": tid, "source": patch.pop("source", "agent"),
                 "time": patch.get("time") or int(time.time() * 1000)}
        items.insert(0, entry)
    entry.update(patch)
    try:
        Path(REGISTRY_PATH).write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print("[registry] 写入失败: " + str(e)[:120], file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 项目参数档案：画面比例/分辨率等 sticky 参数，一次核对、整项目沿用
# ---------------------------------------------------------------------------

_PROJECTS_DIR = Path.home() / ".runninghub" / "projects"

# 按 fieldName 识别（跨工作流通用）：一个项目内几乎总是保持一致、值得提交前核对。
STICKY_FIELDS = {"aspect_ratio", "megapixels", "resolution", "ratio",
                 "width", "height", "video_width", "video_height"}


def _project_slug(project: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in project.strip())
    return keep.strip("_") or "default"


def profile_path(project: str) -> Path:
    return _PROJECTS_DIR / (_project_slug(project) + ".json")


def load_profile(project: str) -> dict:
    try:
        data = json.loads(profile_path(project).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profile(project: str, data: dict) -> None:
    _PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    profile_path(project).write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# 事件输出（NDJSON）
# ---------------------------------------------------------------------------

def emit(event: str, **kw) -> None:
    line = {"ts": round(time.time(), 3), "event": event, **kw}
    print(json.dumps(line, ensure_ascii=False), flush=True)


def sys_notify(title: str, message: str) -> None:
    """跨平台完成通知。文本一律作为「数据」传入（argv / EncodedCommand），
    不拼接进可执行语句，规避注入。失败兜底：终端响铃。"""
    try:
        if sys.platform == "darwin":
            # macOS：标题/正文经 argv 传入 AppleScript（item 1 / item 2）
            subprocess.run(
                ["osascript",
                 "-e", "on run argv",
                 "-e", 'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"',
                 "-e", "end run",
                 title, message],
                capture_output=True, timeout=10)
            return
        if sys.platform == "win32":
            # Windows 10/11：PowerShell 气泡通知（无需第三方模块）。
            # 脚本整体 base64(UTF-16LE) 后走 -EncodedCommand，绕开所有引号/转义问题。
            def ps_quote(s: str) -> str:
                return s.replace("&", "&amp;").replace("'", "''")

            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$n = New-Object System.Windows.Forms.NotifyIcon;"
                  "$n.Icon = [System.Drawing.SystemIcons]::Information;"
                  "$n.Visible = $true;"
                  "$n.ShowBalloonTip(8000, '" + ps_quote(title) + "', '"
                  + ps_quote(message) + "', 'Info');"
                  "Start-Sleep -Seconds 9;")
            encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
            subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                           capture_output=True, timeout=20)
            return
        if sys.platform.startswith("linux"):
            r = subprocess.run(["notify-send", title, message],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return
    except Exception:
        pass
    sys.stderr.write("\a")  # 兜底：终端响铃


def reveal_file(path: str) -> None:
    """在系统文件管理器中显示文件：macOS=Finder，Windows=资源管理器，Linux=xdg-open。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", path], capture_output=True, timeout=10)
        elif sys.platform == "win32":
            subprocess.run(["explorer", "/select,", path], capture_output=True, timeout=10)
        else:
            subprocess.run(["xdg-open", str(Path(path).parent)],
                           capture_output=True, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP（纯 urllib；URL 校验 + 禁重定向）
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 一切重定向都按错误处理，防止绕过 URL 校验


_OPENER = urllib.request.build_opener(_NoRedirect)


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError("URL 协议仅允许 http/https，收到: " + repr(parsed.scheme))
    host = parsed.hostname
    if not host:
        raise RuntimeError("URL 缺少主机名: " + url[:200])
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise RuntimeError("DNS 解析失败 " + host + ": " + str(e)) from e
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_global:
            continue
        # 198.18.0.0/15（RFC2544 基准段）是本机代理 fake-ip DNS 的惯用伪 IP 段，
        # 非内网服务；环回/内网/链路本地（含云元数据 169.254.169.254）仍然拒绝。
        if ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15"):
            continue
        raise RuntimeError("拒绝非公网目标地址 " + str(ip) + " (host=" + host + ")")


def _request(url: str, *, data: bytes | None, headers: dict[str, str],
             timeout: int, context: str) -> bytes:
    # 结果 URL 可能含未转义的非 ASCII 字符（中文文件名），统一按 RFC3986 编码；
    # safe 里保留 URL 结构字符与已编码的 %，避免二次编码破坏原有转义。
    url = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%")
    _validate_url(url)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError(context + " HTTP " + str(e.code) + " " + body) from e
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(context + " " + str(e)[:200]) from e


def post_json(url: str, payload: dict, bearer: str | None = None,
              timeout: int = 60) -> dict:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    raw = _request(url, data=json.dumps(payload).encode("utf-8"),
                   headers=headers, timeout=timeout, context="POST " + url)
    return json.loads(raw)


def upload_media(host: str, key: str, path: str) -> str:
    """v2 媒体上传：Bearer 头 + 仅 file 字段，返回 fileName（1 天有效）。"""
    p = Path(path)
    if not p.exists():
        emit("error", reason="FILE_NOT_FOUND", detail=path)
        sys.exit(1)
    boundary = uuid.uuid4().hex
    filename = p.name.replace('"', "_")

    def file_field(name: str, fname: str, content: bytes) -> bytes:
        head = ("--" + boundary + "\r\n"
                + 'Content-Disposition: form-data; name="' + name + '"; filename="' + fname + '"\r\n'
                + "Content-Type: application/octet-stream\r\n\r\n")
        return head.encode("utf-8") + content + b"\r\n"

    body = (file_field("file", filename, p.read_bytes())
            + ("--" + boundary + "--\r\n").encode("utf-8"))
    raw = _request(host + "/openapi/v2/media/upload/binary", data=body,
                   headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                            "Authorization": "Bearer " + key},
                   timeout=300, context="upload " + filename)
    resp = json.loads(raw)
    if resp.get("code") not in (0, 200):
        emit("error", reason="UPLOAD_FAILED", detail=resp)
        sys.exit(1)
    data = resp.get("data") or {}
    return data.get("fileName") or data.get("download_url") or ""


def download_to(url: str, out: str) -> None:
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    raw = _request(url, data=None, headers={}, timeout=300, context="download")
    Path(out).write_bytes(raw)


def get_json(url: str, context: str = "GET", timeout: int = 30) -> dict:
    raw = _request(url, data=None, headers={"Accept": "application/json"},
                   timeout=timeout, context=context)
    return json.loads(raw)


def cmd_extract(host: str, key: str, webapp_id: str, out_path: str | None) -> dict:
    """从 AI 应用（webappId）拉取参数白名单，生成面板 workflow.json 草稿。"""
    url = (host + "/api/webapp/apiCallDemo?apiKey=" + key
           + "&webappId=" + str(webapp_id))
    resp = get_json(url, context="GET apiCallDemo")
    if resp.get("code") not in (0, 200):
        emit("error", reason="EXTRACT_FAILED", detail=resp.get("msg", resp))
        sys.exit(1)
    entries: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if "fieldName" in o and "fieldType" in o:
                entries.append(o)
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(resp.get("data") or resp)

    params: dict = {}
    media: dict = {"video": [], "image": [], "audio": []}
    for e in entries:
        nid, fname = str(e.get("nodeId")), str(e.get("fieldName"))
        ftype = str(e.get("fieldType", "")).upper()
        desc = e.get("description") or fname
        val = e.get("fieldValue")
        base = {"node": nid, "field": fname}
        if ftype == "IMAGE":
            media["image"].append({**base, "label": desc, "accept": "image/*"})
        elif ftype == "AUDIO":
            media["audio"].append({**base, "label": desc, "accept": "audio/*"})
        elif ftype == "VIDEO":
            media["video"].append({**base, "label": desc,
                                   "accept": "video/mp4,video/quicktime"})
        elif ftype in ("FLOAT", "INT"):
            params[nid + "_" + fname] = {**base, "label": desc, "type": "number",
                                         "default": val}
        elif ftype == "LIST":
            options = []
            fd = e.get("fieldData")
            if isinstance(fd, str):
                try:
                    fd = json.loads(fd)
                except json.JSONDecodeError:
                    fd = None
            if isinstance(fd, dict):
                options = fd.get("options", [])
            elif isinstance(fd, list) and fd and isinstance(fd[-1], dict):
                options = fd[-1].get("options", [])   # 形如 ["COMBO", {...配置...}]
            params[nid + "_" + fname] = {**base, "label": desc, "type": "select",
                                         "default": val,
                                         "options": options or [val]}
        else:
            params[nid + "_" + fname] = {**base, "label": desc, "type": "textarea",
                                         "default": val if isinstance(val, str) else ""}
    draft = {"target": "aiapp", "webappId": str(webapp_id),
             "workflowId": str(webapp_id),
             "name": "AI 应用 " + str(webapp_id),
             "params": params, "media": media}
    text = json.dumps(draft, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        emit("extracted", out=str(Path(out_path).resolve()), entries=len(entries))
    else:
        print(text)
    return draft


# ---------------------------------------------------------------------------
# 提交 / 轮询 / 下载
# ---------------------------------------------------------------------------

def media_field(cfg: dict, nid: str) -> str | None:
    """从 extract 配置查某媒体节点的真实字段名（video/image/audio…）。"""
    for group in (cfg.get("media") or {}).values():
        for m in group:
            if str(m.get("node")) == str(nid):
                return m.get("field")
    return None


def parse_kv(arg: str) -> tuple[str, str, str]:
    """'nodeId:fieldName=value' → (nodeId, fieldName, value)。value 内可含 = 与换行。"""
    c = arg.find(":")
    if c == -1:
        emit("error", reason="BAD_ARG", detail=arg)
        sys.exit(1)
    nid, rest = arg[:c], arg[c + 1:]
    e = rest.find("=")
    if e == -1:
        emit("error", reason="BAD_ARG", detail=arg)
        sys.exit(1)
    return nid, rest[:e], rest[e + 1:]


def submit(host: str, key: str, app_id: str, nodes: list[dict],
           target: str = "workflow") -> str:
    """target=workflow: /task/openapi/create（apiKey 在 body，code 包装响应）
       target=aiapp:   /openapi/v2/run/ai-app/<id>（Bearer，响应为任务对象本身）"""
    if target == "aiapp":
        resp = post_json(host + "/openapi/v2/run/ai-app/" + str(app_id),
                         {"nodeInfoList": nodes}, bearer=key)
        task_id = resp.get("taskId") or (resp.get("data") or {}).get("taskId")
        if not task_id:
            emit("error", reason="SUBMIT_FAILED",
                 detail=resp.get("errorMessage") or resp)
            sys.exit(1)
        tips = resp.get("promptTips")
        if tips:
            try:
                node_errors = json.loads(tips).get("node_errors", {}) \
                    if isinstance(tips, str) else tips.get("node_errors", {})
                if node_errors:
                    emit("error", reason="NODE_ERRORS", node_errors=node_errors)
                    sys.exit(1)
            except (json.JSONDecodeError, TypeError):
                pass
        return str(task_id)

    resp = post_json(host + "/task/openapi/create",
                     {"apiKey": key, "workflowId": str(app_id),
                      "nodeInfoList": nodes})
    if resp.get("code") not in (0, 200):
        emit("error", reason="SUBMIT_FAILED", detail=resp.get("msg", resp))
        sys.exit(1)
    data = resp.get("data", {})
    task_id = data.get("taskId")
    if not task_id:
        emit("error", reason="SUBMIT_FAILED", detail="no taskId in response")
        sys.exit(1)
    tips = data.get("promptTips")
    if tips:
        try:
            node_errors = json.loads(tips).get("node_errors", {})
            if node_errors:
                # 提交被校验拦截：零扣费，错误里通常带合法枚举清单
                emit("error", reason="NODE_ERRORS", node_errors=node_errors)
                sys.exit(1)
        except json.JSONDecodeError:
            pass
    return str(task_id)


def query_once(host: str, key: str, task_id: str) -> dict:
    return post_json(host + "/openapi/v2/query", {"taskId": str(task_id)},
                     bearer=key, timeout=30)


def fix_mov_to_mp4(path: str) -> bool:
    """QuickTime 容器头（ftyp qt  ）改为标准 MP4（isom）。
    只重写原 ftyp 盒自身并保持原盒长，绝不越过盒边界——越界会把后继盒的
    头部踩坏，导致 AVFoundation/严格播放器报「媒体损坏」。"""
    p = Path(path)
    with p.open("rb") as f:
        head = f.read(64)
    if len(head) < 16 or head[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(head[0:4], "big")
    if box_size < 16 or box_size > len(head) or head[8:12] != b"qt  ":
        return False
    brands = [b"isom", b"iso2", b"avc1", b"mp41"]
    new_ftyp = box_size.to_bytes(4, "big") + b"ftyp" + b"isom" + head[12:16]
    for brand in brands[: (box_size - 16) // 4]:
        new_ftyp += brand
    new_ftyp += b"\x00" * (box_size - len(new_ftyp))
    with p.open("r+b") as f:
        f.write(new_ftyp)
    return True


def _safe_name(name: str) -> str:
    """项目/镜头名净化：去掉路径分隔符与父目录引用，防止写出到目录之外。"""
    cleaned = name.replace("\\", "_").replace("/", "_").replace("..", "_").strip()
    return cleaned or "_"


def download_results(urls: list[str], output_base: str | None,
                     project: str | None = None, shot: str | None = None) -> list[str]:
    out_paths = []
    total = len(urls)
    for i, url in enumerate(urls):
        if not output_base:
            # 默认持久目录：~/Downloads/runninghub/[项目名/]镜头名.mp4
            out_dir = Path.home() / "Downloads" / "runninghub"
            if project:
                out_dir = out_dir / _safe_name(project)
            stem = _safe_name(shot) if shot else "h3_" + str(int(time.time()))
            out = str(out_dir / (stem + ".mp4"))
        elif total == 1:
            out = output_base
        else:
            stem = Path(output_base).stem
            suffix = Path(output_base).suffix or ".mp4"
            out = str(Path(output_base).parent / (stem + "_" + str(i + 1) + suffix))
        if total > 1:
            p = Path(out)
            out = str(p.parent / (p.stem + "_" + str(i + 1) + (p.suffix or ".mp4")))
        # 撞名自动加序号，绝不覆盖既有成片
        p = Path(out)
        k = 2
        while p.exists():
            out = str(p.parent / (p.stem + "_" + str(k) + (p.suffix or ".mp4")))
            k += 1
        download_to(url, out)
        if fix_mov_to_mp4(out):
            emit("container_fixed", file=out)
        out_paths.append(str(Path(out).resolve()))
    return out_paths


def watch_task(host: str, key: str, task_id: str, output: str | None,
               interval: int, max_seconds: int, heartbeat: int, notify: bool,
               open_finder: bool = False, project: str | None = None,
               shot: str | None = None) -> None:
    start = time.time()
    last_status, last_beat = None, start
    poll_failures = 0
    while True:
        try:
            resp = query_once(host, key, task_id)
            poll_failures = 0
        except Exception as e:
            poll_failures += 1
            if poll_failures == 1 or poll_failures % 6 == 0:
                # 轮询失败必须可见（节流），避免静默重试让消费方误以为卡死
                emit("poll_error", consecutive=poll_failures,
                     detail=str(e)[:200], taskId=task_id)
            if time.time() - start > max_seconds:
                emit("error", reason="POLL_TIMEOUT", detail=str(e)[:200], taskId=task_id)
                sys.exit(1)
            time.sleep(interval)
            continue
        status = resp.get("status", "")
        now = time.time()
        if status != last_status:
            emit("progress", status=status, elapsed=round(now - start, 1), taskId=task_id)
            last_status = status
            last_beat = now
        elif now - last_beat >= heartbeat:
            emit("progress", status=status, heartbeat=True,
                 elapsed=round(now - start, 1), taskId=task_id)
            last_beat = now

        if status == "SUCCESS":
            results = resp.get("results") or []
            usage = resp.get("usage") or {}
            urls = [u.get("url") or u.get("outputUrl") for u in results]
            urls = [u for u in urls if u]
            paths = download_results(urls, output, project, shot) if urls else []
            registry_update(task_id, {"status": "完成", "output": paths[0] if paths else None,
                                  "cost_coins": usage.get("consumeCoins"),
                                  "gpu_seconds": usage.get("taskCostTime"),
                                  "time_end": int(time.time() * 1000)})
            done = {
                "ts": round(time.time(), 3), "event": "done", "taskId": task_id,
                "file": paths[0] if paths else None, "files": paths, "urls": urls,
                "costCoins": usage.get("consumeCoins"),
                "costMoney": usage.get("consumeMoney") or usage.get("thirdPartyConsumeMoney"),
                "gpuSeconds": usage.get("taskCostTime"),
            }
            print(json.dumps(done, ensure_ascii=False), flush=True)
            if open_finder and paths:
                reveal_file(paths[0])
            if notify:
                sys_notify("H3 工作流完成", Path(paths[0]).name if paths else task_id)
            return
        if status == "FAILED":
            fr = resp.get("failedReason") or {}
            registry_update(task_id, {"status": "失败",
                                  "failure_node": fr.get("node_name"),
                                  "failure": fr.get("exception_message"),
                                  "time_end": int(time.time() * 1000)})
            emit("failed", taskId=task_id, errorCode=resp.get("errorCode"),
                 node=fr.get("node_id"), node_name=fr.get("node_name"),
                 exception=fr.get("exception_message"),
                 traceback=(fr.get("traceback") or [""])[-1][:500])
            if notify:
                sys_notify("H3 工作流失败", str(fr.get("node_name") or resp.get("errorCode")))
            sys.exit(1)
        if now - start > max_seconds:
            emit("error", reason="POLL_TIMEOUT", detail="超过 " + str(max_seconds) + "s",
                 taskId=task_id)
            sys.exit(1)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_common(p) -> None:
    p.add_argument("--output", "-o")
    p.add_argument("--project", help="项目/会话名：自动建 ~/Downloads/runninghub/<project>/ 子目录")
    p.add_argument("--shot", help="镜头名：作为视频文件名（自动加 .mp4，撞名自动加序号）")
    p.add_argument("--interval", type=int, default=POLL_INTERVAL_DEFAULT)
    p.add_argument("--max-seconds", type=int, default=MAX_SECONDS_DEFAULT)
    p.add_argument("--heartbeat", type=int, default=60, help="状态无变化时的心跳间隔秒数")
    p.add_argument("--notify", action="store_true", help="结束时发 macOS 系统通知（CI 感知）")
    p.add_argument("--open", action="store_true", help="完成后在 Finder 中显示文件")


def main() -> None:
    ap = argparse.ArgumentParser(description="RunningHub H3 workflow runner (NDJSON events)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_sub = sub.add_parser("submit", help="提交工作流任务")
    p_sub.add_argument("--workflow", help="workflow 模式=workflowId；aiapp 模式=webappId；"
                                          "可省略并由 --config 提供")
    p_sub.add_argument("--config", help="复用配置（extract 的产物 JSON：含 target/appId/params 默认值）")
    p_sub.add_argument("--dry-run", action="store_true",
                       help="只打印将提交的 nodeInfoList，不真正提交（免费；"
                            "注意素材仍会真实上传以获取 fileName，上传不计费）")
    p_sub.add_argument("--target", choices=["workflow", "aiapp"], default=None,
                       help="提交端点：workflow=/task/openapi/create；aiapp=/openapi/v2/run/ai-app/<id>"
                            "（缺省依次取 --config 的 target、workflow）")
    p_sub.add_argument("--node", action="append", default=[],
                       help="id:field=value，可重复（如 259:value=10）")
    p_sub.add_argument("--file", action="append", default=[],
                       help="id:field=path，先上传再回填 fileName，可重复（如 27:video=a.mp4）")
    p_sub.add_argument("--confirm-params", action="store_true",
                       help="把本次生效的画面比例/分辨率等参数固化为项目档案"
                            "（配合 --project；之后同项目提交自动沿用）")
    add_common(p_sub)
    p_sub.add_argument("--no-wait", action="store_true", help="只提交不轮询")

    p_w = sub.add_parser("watch", help="轮询已有任务")
    p_w.add_argument("--task", required=True)
    add_common(p_w)

    p_s = sub.add_parser("status", help="单次查询任务状态（不轮询、不下载）")
    p_s.add_argument("--task", required=True)

    p_e = sub.add_parser("extract", help="从 AI 应用提取参数白名单 → 面板 workflow.json 草稿")
    p_e.add_argument("--webapp", required=True, help="AI 应用的 webappId")
    p_e.add_argument("--out", "-o", help="输出路径（缺省打印到 stdout）")

    p_prm = sub.add_parser("params", help="查看/固化项目参数档案（画面比例、分辨率等）")
    p_prm.add_argument("--project", required=True, help="项目名（与 submit --project 一致）")
    p_prm.add_argument("--set", action="append", default=[], metavar="NODE:FIELD=VALUE",
                       help='固化一项，如 --set "252:aspect_ratio=9:16 (Portrait Widescreen)"（可重复）')
    p_prm.add_argument("--unset", action="append", default=[], metavar="NODE:FIELD",
                       help="移除一项（可重复）")

    ap.add_argument("--register", action="store_true",
                    help="把任务登记进共享任务注册表（面板与 CLI 共用；"
                         "注册表默认 ~/.runninghub-panel/tasks.json，存在即自动启用）")

    args = ap.parse_args()
    # params 是纯本地操作（项目参数档案），不应强迫先配密钥
    if args.cmd == "params":
        host = key = ""
    else:
        host, key = load_cfg()

    global REGISTRY_PATH
    default_registry = Path.home() / ".runninghub-panel" / "tasks.json"
    if args.register or default_registry.exists():
        REGISTRY_PATH = str(default_registry)   # 面板与 CLI 共用的任务登记表

    if args.cmd == "submit":
        # 配置文件（extract 产物）提供 target / app id / 参数默认值；CLI 参数优先
        cfg = {}
        if args.config:
            try:
                cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
            except Exception as e:
                emit("error", reason="BAD_CONFIG", detail=str(e)[:200])
                sys.exit(1)
        app_id = args.workflow or cfg.get("webappId") or cfg.get("workflowId")
        if not app_id:
            emit("error", reason="NO_WORKFLOW_ID",
                 detail="用 --workflow 指定，或 --config 提供(webappId/workflowId)")
            sys.exit(1)
        target = args.target or cfg.get("target") or "workflow"

        nodes: list[dict] = []
        for f in args.file:
            nid, field, path = parse_kv(f)
            emit("uploading", node=nid, file=Path(path).name)
            name = upload_media(host, key, path)
            emit("uploaded", node=nid, fileName=name)
            # extract 配置记录了每个媒体节点的真实字段名（video/image/audio）；
            # 用户手滑写 file= 等通用名时以配置为准纠正，避免提交被
            # NODE_INFO_MISMATCH(field_not_found_in_node_inputs) 拦截
            cfg_field = media_field(cfg, nid)
            if cfg_field and cfg_field != field:
                emit("field_fixed", node=nid, field=field, use=cfg_field)
                field = cfg_field
            nodes.append({"nodeId": nid, "fieldName": field, "fieldValue": name})
        for n in args.node:
            nid, field, value = parse_kv(n)
            nodes.append({"nodeId": nid, "fieldName": field, "fieldValue": value})
        # 参数回填优先级：--node/--file 显式 > 项目档案（一次核对、整项目沿用） > extract 默认值
        pinned_params = (load_profile(args.project).get("params") or {}) if args.project else {}
        sources: dict[str, str] = {}
        for n in nodes:
            sources[f'{n["nodeId"]}:{n["fieldName"]}'] = "cli"
        overridden = {(n["nodeId"], n["fieldName"]) for n in nodes}
        for pkey, pval in pinned_params.items():
            if ":" not in pkey or pval in (None, ""):
                continue
            nid, field = pkey.split(":", 1)
            if (nid, field) not in overridden:
                nodes.append({"nodeId": nid, "fieldName": field, "fieldValue": str(pval)})
                sources[pkey] = "project"
                overridden.add((nid, field))
                emit("param_pinned", project=args.project, node=nid, field=field,
                     value=str(pval))
        for p in (cfg.get("params") or {}).values():
            if p.get("node") and p.get("field") and \
               (str(p["node"]), str(p["field"])) not in overridden \
               and p.get("default") not in (None, ""):
                nodes.append({"nodeId": str(p["node"]), "fieldName": str(p["field"]),
                              "fieldValue": str(p["default"])})
                sources[f'{p["node"]}:{p["field"]}'] = "config"

        # 画面比例/尺寸核对：一个项目内通常不变。未经核对的 → 提问；与档案不一致的 → 告警
        if args.project:
            pending, mismatch = [], []
            for n in nodes:
                if n["fieldName"].lower() not in STICKY_FIELDS:
                    continue
                skey = f'{n["nodeId"]}:{n["fieldName"]}'
                if skey not in pinned_params:
                    pending.append({"node": n["nodeId"], "field": n["fieldName"],
                                    "effective": n["fieldValue"],
                                    "hint": f'params --project {args.project} --set "{skey}={n["fieldValue"]}"'})
                elif str(pinned_params[skey]) != str(n["fieldValue"]):
                    mismatch.append({"node": n["nodeId"], "field": n["fieldName"],
                                     "pinned": pinned_params[skey],
                                     "effective": n["fieldValue"]})
            if pending:
                emit("param_confirm", project=args.project, unconfirmed=pending,
                     question="这些画面比例/尺寸参数尚未在本项目核对过（当前值未必是你要的），"
                              "请向用户确认；确认方式：按 hint 用 params 子命令固化，"
                              "或本次提交加 --confirm-params")
            if mismatch:
                emit("param_mismatch", project=args.project, diff=mismatch,
                     warning="画面比例/尺寸与本项目档案不一致；有意为之可忽略，否则改回档案值")
            if args.confirm_params:
                new_pinned = dict(pinned_params)
                for n in nodes:
                    if n["fieldName"].lower() in STICKY_FIELDS:
                        new_pinned[f'{n["nodeId"]}:{n["fieldName"]}'] = str(n["fieldValue"])
                if new_pinned != pinned_params:
                    save_profile(args.project,
                                 {"params": new_pinned, "updated": int(time.time())})
                    emit("param_saved", project=args.project,
                         pinned={k: new_pinned[k] for k in sorted(new_pinned)})

        if args.dry_run:
            print(json.dumps({"target": target, "appId": app_id,
                              "nodeInfoList": nodes,
                              "param_sources": sources}, ensure_ascii=False, indent=1))
            return
        task_id = submit(host, key, app_id, nodes, target=target)
        registry_update(task_id, {"source": "agent", "status": "已提交",
                                  "project": args.project or "panel",
                                  "shot": args.shot or "",
                                  "target": target,
                                  "app_id": app_id})
        emit("submitted", taskId=task_id)
        if not args.no_wait:
            watch_task(host, key, task_id, args.output,
                       args.interval, args.max_seconds, args.heartbeat, args.notify,
                       open_finder=args.open, project=args.project, shot=args.shot)
    elif args.cmd == "extract":
        cmd_extract(host, key, args.webapp, args.out)
    elif args.cmd == "params":
        pinned = dict(load_profile(args.project).get("params") or {})
        for item in args.set:
            kv, sep, value = item.partition("=")
            if not (sep and ":" in kv and value != ""):
                emit("error", reason="BAD_ARG", detail=item)
                sys.exit(1)
            pinned[kv] = value
        for item in args.unset:
            pinned.pop(item, None)
        if args.set or args.unset:
            save_profile(args.project, {"params": pinned, "updated": int(time.time())})
            emit("param_saved", project=args.project, pinned=pinned)
        emit("params", project=args.project, file=str(profile_path(args.project)),
             params=pinned)
    elif args.cmd == "status":
        resp = query_once(host, key, args.task)
        usage = resp.get("usage") or {}
        fr = resp.get("failedReason") or {}
        emit("status", taskId=args.task, status=resp.get("status", ""),
             results=len(resp.get("results") or []),
             costCoins=usage.get("consumeCoins"),
             costMoney=usage.get("consumeMoney") or usage.get("thirdPartyConsumeMoney"),
             gpuSeconds=usage.get("taskCostTime"),
             node=fr.get("node_id"), node_name=fr.get("node_name"),
             exception=fr.get("exception_message"))
    else:
        watch_task(host, key, args.task, args.output,
                   args.interval, args.max_seconds, args.heartbeat, args.notify,
                   open_finder=args.open, project=args.project, shot=args.shot)


if __name__ == "__main__":
    main()
