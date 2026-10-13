#!/usr/bin/env python3
"""RunningHub GPT Image（rhart-image-g-2）REST 客户端 —— 与 h3_run.py 同级的生图通道。

h3_run.py 只覆盖 workflow / aiapp 两类提交通道；GPT Image 不是 workflow，
而是独立 REST 端点族（text-to-image / image-to-image / upload / query），本脚本封装之。

用法：
  gpt_image.py t2i  --prompt "..." [-o out.png] [--aspect "16:9"] [--resolution 1k]
  gpt_image.py i2i  --prompt "..." --ref a.png --ref b.png [-o out.png] [--aspect ...] [--resolution ...]
  gpt_image.py fetch --task-id <id> [-o out.png]     # 按 taskId 找回结果（SSL 抖动丢了下载时用，不重复扣费）

密钥来源（依次尝试）：--config 路径 → $GPT_IMAGE_CONFIG → ~/.config/runninghub/enterprise.json
→ ~/.config/runninghub/config.json。JSON 需含 apiKey 与 host。

实测要点（2026-10-04）：
- imageUrls 最多 10 张，第 11 张直接被拒（input number greater than maximum: 10）。
- 上传用 POST /openapi/v2/media/upload/binary（multipart 字段名 file）→ 取 data.download_url。
- 轮询 POST /openapi/v2/query，status 在响应顶层；结果在 results[].url。
- 计费约 $0.015/张（1k）；失败不扣费。网络抖动常见 SSL: UNEXPECTED_EOF —— 上传/查询/下载均需重试。
"""
import argparse, json, os, sys, time, uuid, urllib.request

DEFAULT_ASPECT = "16:9"
DEFAULT_RES = "1k"
RETRYABLE = ("SSL: UNEXPECTED_EOF", "timed out", "URLError", "ConnectionReset")

def load_cfg(path):
    for p in ([path] if path else []) + [
        os.environ.get("GPT_IMAGE_CONFIG"),
        os.path.expanduser("~/.config/runninghub/enterprise.json"),
        os.path.expanduser("~/.config/runninghub/config.json"),
    ]:
        if p and os.path.exists(p):
            cfg = json.load(open(p))
            if cfg.get("apiKey") and cfg.get("host"):
                return cfg
    sys.exit("未找到密钥配置（--config / GPT_IMAGE_CONFIG / ~/.config/runninghub/*.json）")

def _post(cfg, url, payload, timeout=120, retries=4):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["apiKey"]})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            if any(s in str(e) for s in RETRYABLE) and i < retries - 1:
                time.sleep(5 * (i + 1)); continue
            raise
    raise last

def upload_file(cfg, path):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{os.path.basename(path)}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(cfg["host"] + "/openapi/v2/media/upload/binary", data=body,
                headers={"Authorization": "Bearer " + cfg["apiKey"],
                         "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=300) as r:
                d = json.loads(r.read().decode())
            url = d["data"]["download_url"]
            print(json.dumps({"event": "uploaded", "file": os.path.basename(path), "url": url}), flush=True)
            return url
        except Exception as e:
            last = e
            if any(s in str(e) for s in RETRYABLE) and i < 3:
                time.sleep(5 * (i + 1)); continue
            raise
    raise last

def submit_task(cfg, prompt, refs, aspect, resolution):
    payload = {"prompt": prompt, "aspectRatio": aspect, "resolution": resolution}
    if refs:
        if len(refs) > 10:
            sys.exit("imageUrls 最多 10 张")
        payload["imageUrls"] = refs
    d = _post(cfg, cfg["host"] + "/openapi/v2/rhart-image-g-2/" + ("image-to-image" if refs else "text-to-image"), payload)
    if d.get("errorCode") or not d.get("taskId"):
        sys.exit("submit failed: " + json.dumps(d, ensure_ascii=False)[:300])
    print(json.dumps({"event": "submitted", "taskId": d["taskId"]}), flush=True)
    return d["taskId"]

def query(cfg, task_id):
    return _post(cfg, cfg["host"] + "/openapi/v2/query", {"taskId": task_id}, timeout=60)

def wait_and_download(cfg, task_id, out, max_seconds=600):
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        d = query(cfg, task_id)
        st = d.get("status")
        print(json.dumps({"event": "progress", "status": st, "elapsed": round(time.time() - t0, 1)}), flush=True)
        if st == "SUCCESS":
            for res in (d.get("results") or []):
                for i in range(4):
                    try:
                        urllib.request.urlretrieve(res["url"], out)
                        print(json.dumps({"event": "done", "file": out,
                                          "cost": (d.get("usage") or {}).get("thirdPartyConsumeMoney")}), flush=True)
                        return
                    except Exception as e:
                        if i == 3: raise
                        time.sleep(5 * (i + 1))
            return
        if st == "FAILED":
            sys.exit("task FAILED: " + json.dumps(d, ensure_ascii=False)[:300])
        time.sleep(8)
    sys.exit("poll timeout —— 任务可能仍在跑，稍后用 fetch 子命令按 taskId 找回")

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="密钥 JSON 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("t2i"); p1.add_argument("--prompt", required=True)
    p1.add_argument("-o", "--output", required=True)
    p1.add_argument("--aspect", default=DEFAULT_ASPECT); p1.add_argument("--resolution", default=DEFAULT_RES)
    p1.add_argument("--no-wait", action="store_true")

    p2 = sub.add_parser("i2i"); p2.add_argument("--prompt", required=True)
    p2.add_argument("--ref", action="append", required=True)
    p2.add_argument("-o", "--output", required=True)
    p2.add_argument("--aspect", default=DEFAULT_ASPECT); p2.add_argument("--resolution", default=DEFAULT_RES)
    p2.add_argument("--no-wait", action="store_true")

    p3 = sub.add_parser("fetch"); p3.add_argument("--task-id", required=True)
    p3.add_argument("-o", "--output", required=True)

    a = ap.parse_args()
    cfg = load_cfg(a.config)
    if a.cmd == "fetch":
        wait_and_download(cfg, a.task_id, a.output, max_seconds=120)
    else:
        refs = [upload_file(cfg, p) for p in (a.ref if a.cmd == "i2i" else [])]
        tid = submit_task(cfg, a.prompt, refs, a.aspect, a.resolution)
        if not a.no_wait:
            wait_and_download(cfg, tid, a.output)

if __name__ == "__main__":
    main()
