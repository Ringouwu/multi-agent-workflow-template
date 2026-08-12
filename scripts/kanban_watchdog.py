#!/usr/bin/env python3
"""
Kanban Watchdog — 多 Agent 流水线守护脚本

三大功能（按重要性排序）：
1. 🔗 **返工桥接**：auditor 发现问题 → architect 出修复方案 → [自动创建 builder 修复执行任务] → auditor 复审
   （解决了原生流水线中「方案做完了没人通知 builder 干活」的断链问题）

2. ✅ **完成汇报**：看板全部任务 done → 自动发桌面通知
   （不用手动来问「做完了吗」）

3. 🚨 **健康检查**：blocked 超 30 分钟 / 审计不通过但无跟进 → 告警
   （兜底，发现桥接失效或其他异常）

用法:
    # 方式 A（推荐，cron）：每分钟跑一次，用完即走
    python3 kanban_watchdog.py --board <board-slug>

    # 方式 B（daemon）：常驻后台
    python3 kanban_watchdog.py --board <board-slug> --daemon --interval 60

设计原则：纯脚本，不花 LLM 钱，不依赖第三方服务。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ========== 配置 ==========

# hermes 完整路径（cron 环境 PATH 不完整）
HERMES_BIN = str(Path.home() / ".hermes-web-ui/desktop-runtime/hermes/0.18.0/mac-arm64/python/bin/hermes")

# 状态文件目录
STATE_DIR = Path.home() / ".hermes" / "kanban-watchdog"

# 健康检查阈值
STALE_BLOCK_MINUTES = 30  # blocked 超过多久算异常


# ========== Kanban 操作 ==========

def kanban(board, subcmd, *args, parse_json=False):
    """执行 hermes kanban 命令"""
    env = os.environ.copy()
    env["HERMES_KANBAN_BOARD"] = board
    env["PATH"] = f"/usr/local/bin:/opt/homebrew/bin:{env.get('PATH', '')}"

    cmd = [HERMES_BIN, "kanban", subcmd] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    except Exception as e:
        print(f"[ERROR] kanban {subcmd} 执行失败: {e}", file=sys.stderr)
        return None

    out = result.stdout.strip()

    if parse_json and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return out


def list_tasks(board):
    """列出所有任务，返回 list of dict"""
    data = kanban(board, "list", "--json", parse_json=True)
    if isinstance(data, dict) and "tasks" in data:
        return data["tasks"]
    if isinstance(data, list):
        return data

    # 退回文本解析
    out = kanban(board, "list")
    if not out:
        return []

    tasks = []
    for line in out.split("\n"):
        line = line.strip()
        if not line or line.startswith("Board:") or line.startswith("SLUG"):
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        _, tid, status = parts[0], parts[1], parts[2]
        rest = parts[3]
        rest_parts = rest.split(None, 1)
        assignee = rest_parts[0] if rest_parts else ""
        title = rest_parts[1] if len(rest_parts) > 1 else ""

        if tid.startswith("t_"):
            tasks.append({
                "id": tid,
                "status": status,
                "assignee": assignee,
                "title": title
            })
    return tasks


def create_task(board, title, assignee, body="", priority=8):
    """创建任务，返回任务 ID 或 None"""
    data = kanban(
        board, "create", title,
        "--assignee", assignee,
        "--priority", str(priority),
        "--body", body,
        "--json",
        parse_json=True
    )
    if isinstance(data, dict):
        return data.get("id")
    return None


def link_tasks(board, parent_id, child_id):
    """建立 parent → child 依赖"""
    kanban(board, "link", parent_id, child_id)


def comment_task(board, task_id, text):
    """添加评论"""
    kanban(board, "comment", task_id, text)


# ========== 状态持久化 ==========

def load_state(board):
    state_file = STATE_DIR / f"{board}.json"
    if state_file.exists():
        try:
            with open(state_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "bridged_plan_ids": [],     # 已桥接的修复方案任务 ID
        "notified_completion": False,  # 上一次是否是完成状态
        "last_check": 0
    }


def save_state(board, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{board}.json"
    state["last_check"] = int(time.time())
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ========== 功能 1：返工桥接 ==========

def check_repair_bridge(board, tasks, state):
    """
    检测到 auditor blocked + 修复方案 done → 自动创建 builder 修复任务并链接

    为什么需要这个：
      Kanban 原生的审计不通过返工流程是：auditor block + 创建修复方案任务给 architect。
      但 architect 出完方案后，没有机制自动触发 builder 去执行修复，导致闭环断开。
      这个 watchdog 就是补上这一环。
    """
    blocked_auditors = [t for t in tasks
                        if t["assignee"] == "auditor" and t["status"] == "blocked"]
    if not blocked_auditors:
        return 0

    # 找已完成的修复方案任务（还没桥接过的）
    plan_tasks = [t for t in tasks
                   if t["assignee"] == "architect"
                   and "修复方案" in t["title"]
                   and t["status"] == "done"
                   and t["id"] not in state["bridged_plan_ids"]]

    if not plan_tasks:
        return 0

    bridged = 0
    for plan in plan_tasks:
        auditor = blocked_auditors[0]
        short_name = plan["title"].replace("修复方案：", "").strip()
        exec_title = f"修复执行：{short_name}"

        # 检查是否已有同名 builder 任务
        existing = [t for t in tasks
                    if t["assignee"] == "builder" and exec_title == t["title"]]
        if existing:
            state["bridged_plan_ids"].append(plan["id"])
            continue

        print(f"[桥接] 创建修复执行任务: {exec_title}")

        exec_id = create_task(
            board, exec_title,
            assignee="builder",
            body=f"按照 architect 修复方案（任务 {plan['id']}）执行代码修复。\n\n方案标题：{plan['title']}\n\n修复完成后提交，auditor 会自动复审。",
            priority=9
        )

        if exec_id:
            link_tasks(board, plan["id"], exec_id)  # 方案 → 执行
            link_tasks(board, exec_id, auditor["id"])  # 执行 → 审计（等执行完成自动复审）

            comment_task(board, auditor["id"],
                         f"🐶 watchdog 自动桥接：已创建 builder 修复执行任务 {exec_id}（{exec_title}）。"
                         f"该任务完成后自动进入复审。")

            state["bridged_plan_ids"].append(plan["id"])
            bridged += 1
            print(f"       任务 ID: {exec_id}，已链接到 auditor 任务 {auditor['id']}")
        else:
            print(f"       ✗ 创建失败")

    return bridged


# ========== 功能 2：完成汇报 ==========

def check_completion(board, tasks, state):
    """看板全部 done 时发桌面通知。新任务开始后自动重置。"""
    active = [t for t in tasks if t["status"] not in ("archived",)]
    if not active:
        return False

    all_done = all(t["status"] == "done" for t in active)

    if all_done and not state["notified_completion"]:
        print(f"[完成] 看板 {board} 全部完成（{len(active)} 个任务）")
        send_notification(
            title=f"✅ Kanban 完成：{board}",
            body=f"共 {len(active)} 个任务全部完成。"
        )
        state["notified_completion"] = True
        return True

    if not all_done and state["notified_completion"]:
        state["notified_completion"] = False
        print(f"[重置] 看板 {board} 有新任务，完成通知已重置")

    return False


def send_notification(title, body):
    """发送桌面通知（macOS）"""
    try:
        safe_title = title.replace('"', '\\"')
        safe_body = body.replace('"', '\\"')
        script = f'display notification "{safe_body}" with title "{safe_title}"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        return True
    except Exception as e:
        print(f"[WARN] 通知发送失败: {e}", file=sys.stderr)
        return False


# ========== 功能 3：健康检查（兜底告警） ==========

def check_health(board, tasks, state):
    """
    兜底健康检查：
    - blocked 超过 30 分钟的任务（可能卡死了）
    - 审计不通过但没有任何修复方案任务跟进
    """
    alerts = []

    # 检查阻塞任务
    for t in tasks:
        if t.get("status") == "blocked":
            # 没有精确的 block 时间，用状态本身告警
            # （如果已经有修复方案在跑，说明流程是健康的，不算告警）
            has_followup = any(
                "修复方案" in other.get("title", "") or "修复执行" in other.get("title", "")
                for other in tasks
                if other.get("status") in ("todo", "ready", "running")
            )
            if not has_followup:
                alerts.append(
                    f"⚠️ 任务阻塞且无修复跟进: {t['id']} [{t.get('assignee','?')}] {t.get('title','')}"
                )

    if alerts:
        print(f"[告警] 发现 {len(alerts)} 个健康问题:")
        for a in alerts:
            print(f"       {a}")

    return len(alerts)


# ========== 主流程 ==========

def run_check(board):
    state = load_state(board)
    tasks = list_tasks(board)

    if not tasks:
        print(f"[INFO] 看板 {board} 无任务")
        save_state(board, state)
        return

    bridged = check_repair_bridge(board, tasks, state)
    completed = check_completion(board, tasks, state)
    alerts = check_health(board, tasks, state)

    save_state(board, state)

    if bridged or completed or alerts:
        print(f"[OK] 桥接 {bridged} 个 | 完成通知 {'已发送' if completed else '未触发'} | 告警 {alerts} 个")


def main():
    parser = argparse.ArgumentParser(description="Kanban Watchdog — 多 Agent 流水线守护脚本")
    parser.add_argument("--board", required=True, help="看板 slug")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式")
    parser.add_argument("--interval", type=int, default=60, help="守护模式间隔（秒），默认 60")
    args = parser.parse_args()

    if args.daemon:
        print(f"🐶 Kanban Watchdog 启动")
        print(f"   看板: {args.board}")
        print(f"   间隔: {args.interval}s")
        print(f"   状态: {STATE_DIR / args.board}.json")
        print()
        while True:
            try:
                run_check(args.board)
            except Exception as e:
                print(f"[ERROR] {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        run_check(args.board)


if __name__ == "__main__":
    main()
