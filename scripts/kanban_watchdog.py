#!/usr/bin/env python3
"""Kanban 健康检查 watchdog — 每 30 分钟检查一次 esp32-deepseek-monitor 看板。

检测两类异常并输出告警（stdout 非空 = 有告警，cron 会投递）：
1. blocked 状态超过 30 分钟的任务（可能是审计不通过被 block 后没人处理，或卡死）
2. 最近的审计/复审任务已 complete 但结果含"不通过/P0/P1"（说明返工循环没自动触发）

正常时 stdout 为空（cron --no-agent 模式静默）。
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

BOARD = sys.argv[1] if len(sys.argv) > 1 else "<your-board-slug>"
STALE_BLOCK_MINUTES = 30
AUDIT_KEYWORDS = ("不通过", "P0", "P1", "审计不通过", "复审计不通过")

def run_kanban(*args):
    try:
        r = subprocess.run(
            ["hermes", "kanban", *args],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout, r.returncode
    except Exception as e:
        return str(e), -1

def main():
    # 1. 列出任务
    out, rc = run_kanban("list", "--json")
    if rc != 0:
        print(f"[kanban-watch] 看板列表失败: {out[:200]}")
        return
    try:
        tasks = json.loads(out)
    except json.JSONDecodeError:
        print(f"[kanban-watch] 看板 JSON 解析失败: {out[:200]}")
        return

    now = time.time()
    alerts = []

    # 2. 检查 blocked 任务
    for t in tasks:
        if t.get("status") == "blocked":
            # 找 block 时间（用 updated_at 或 started_at 近似）
            updated = t.get("updated_at") or t.get("blocked_at") or t.get("started_at") or 0
            if updated:
                try:
                    age_min = (now - float(updated)) / 60
                except (TypeError, ValueError):
                    age_min = 0
                if age_min > STALE_BLOCK_MINUTES:
                    alerts.append(
                        f"⚠️ 任务被阻塞超过 {int(age_min)} 分钟无人处理: "
                        f"{t['id']} [{t.get('assignee','?')}] {t.get('title','')}"
                    )

    # 3. 检查最近审计/复审任务的结果
    for t in tasks:
        title = t.get("title", "")
        if "审计" not in title and "复审" not in title:
            continue
        result = t.get("result") or ""
        if any(k in result for k in AUDIT_KEYWORDS):
            # 确认是否有后续修复方案任务（说明循环已触发）
            followup = False
            for other in tasks:
                if other.get("id") != t.get("id"):
                    # 粗略判断：修复方案任务标题含"修复方案"且未被完成
                    if "修复方案" in other.get("title", "") and other.get("status") in ("todo", "ready", "running", "blocked"):
                        followup = True
                        break
            if not followup:
                alerts.append(
                    f"⚠️ 审计未通过但未发现修复方案任务在跟进: "
                    f"{t['id']} {title} → 结果: {result[:80]}"
                )

    # 4. 输出（非空才投递）
    if alerts:
        print(f"[kanban-watch] {datetime.now().strftime('%H:%M')} 发现 {len(alerts)} 个问题:")
        for a in alerts:
            print(a)

if __name__ == "__main__":
    main()
