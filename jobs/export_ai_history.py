#!/usr/bin/env python3
"""导出 CloudBase ai_requests/ai_replies → 本机（原始 JSON 备份 + pi 会话 + 主库）。

用法：
  python3 export_ai_history.py check     # 只预检（拉取统计，不写任何东西）
  python3 export_ai_history.py run       # 执行完整导出
"""
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENV_PATH = Path("/home/ubuntu/stock-ai/.env")
BASE = ("https://ljx-d1gjpcu23fa094e67.api.tcloudbasegateway.com"
        "/v1/database/instances/(default)/databases/(default)")
RAW_DIR = Path("/home/ubuntu/backup/cloudbase-export-20260918")
PI_SESSIONS = Path("/home/ubuntu/stock-ai/.pi-sessions")
MAIN_DB = Path("/home/ubuntu/stock-alert/data/stockdesk.db")

MODEL_MAP = {  # 云端请求的 model 字段 → pi 会话 model_change 的 provider/modelId
    "oczen/deepseek-v4-flash": ("oczen", "deepseek-v4-flash"),
    "kimi/k3": ("kimi", "k3"),
}


def load_key() -> str:
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("CB_API_KEY="):
            return line.split("=", 1)[1].strip()
    sys.exit("CB_API_KEY not found in .env")


def fetch_all(coll: str) -> list[dict]:
    out, offset = [], 0
    while True:
        req = urllib.request.Request(
            f"{BASE}/collections/{coll}/documents?limit=1000&offset={offset}",
            headers={"Authorization": "Bearer " + load_key()})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
        batch = j.get("list", [])
        out.extend(batch)
        if len(batch) < 1000:
            return out


def ms_to_ms(v):
    """created_at 兼容多形态：{$date:{$numberLong}}、{$numberLong}、ISO 串、毫秒数(含数字串)。"""
    if isinstance(v, dict):
        v = v.get("$date", v)
        if isinstance(v, dict):
            v = v["$numberLong"]
    if isinstance(v, str):
        if v.isdigit():  # $numberLong 解包出的毫秒字符串
            return int(v)
        return int(datetime.fromisoformat(v).timestamp() * 1000)
    return int(v)


