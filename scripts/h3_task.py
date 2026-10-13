#!/usr/bin/env python3
"""
h3_task.py — RunningHub 提交全生命周期编排器（h3_run.py 的高层封装）。

设计分工：
  Agent   只做创作决策：写提示词、选参考素材、填 plan（语义名，无槽位号）
  本脚本  机械执行：语义→槽位翻译、提交前校验、退避重试、机械降级、台账、QC
  人/Agent 语义决策：OOM 后减哪张图、砍不砍时长（本脚本只报告，不擅动素材）

子命令：
  submit    <plan.json>            提交单任务（validate → 退避 → 台账）
  parallel  <plan1.json> <plan2.json> ...   并发提交（上限 2，日志落盘）
  qc        <video>                一键质检：规格/抽帧表/音轨发声段
  tasks                            刷新台账：当前几个任务在跑/排队/完成
  cancel    <taskId>               取消任务（不扣费）
  ledger    [shot过滤]             按 shot 汇总花费
  validate  <plan.json>            只校验不提交
  workflows                        列出已沉淀的工作流映射表

plan.json（语义字段，槽位号由 workflows/<id>.json 翻译）：
{
  "workflow":   "2093984571330498561",
  "shot":       "shot5f_白光转场_v2",
  "project":    "H3音色克隆",
  "seconds":    5,
  "quality":    "1080p",              ← 查映射表 quality_map；千问类填 "16:9" 等
  "instance":   "default",            ← 可省略
  "media":      {"video1": "ref.mp4", "image1": "char.png", "audio1": "voice.mp3"},
  "prompt_file": "prompt.md",         ← 或 "prompt": "内联文本"
  "output":     "out.mp4",
  "policy":     {"auto_plus": true}   ← OOM 机械降级是否允许自动升 plus（更贵）
}

退避策略（仅约束提交阶段）：
  INIT 30s ×1.5 cap 90s · equal-jitter · 最多 6 次 · 总 TTL 60 分钟
  预留 estimated_runtime（按秒数/画质动态估算）不够则立即放弃
  可重试白名单：并发槽满 / SSL 抖动 / 5xx；其余错误立即抛回（重试无意义）

台账：~/.runninghub/tasks_ledger.jsonl（每行一个事件，tasks/ledger 按最新状态聚合）
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
H3_RUN = SCRIPT_DIR / "h3_run.py"
WF_DIR = SCRIPT_DIR.parent / "workflows"
LEDGER = Path.home() / ".runninghub" / "tasks_ledger.jsonl"
SETTINGS_FILE = Path.home() / ".runninghub" / "settings.json"

# ---- 设置（可覆盖：~/.runninghub/settings.json < plan.policy < 命令行）----
_SETTINGS_DEFAULTS = {
    "max_parallel": 2,
    "backoff": {"init_seconds": 30, "multiplier": 1.5, "max_seconds": 90,
                "max_retry": 6, "ttl_minutes": 60, "jitter": "equal"},
    "default_policy": {"auto_plus": True},
}


def load_settings() -> dict:
    s = json.loads(json.dumps(_SETTINGS_DEFAULTS))      # deep copy
    if SETTINGS_FILE.exists():
        try:
            user = json.loads(SETTINGS_FILE.read_text())
            for k, v in user.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(s.get(k), dict):
                    s[k].update(v)
                else:
                    s[k] = v
        except json.JSONDecodeError as e:
            print(f"[settings] {SETTINGS_FILE} 解析失败，用默认值：{e}", file=sys.stderr)
    return s


S = load_settings()
INIT_BACKOFF = S["backoff"]["init_seconds"]
MULTIPLIER = S["backoff"]["multiplier"]
MAX_BACKOFF = S["backoff"]["max_seconds"]
MAX_RETRY = S["backoff"]["max_retry"]
MAX_TASK_TTL = int(S["backoff"]["ttl_minutes"] * 60)

RETRYABLE = {"QUEUE_LIMIT", "SSL_ERROR", "SERVER_5XX"}
FATAL = {"FILE_NOT_FOUND", "PARAM_INVALID", "NODE_ERRORS", "NO_BALANCE", "UNKNOWN_SUBMIT"}

QUALITY_ORDER = ["1080p", "1440", "720p"]   # 机械降级顺序（H3 类）


# ---------------------------------------------------------------- 基础设施

def cfg() -> dict:
    for p in ["~/.config/runninghub/config.json"]:
        f = Path(p).expanduser()
        if f.exists():
            return json.loads(f.read_text())
    return {"apiKey": os.environ.get("RUNNINGHUB_API_KEY", ""),
            "host": os.environ.get("RUNNINGHUB_HOST", "https://www.runninghub.ai")}


def load_wf(wf_id: str) -> dict:
    f = WF_DIR / f"{wf_id}.json"
    if not f.exists():
        sys.exit(f"[workflows] 无映射表 {f}。先用 `extract` 摸清槽位，再总结沉淀到该文件。")
    return json.loads(f.read_text())


def append_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def ledger_latest() -> dict:
    """taskId → 最新一条记录"""
    latest = {}
    for rec in read_ledger():
        tid = rec.get("taskId")
        if tid:
            latest[tid] = rec
    return latest


def query_once(task_id: str) -> dict:
    c = cfg()
    req = urllib.request.Request(
        c["host"] + "/openapi/v2/query",
        data=json.dumps({"taskId": task_id}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + c["apiKey"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def ffprobe(path: str) -> dict:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,r_frame_rate,nb_frames",
             "-of", "json", path], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout or "{}")
    except Exception:
        return {}


def err(msg: str, code: int = 1):
    print(f"[h3_task] 错误：{msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- plan 翻译

def translate_plan(plan: dict) -> tuple[str, list[str]]:
    """语义 plan → (workflow_id, h3_run.py 参数列表)。查映射表翻译。"""
    wf = load_wf(str(plan["workflow"]))
    args = ["submit", "--target", wf["target"], "--workflow", str(wf["id"])]
    if wf.get("config"):
        args += ["--config", os.path.expanduser(wf["config"])]
    if plan.get("project"):
        args += ["--project", plan["project"]]
    args += ["--shot", plan.get("shot", "task")]

    media = plan.get("media", {})
    slots, overrides = wf["media_slots"], wf.get("media_field_overrides", {})
    counts = {"video": 0, "image": 0, "audio": 0}
    for sem, path in media.items():
        if sem not in slots:
            err(f"plan.media 的语义名 '{sem}' 不在 {wf['id']} 映射表里（可用：{list(slots)}）")
        slot = slots[sem]
        kind = overrides.get(sem, wf.get("media_field", "image"))
        counts[kind.rstrip("0123456789")] += 1
        args += ["--file", f"{slot}:{kind}={os.path.abspath(os.path.expanduser(path))}"]

    lim = wf.get("limits", {})
    for kind, cap_key in [("video", "video"), ("image", "image"), ("audio", "audio")]:
        if cap_key in lim and counts[kind] > lim[cap_key]:
            err(f"{kind} 数量 {counts[kind]} 超过 {wf['id']} 上限 {lim[cap_key]}")

    prompt = plan.get("prompt")
    if plan.get("prompt_file"):
        prompt = Path(plan["prompt_file"]).expanduser().read_text()
    if prompt:
        pn = wf["params"]["prompt"]
        args += ["--node", f"{pn['node']}:{pn['field']}={prompt}"]

    q = wf.get("quality")
    if q and plan.get("quality"):
        spec = q["map"].get(plan["quality"])
        if spec is None:
            err(f"quality '{plan['quality']}' 不在 {wf['id']} 的 quality_map（可用：{list(q['map'])}）")
        p1, s1 = q["primary"], q["secondary"]
        if q.get("type") == "aspect_ratio":
            args += ["--node", f"{p1['node']}:{p1['field']}={spec[0]}",
                     "--node", f"{s1['node']}:{s1['field']}={spec[1]}"]
        else:
            args += ["--node", f"{p1['node']}:{p1['field']}={spec[0]}",
                     "--node", f"{s1['node']}:{s1['field']}={spec[1]}"]

    if plan.get("seconds") and "seconds" in wf["params"]:
        sn = wf["params"]["seconds"]
        args += ["--node", f"{sn['node']}:{sn['field']}={plan['seconds']}"]

    if plan.get("instance"):
        args += ["--instance", plan["instance"]]

    out = os.path.abspath(os.path.expanduser(plan["output"]))
    args += ["--output", out, "--notify", "--max-seconds", "2700"]
    return str(wf["id"]), args


def estimated_runtime_min(plan: dict, wf: dict) -> float:
    est = wf.get("estimated_runtime_min")
    if not est:
        return 20.0
    t = est.get("base", 6) + est.get("per_second", 0.6) * float(plan.get("seconds", 5))
    if plan.get("instance") == "plus" or (plan.get("quality") == "1080p" and est.get("plus_1080p_extra")):
        t += est.get("plus_1080p_extra", 0)
    return t


# ---------------------------------------------------------------- validate

ZH = re.compile(r"[\u4e00-\u9fff]")


def validate(plan: dict, verbose: bool = True) -> list[str]:
    """提交前自检。返回问题列表（空 = 通过）。"""
    problems = []
    wf = load_wf(str(plan["workflow"]))

    for sem, p in plan.get("media", {}).items():
        p = os.path.expanduser(p)
        if not os.path.exists(p):
            problems.append(f"media.{sem} 文件不存在: {p}")
        elif sem.startswith("audio"):
            info = ffprobe(p)
            dur = float((info.get("format") or {}).get("duration", 0))
            lo, hi = wf.get("limits", {}).get("audio_seconds", [4, 15])
            if dur and not (lo <= dur <= hi):
                problems.append(f"media.{sem} 音频时长 {dur:.1f}s 超出 [{lo},{hi}]s")

    prompt = plan.get("prompt")
    if plan.get("prompt_file"):
        pf = Path(plan["prompt_file"]).expanduser()
        if not pf.exists():
            problems.append(f"prompt_file 不存在: {pf}")
        else:
            prompt = pf.read_text()
    if prompt:
        if wf.get("prompt_language", "en_only") == "en_only":
            body = re.sub(r"<d>.*?</d>", "", prompt, flags=re.S)
            bad = [ln for ln in body.splitlines() if ZH.search(ln)]
            if bad:
                problems.append(f"提示词 <d> 外混入中文（会导致台词夹中文）：{bad[:2]}")

    sec = plan.get("seconds")
    lo, hi = wf.get("limits", {}).get("seconds", [5, 15])
    if sec and not (lo <= sec <= hi):
        problems.append(f"seconds={sec} 超出 [{lo},{hi}]")

    out = plan.get("output")
    if out and os.path.exists(os.path.expanduser(out)) and not plan.get("overwrite"):
        problems.append(f"output 已存在（加 overwrite:true 覆盖）: {out}")

    if verbose:
        if problems:
            for p in problems:
                print(f"  ✗ {p}")
            print(f"[validate] {len(problems)} 个问题，禁止提交")
        else:
            print("[validate] 全部通过 ✓")
    return problems


# ---------------------------------------------------------------- 提交 + 退避

def classify_error(events: list[dict]) -> str:
    """从 h3_run.py 事件流提取最后一个错误并分类。"""
    for ev in reversed(events):
        if ev.get("event") == "error":
            detail = json.dumps(ev.get("detail", ""), ensure_ascii=False) + \
                     json.dumps(ev, ensure_ascii=False)
            reason = ev.get("reason", "")
            if reason == "NODE_ERRORS" or "NODE_INFO_MISMATCH" in detail:
                return "PARAM_INVALID"
            if "FILE_NOT_FOUND" in detail or "No such file" in detail:
                return "FILE_NOT_FOUND"
            if "queue limit" in detail or "并发" in detail or "421" in detail:
                return "QUEUE_LIMIT"
            if "SSL" in detail or "UNEXPECTED_EOF" in detail or "ConnectionReset" in detail \
               or "timed out" in detail or "URLError" in detail:
                return "SSL_ERROR"
            if reason == "SUBMIT_FAILED":
                return "UNKNOWN_SUBMIT"
            return reason or "UNKNOWN"
    return "NO_DONE"


def run_h3_run(args: list[str], log_path: Path | None = None) -> tuple[bool, list[dict]]:
    """跑 h3_run.py，消费 NDJSON 事件流。返回 (成功?, 事件列表)。"""
    cmd = [sys.executable, str(H3_RUN)] + args
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    events, task_id = [], None
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            if log_path:
                log_path.open("a").write(line + "\n")
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if log_path:
            log_path.open("a").write(json.dumps(ev, ensure_ascii=False) + "\n")
        if ev.get("event") == "submitted":
            task_id = ev.get("taskId")
        if ev.get("event") in ("done", "failed", "error"):
            break
    proc.wait()
    ok = any(ev.get("event") == "done" for ev in events)      # 只认 done（SSL 日志误判防线）
    return ok, events


def submit_with_backoff(plan: dict) -> dict:
    """退避状态机：仅约束提交阶段。OOM 在执行阶段 → 机械降级。返回结果摘要。"""
    wf = load_wf(str(plan["workflow"]))
    problems = validate(plan)
    if problems:
        return {"ok": False, "stage": "validate", "problems": problems}

    auto_plus = (plan.get("policy") or {}).get(
        "auto_plus", S.get("default_policy", {}).get("auto_plus", True))
    q = plan.get("quality", "1080p")
    inst = plan.get("instance", "default")
    attempts = []          # 每次尝试的记录
    start = time.time()

    # 机械降级序列（仅 H3 类，纯参数级；减图属语义决策，绝不自动做）
    chain = [(inst, q)]
    if "quality" in wf.get("quality", {}).get("map", {}) and wf["quality"].get("secondary"):
        qi = QUALITY_ORDER.index(q) if q in QUALITY_ORDER else 99
        for lower in QUALITY_ORDER[qi + 1:]:
            chain.append((inst, lower))
        if auto_plus and inst != "plus":
            chain.append(("plus", q))

    chain_i, backoff, retry = 0, INIT_BACKOFF, 0
    cur = dict(plan)
    log_path = Path(os.path.abspath(os.path.expanduser(plan["output"]))).with_suffix(".log")

    while True:
        cur = dict(plan)
        if chain_i > 0:
            cur["instance"], cur["quality"] = chain[chain_i]
        wf_id, args = translate_plan(cur)
        print(f"[submit] 尝试 {retry + 1}/{MAX_RETRY} 档位 inst={cur.get('instance', 'default')} "
              f"quality={cur.get('quality', '-')}：上传+提交中…")
        t0 = time.time()
        ok, events = run_h3_run(args, log_path)
        elapsed = time.time() - t0

        if ok:
            done_ev = next(e for e in events if e["event"] == "done")
            sub_ev = next((e for e in events if e["event"] == "submitted"), {})
            out_file = done_ev.get("file") or os.path.abspath(os.path.expanduser(plan["output"]))
            rec = {"ts": int(time.time()), "taskId": sub_ev.get("taskId", ""),
                   "shot": plan.get("shot", ""), "workflow": str(plan["workflow"]),
                   "instance": cur.get("instance", "default"), "quality": cur.get("quality", ""),
                   "output": out_file,
                   "status": "SUCCESS", "costCoins": done_ev.get("costCoins"),
                   "gpuSeconds": done_ev.get("gpuSeconds"), "attempts": retry + 1,
                   "degraded": chain_i > 0}
            append_ledger(rec)
            print(f"[submit] ✓ 出片 {out_file}  "
                  f"{done_ev.get('costCoins', '?')} RHB / {done_ev.get('gpuSeconds', '?')} GPU s"
                  + ("（已机械降级）" if chain_i > 0 else ""))
            return {"ok": True, **rec}

        # 失败：区分 提交被拒（可退避）vs 执行 OOM（机械降级）
        oom = any("OutOfMemory" in json.dumps(e, ensure_ascii=False) or
                  e.get("errorCode") == "805" for e in events)
        kind = "OOM" if oom else classify_error(events)

        if kind == "OOM":
            chain_i += 1
            if chain_i < len(chain):
                print(f"[submit] ✗ OOM → 机械降级到 inst={chain[chain_i][0]} quality={chain[chain_i][1]}")
                continue
            return {"ok": False, "stage": "oom_semantic_decision",
                    "report": oom_report(cur, events, log_path),
                    "message": "机械降级链耗尽仍 OOM。需要语义决策：减哪张参考图 / 砍时长 / 拆镜头。素材由上层拍板，本脚本不擅动。"}

        retry += 1
        attempts.append({"kind": kind, "elapsed": round(elapsed, 1)})
        if kind not in RETRYABLE:
            return {"ok": False, "stage": "submit", "error": kind,
                    "message": f"不可重试错误（重试无意义）：{kind}",
                    "last_events": events[-3:], "attempts": attempts}
        remain = MAX_TASK_TTL - (time.time() - start)
        est = estimated_runtime_min(cur, wf) * 60
        if remain < est:
            return {"ok": False, "stage": "submit", "error": "TIME_BUDGET_EXHAUSTED",
                    "message": f"剩余预算 {remain/60:.0f}min 不足估算执行 {est/60:.0f}min，终止重试",
                    "attempts": attempts}
        if retry >= MAX_RETRY:
            return {"ok": False, "stage": "submit", "error": "RETRY_EXHAUSTED",
                    "message": f"{MAX_RETRY} 次重试耗尽（多为共享池拥堵）。可：稍后重试 / 换实例档 / 排队错峰",
                    "attempts": attempts}
        wait = min(backoff * MULTIPLIER, MAX_BACKOFF, remain * 0.4)
        actual = random.uniform(wait * 0.5, wait)      # equal-jitter
        print(f"[submit] ✗ {kind}，退避 {actual:.0f}s 后重试")
        time.sleep(actual)
        backoff = wait
    # unreachable


def oom_report(plan: dict, events: list[dict], log_path: Path) -> dict:
    media_info = []
    for sem, p in plan.get("media", {}).items():
        p = os.path.expanduser(p)
        media_info.append({"slot": sem, "path": p,
                           "size_mb": round(os.path.getsize(p) / 1e6, 1) if os.path.exists(p) else None})
    return {"input_media": media_info, "log": str(log_path),
            "candidates": ["减一张参考图（建议先去掉画风/次要锚点）",
                           "seconds 降一档", "quality 再降档", "拆分成两条短视频"]}


# ---------------------------------------------------------------- 并发

def parallel(plans: list[dict]) -> list[dict]:
    """并发提交。超过 max_parallel 的任务排队等槽位释放（不丢弃）。"""
    sem = threading.Semaphore(S.get("max_parallel", 2))
    results = [None] * len(plans)
    threads = []

    def work(i, p):
        try:
            with sem:                       # 槽位满则等待，前一个任务结束即释放
                results[i] = submit_with_backoff(p)
        except SystemExit as e:
            results[i] = {"ok": False, "error": str(e)}
        except Exception as e:
            results[i] = {"ok": False, "error": repr(e)}

    for i, p in enumerate(plans):
        t = threading.Thread(target=work, args=(i, p))
        t.start()
        threads.append(t)
        if i < len(plans) - 1:
            time.sleep(3)          # 错峰上传
    for t in threads:
        t.join()
    return results


# ---------------------------------------------------------------- qc

def voiced_segments(wav_path: str) -> list[tuple[float, float]]:
    """20ms RMS 窗 → 阈值(峰值6%) + 最短0.1s + 邻近合并。"""
    try:
        w = wave.open(wav_path, "rb")
        n, rate = w.getnframes(), w.getframerate()
        raw = w.readframes(n)
        w.close()
    except Exception:
        return []
    width = w.getsampwidth() if False else 2
    ch = w.getnchannels() if False else 1
    import struct
    step = max(1, int(rate * 0.02))
    vals = []
    for i in range(0, n - step, step):
        frames = raw[i * width * ch:(i + step) * width * ch]
        if len(frames) < width:
            break
        sq = [s * s for s in struct.unpack(f"<{len(frames)//width}h", frames[:len(frames)//width*width])]
        vals.append((i / rate, math.sqrt(sum(sq) / max(1, len(sq)))))
    if not vals:
        return []
    peak = max(v for _, v in vals) or 1
    thr = peak * 0.06
    segs, cur = [], None
    for t, v in vals:
        if v > thr and cur is None:
            cur = t
        elif v <= thr and cur is not None:
            if t - cur >= 0.1:
                segs.append([cur, t])
            cur = None
    if cur is not None:
        segs.append([cur, vals[-1][0]])
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < 0.15:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [tuple(round(x, 2) for x in s) for s in merged]


def qc(video: str) -> dict:
    video = os.path.abspath(os.path.expanduser(video))
    if not os.path.exists(video):
        err(f"文件不存在: {video}")
    info = ffprobe(video)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    out_dir = os.path.join(os.path.dirname(video), "qc_" + Path(video).stem)
    os.makedirs(out_dir, exist_ok=True)

    # 抽帧拼表
    w, h = vs.get("width", 0), vs.get("height", 0)
    tile = "6x4" if True else ""
    sheet = os.path.join(out_dir, "细表.jpg")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video,
                    "-vf", "fps=2,scale=240:-1,tile=6x4", "-frames:v", "1", sheet],
                   capture_output=True)

    # 音轨 + 发声段
    wav = os.path.join(out_dir, "audio.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-vn",
                    "-ac", "1", "-ar", "16000", "-f", "wav", wav], capture_output=True)
    segs = voiced_segments(wav) if os.path.exists(wav) else []

    report = {
        "video": video,
        "spec": {"size": f"{w}x{h}", "fps": vs.get("r_frame_rate"), "frames": vs.get("nb_frames"),
                 "duration": round(float((info.get("format") or {}).get("duration", 0)), 2)},
        "contact_sheet": sheet if os.path.exists(sheet) else None,
        "audio_wav": wav if os.path.exists(wav) else None,
        "voiced_segments": segs,
        "voiced_count": len(segs),
        "notes": ["画外音常被打成画面小字幕 → 看抽帧表确认", "音色/情绪需人耳终判"],
    }
    rp = os.path.join(out_dir, "report.json")
    open(rp, "w").write(json.dumps(report, ensure_ascii=False, indent=1))
    report["report_file"] = rp
    return report


# ---------------------------------------------------------------- tasks / cancel / ledger

TERMINAL = {"SUCCESS", "FAILED", "CANCELED"}


def tasks_cmd() -> None:
    latest = ledger_latest()
    if not latest:
        print("台账为空（还没有经 h3_task 提交过任务；可手动补录：echo '{\"taskId\":\"...\"}' >> ~/.runninghub/tasks_ledger.jsonl）")
        return
    open_tasks = {t: r for t, r in latest.items() if r.get("status") not in TERMINAL}
    done_tasks = {t: r for t, r in latest.items() if r.get("status") in TERMINAL}

    def refresh(tid, rec):
        try:
            resp = query_once(tid)
            rec = dict(rec)
            rec["status"] = resp.get("status", rec.get("status"))
            u = resp.get("usage") or {}
            rec["costCoins"] = u.get("consumeCoins")
            rec["gpuSeconds"] = u.get("taskCostTime")
            if resp.get("results"):
                rec["has_result"] = True
            append_ledger(rec)
        except Exception as e:
            rec["query_error"] = str(e)[:80]
        return rec

    th, refreshed = [], {}
    for tid, rec in open_tasks.items():
        t = threading.Thread(target=lambda tid=tid, rec=rec: refreshed.update(
            {tid: refresh(tid, rec)}))          # 默认参数绑定，防闭包错绑循环变量
        t.start()
        th.append(t)
    for t in th:
        t.join()
    refreshed.update({t: r for t, r in done_tasks.items()})

    running = {t: r for t, r in refreshed.items() if r.get("status") in ("RUNNING", "QUEUED")}
    print(f"=== 任务面板：{len(running)} 个在跑/排队，{len(done_tasks)} 个已完结 ===")
    icons = {"RUNNING": "🔶 运行中", "QUEUED": "🔷 排队"}
    for tid, r in sorted(running.items(), key=lambda kv: -kv[1].get("ts", 0)):
        age = (time.time() - r.get("ts", time.time())) / 60
        print(f"  {icons.get(r.get('status'), '?')}  {tid}  {r.get('shot', '')}  "
              f"自提交 {age:.0f}min  inst={r.get('instance')}  quality={r.get('quality')}")
    for tid, r in sorted(done_tasks.items(), key=lambda kv: -kv[1].get("ts", 0))[:8]:
        print(f"  ✅ {r.get('status')}  {r.get('shot', '')}  {r.get('costCoins', '?')} RHB  "
              f"{r.get('output', '')}")
    total = sum(float(r.get("costCoins") or 0) for r in done_tasks.values())
    print(f"  —— 台账累计 {total:.0f} RHB ——")


def cancel_cmd(task_id: str) -> None:
    c = cfg()
    req = urllib.request.Request(
        c["host"] + "/task/openapi/cancel",
        data=json.dumps({"apiKey": c["apiKey"], "taskId": task_id}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:
        err(f"取消失败: {e}")
    print(json.dumps(resp, ensure_ascii=False)[:300])
    append_ledger({"ts": int(time.time()), "taskId": task_id, "status": "CANCELED"})


def ledger_cmd(filt: str) -> None:
    latest = ledger_latest()
    rows = [r for r in latest.values() if not filt or filt in (r.get("shot") or "")]
    rows.sort(key=lambda r: r.get("ts", 0))
    total = 0.0
    for r in rows:
        cost = float(r.get("costCoins") or 0)
        total += cost
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(r.get('ts', 0)))}  "
              f"{r.get('shot', ''):<28} {r.get('status', '?'):<8} {cost:>6.0f} RHB  "
              f"{Path(r.get('output', '.')).name}")
    print(f"  合计 {total:.0f} RHB（{len(rows)} 条）")


# ---------------------------------------------------------------- CLI

def load_plan(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        err(f"plan 不存在: {p}")
    plan = json.loads(p.read_text())
    base = p.parent
    for k in ("media",):
        plan[k] = {s: (v if os.path.isabs(os.path.expanduser(v)) else str((base / v).resolve()))
                   for s, v in plan.get(k, {}).items()}
    for k in ("prompt_file", "output"):
        if plan.get(k) and not os.path.isabs(os.path.expanduser(plan[k])):
            plan[k] = str((base / plan[k]).resolve())
    return plan


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd, rest = sys.argv[1], sys.argv[2:]

    if cmd == "submit":
        plan = load_plan(rest[0])
        result = submit_with_backoff(plan)
        print(json.dumps(result, ensure_ascii=False, indent=1)[:2000])
        sys.exit(0 if result.get("ok") else 2)

    elif cmd == "validate":
        plan = load_plan(rest[0])
        problems = validate(plan)
        sys.exit(1 if problems else 0)

    elif cmd == "parallel":
        results = parallel([load_plan(p) for p in rest])
        for i, r in enumerate(results):
            tag = "✓" if r and r.get("ok") else "✗"
            print(f"[parallel #{i + 1}] {tag} "
                  f"{(r or {}).get('output') or (r or {}).get('error') or (r or {}).get('message', '')}")
        sys.exit(0 if all(r and r.get("ok") for r in results) else 2)

    elif cmd == "qc":
        report = qc(rest[0])
        print(json.dumps(report, ensure_ascii=False, indent=1))
        print(f"\nQC 摘要：{report['spec']['size']} / {report['spec']['duration']}s / "
              f"{report['voiced_count']} 个发声段 {report['voiced_segments']}")
        print(f"抽帧表：{report['contact_sheet']}\n人工项：看抽帧表 + 耳听音色")

    elif cmd == "tasks":
        tasks_cmd()

    elif cmd == "cancel":
        cancel_cmd(rest[0])

    elif cmd == "ledger":
        ledger_cmd(rest[0] if rest else "")

    elif cmd == "workflows":
        for f in sorted(WF_DIR.glob("*.json")):
            wf = json.loads(f.read_text())
            print(f"  {wf['id']}  {wf.get('name', '')}  target={wf.get('target')}  "
                  f"slots={list(wf.get('media_slots', {}))}")

    elif cmd == "settings":
        print(f"来源：{'用户 ' + str(SETTINGS_FILE) if SETTINGS_FILE.exists() else '内置默认'}")
        print(json.dumps(S, ensure_ascii=False, indent=1))
        print("覆盖顺序：settings.json < plan.json.policy < 命令行")

    else:
        err(f"未知子命令 {cmd}（可用：submit/parallel/qc/tasks/cancel/ledger/validate/workflows）")


if __name__ == "__main__":
    main()