def ms_iso(ms) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def pi_fname(t_iso: str, sid: str) -> str:
    """pi 会话文件名格式：2026-09-01T07-42-44-395Z_<session-uuid>.jsonl"""
    return t_iso.replace(":", "-") + f"_{sid}.jsonl"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    reqs = sorted(fetch_all("ai_requests"), key=lambda r: ms_to_ms(r["created_at"]))
    reps = fetch_all("ai_replies")
    print(f"拉取: {len(reqs)} 请求 / {len(reps)} 回复")

    # 按 session 聚合；stop 控制消息不产生对话内容，跳过
    sessions = {}
    for r in reqs:
        if r.get("mode") == "stop":
            continue
        sid = r.get("session_id") or "no-session"
        sessions.setdefault(sid, []).append(r)
    req_ids = {r["_id"] for r in reqs}
    orphan = [r for r in reps if r.get("request_id") not in req_ids]
    print(f"会话数: {len(sessions)}（stop 消息已跳过）")
    if orphan:
        print(f"⚠️ 孤儿回复 {len(orphan)} 条（无对应请求，仅入原始备份）")

    if mode == "check":
        for sid, items in sorted(sessions.items()):
            d0 = ms_iso(ms_to_ms(items[0]["created_at"]))[:10]
            d1 = ms_iso(ms_to_ms(items[-1]["created_at"]))[:10]
            exists = (PI_SESSIONS / sid).exists()
            print(f"  {'[已有]' if exists else '[新增]'} {sid}  "
                  f"{len(items)}问  {d0}~{d1}  首问: {items[0].get('question','')[:30]}")
        return

    # ---- 1) 原始 JSON 备份 ----
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    (RAW_DIR / f"ai_requests_{stamp}.json").write_text(
        json.dumps(reqs, ensure_ascii=False, indent=1))
    (RAW_DIR / f"ai_replies_{stamp}.json").write_text(
        json.dumps(reps, ensure_ascii=False, indent=1))
    print(f"原始备份: {RAW_DIR}/ai_*.json（{stamp}）")

    # ---- 2) 重建 pi 会话（只补缺失目录，不碰已有）----
    rep_by_id = {r.get("request_id"): r for r in reps}
    new_sess = skip_sess = 0
    for sid, items in sorted(sessions.items()):
        d = PI_SESSIONS / sid
        if d.exists():
            skip_sess += 1
            continue
        d.mkdir(parents=True)
        t0 = ms_iso(ms_to_ms(items[0]["created_at"]))
        fpath = d / pi_fname(t0, sid)
        with fpath.open("w") as f:
            f.write(json.dumps({"type": "session", "version": 3,
                                "id": f"migr-{sid[:8]}", "timestamp": t0,
                                "cwd": "/home/ubuntu/stock-ai",
                                "note": "migrated from CloudBase ai_requests 2026-09-18"},
                               ensure_ascii=False) + "\n")
            provider, model = MODEL_MAP.get(items[0].get("model") or "", ("kimi", "k3"))
            f.write(json.dumps({"type": "model_change", "id": "mc0001",
                                "parentId": None, "timestamp": t0,
                                "provider": provider, "modelId": model}) + "\n")
            # 先收集全部问答消息，再按真实时间戳排序输出（用户在上一答完成前
            # 重试提问时，问答会交错，严格按时间排序才能保持单调）
            events = []
            for it in items:
                u_id = "u" + it["_id"][:7]
                a_id = "a" + it["_id"][:7]
                events.append((ms_to_ms(it["created_at"]), "u", u_id, {
                    "type": "message", "id": u_id,
                    "message": {"role": "user", "content": [
                        {"type": "text", "text": it.get("question", "")}]}}))
                rep = rep_by_id.get(it["_id"])
                if rep and (rep.get("text") or rep.get("thinking")):
                    content = []
                    if rep.get("thinking"):
                        content.append({"type": "thinking",
                                        "thinking": rep["thinking"]})
                    if rep.get("text"):
                        content.append({"type": "text", "text": rep["text"]})
                    events.append((ms_to_ms(rep.get("updated_at") or rep["created_at"]),
                                   "a", a_id, {
                        "type": "message", "id": a_id,
                        "message": {"role": "assistant", "content": content}}))
            events.sort(key=lambda e: int(e[0]))
            parent = "mc0001"
            for ts_ms, _, mid, payload in events:
                payload["parentId"] = parent
                payload["timestamp"] = ms_iso(int(ts_ms))
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                parent = mid
        new_sess += 1
    print(f"pi 会话: 新建 {new_sess}，跳过已存在 {skip_sess}")

    # ---- 3) 同步入本机主库（幂等，按主键去重）----
    conn = sqlite3.connect(MAIN_DB)
    cur = conn.cursor()
    ins_r = ins_p = 0
    for r in reqs:
        if cur.execute("SELECT 1 FROM ai_requests WHERE id=?",
                       (r["_id"],)).fetchone():
            continue
        ca = ms_to_ms(r["created_at"])
        ua = ms_to_ms(r.get("updated_at") or r["created_at"])
        cur.execute(
            "INSERT INTO ai_requests (id,mode,question,session_id,status,"
            "model,skill,target_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["_id"], r.get("mode"), r.get("question"), r.get("session_id"),
             r.get("status"), r.get("model"), r.get("skill"),
             r.get("target_id"), ca, ua))
        ins_r += 1
    for r in reps:
        if cur.execute("SELECT 1 FROM ai_replies WHERE request_id=?",
                       (r["request_id"],)).fetchone():
            continue
        ca = ms_to_ms(r["created_at"])
        ua = ms_to_ms(r.get("updated_at") or r["created_at"])
        cur.execute(
            "INSERT INTO ai_replies (request_id,text,thinking,done,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (r["request_id"], r.get("text"), r.get("thinking"),
             1 if r.get("done") else 0, ca, ua))
        ins_p += 1
    conn.commit()
    print(f"主库: 新增 {ins_r} 请求 / {ins_p} 回复 → {MAIN_DB}")
    conn.close()
    print("完成。云端数据已三处落地：原始 JSON / pi 会话 / 本机主库")


if __name__ == "__main__":
    main()
