#!/usr/bin/env python3
"""
Kanban Watchdog — 多 Agent 流水线守护脚本

四大核心功能（稳定，默认启用）：
1. 阻塞启动：auditor blocked + 有修复方案任务依赖它 → 自动解绑并启动修复方案
2. 返工桥接：修复方案 done → 自动创建 builder 修复执行任务并链接到 auditor
3. 自动复审：修复执行 done → 自动 unblock auditor 进入复审
4. 完成汇报：看板全部 done → 发桌面通知

用法:
    python3 kanban_watchdog.py --board <board-slug> [--daemon] [--interval 60]

设计：纯脚本，不花 LLM 钱。
- 方式 A（cron）：每分钟跑一次，--once（默认）
- 方式 B（daemon）：常驻后台
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


# ========== Kanban 操作 ==========

def kanban(board, subcmd, *args, parse_json=False):
    """执行 hermes kanban 命令"""
    env = os.environ.copy()
    env["HERMES_KANBAN_BOARD"] = board
    # 确保 Python 等路径也能用
    env["PATH"] = f"/usr/local/bin:/opt/homebrew/bin:{env.get('PATH', '')}"

    cmd = [HERMES_BIN, "kanban", subcmd] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    except Exception as e:
        print(f"[ERROR] kanban {subcmd} 执行失败: {e}", file=sys.stderr)
        return None

    if result.returncode != 0 and result.stderr.strip():
        # 有些命令 stderr 有警告但其实成功了，只在 stdout 空时打印
        if not result.stdout.strip():
            print(f"[WARN] kanban {subcmd}: {result.stderr.strip()[:200]}", file=sys.stderr)

    out = result.stdout.strip()

    if parse_json and out:
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return out


def list_tasks(board):
    """列出所有任务，返回 list of dict"""
    # 先尝试 --json（如果支持）
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
        # 格式: ▶ t_xxx  status  assignee  title
        # 图标字符可能是 ▶ ◻ ✓ ⊘ ● 等
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        # 跳过第一列的状态图标，取后三列
        status_icon, tid, status = parts[0], parts[1], parts[2]
        rest = parts[3]
        # rest 里 assignee 和 title 再拆一次
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


def get_task_deps(board, task_id):
    """获取任务的依赖关系（父任务列表），返回 list of task_id"""
    # 用 kanban show 看详情
    out = kanban(board, "show", task_id)
    if not out:
        return []
    # 解析父任务行
    # 格式可能是:
    #   parents:   t_xxx, t_yyy
    #   Depends on: t_xxx
    #   depends on: t_xxx
    deps = []
    import re
    for line in out.split("\n"):
        line_stripped = line.strip()
        if (line_stripped.startswith("parents:") or
            line_stripped.startswith("Depends on:") or
            line_stripped.startswith("depends on:")):
            deps = re.findall(r't_[a-f0-9]+', line_stripped)
            break
    return deps


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


def unlink_tasks(board, parent_id, child_id):
    """解除 parent → child 依赖"""
    kanban(board, "unlink", parent_id, child_id)


def promote_task(board, task_id, force=False):
    """提升任务状态 todo → ready"""
    args = ["promote", task_id]
    if force:
        args.append("--force")
    kanban(board, *args)


def unblock_task(board, task_id, reason=""):
    """解除阻塞"""
    args = ["unblock", task_id]
    if reason:
        args.append(reason)
    kanban(board, *args)


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
        "unblocked_plan_ids": [],   # 已解绑启动的修复方案任务 ID
        "bridged_plan_ids": [],     # 已桥接的修复方案任务 ID
        "completed_tasks": [],      # 上次检查时 done 的任务 ID 列表
        "notified_completion": False,  # 上一次是否是完成状态
        "last_check": 0
    }


def save_state(board, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / f"{board}.json"
    state["last_check"] = int(time.time())
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ========== 功能 1：阻塞启动 ==========

def check_blocked_start(board, tasks, state):
    """
    检测 auditor blocked + 有 architect 修复方案任务依赖它（todo 状态）
    → 解绑依赖 + 强制 promote，让修复方案立即开始

    背景：auditor block 时会自己创建一个后续任务，但把自己设为父依赖，
    导致修复任务永远无法启动（父任务是 blocked，不是 done）。
    """
    # 找 auditor 阻塞任务
    blocked_auditors = [t for t in tasks
                        if t["assignee"] == "auditor" and t["status"] == "blocked"]
    if not blocked_auditors:
        return 0

    # 找 todo 状态的 architect 修复方案任务
    plan_tasks = [t for t in tasks
                   if t["assignee"] == "architect"
                   and "修复方案" in t["title"]
                   and t["status"] == "todo"
                   and t["id"] not in state["unblocked_plan_ids"]]

    if not plan_tasks:
        return 0

    unblocked = 0
    for plan in plan_tasks:
        # 检查这个修复方案的父依赖是不是 blocked auditor
        deps = get_task_deps(board, plan["id"])
        has_blocked_parent = any(d in [a["id"] for a in blocked_auditors] for d in deps)

        if has_blocked_parent or not deps:
            # 有 blocked 父依赖，或者没有依赖但状态是 todo（也可能是 auditor 建的）
            # → 解绑 + 强制 promote
            print(f"[启动修复] 发现阻塞的修复方案任务: {plan['id']} ({plan['title']})")

            # 解除和 blocked auditor 的依赖
            for dep_id in deps:
                if dep_id in [a["id"] for a in blocked_auditors]:
                    print(f"       解绑依赖: {dep_id} → {plan['id']}")
                    unlink_tasks(board, dep_id, plan["id"])

            # 强制提升到 ready
            promote_task(board, plan["id"], force=True)
            print(f"       已 promote 到 ready 状态")

            # 找对应的 auditor 任务加评论
            auditor = blocked_auditors[0]
            comment_task(board, auditor["id"],
                         f"Watchdog 自动启动修复流程：修复方案任务 {plan['id']} 已解除阻塞依赖并开始执行。"
                         f"方案完成后将自动创建 builder 修复执行任务。")

            state["unblocked_plan_ids"].append(plan["id"])
            unblocked += 1

    return unblocked


# ========== 功能 2：返工桥接 ==========

def check_repair_bridge(board, tasks, state):
    """
    检测到 architect 修复方案 done → 创建 builder 修复执行任务
    修复执行完成后会自动触发 auditor 复审（因为 exec → auditor 有 link）
    """
    # 找 auditor 阻塞任务
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
        # 找对应的 auditor 任务（取第一个阻塞的，简化处理）
        auditor = blocked_auditors[0]

        # 标题：去掉「修复方案：」前缀，加「修复执行：」
        short_name = plan["title"].replace("修复方案：", "").strip()
        exec_title = f"修复执行：{short_name}"

        # 检查是否已经有同名 builder 任务了
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
            # 链接依赖：plan → exec（exec 依赖 plan 的完成）
            link_tasks(board, plan["id"], exec_id)
            # 链接：exec → auditor（auditor 等 exec 完成后自动复审）
            link_tasks(board, exec_id, auditor["id"])

            # 给 auditor 加评论通知
            comment_task(board, auditor["id"],
                         f"自动桥接：已创建 builder 修复执行任务 {exec_id}（{exec_title}）。"
                         f"该任务完成后自动进入复审。")

            state["bridged_plan_ids"].append(plan["id"])
            bridged += 1
            print(f"       任务 ID: {exec_id}，已链接到 auditor 任务 {auditor['id']}")
        else:
            print(f"       ✗ 创建失败")

    return bridged


# ========== 功能 3：自动复审 ==========

def check_auto_unblock(board, tasks, state):
    """
    检测到 builder 修复执行 done + 对应的 auditor 还 blocked
    → 自动 unblock auditor，进入复审

    背景：kanban dispatcher 不会自动 unblock blocked 任务（即使父依赖都 done）。
    blocked 是主动阻塞状态，需要显式 unblock。

    防过早触发（v1.6.0）：auditor 首轮 block 时，父依赖里可能只有原始任务（如
    "代码实现"）已 done，但修复链（修复方案→修复执行）还没建立。此时若按
    "所有父依赖 done" 直接 unblock，auditor 复审会看到未修复的代码再次 block，
    形成空转循环。修复：只有当 auditor 的父依赖里存在 **done 的修复执行任务**
    时才 unblock——即修复确实被执行完，才放行复审。
    """
    if "unblocked_auditor_ids" not in state:
        state["unblocked_auditor_ids"] = []

    # 找 blocked 的 auditor 任务
    blocked_auditors = [t for t in tasks
                        if t["assignee"] == "auditor" and t["status"] == "blocked"
                        and t["id"] not in state["unblocked_auditor_ids"]]
    if not blocked_auditors:
        return 0

    unblocked = 0
    for auditor in blocked_auditors:
        # 获取 auditor 的父任务
        deps = get_task_deps(board, auditor["id"])
        if not deps:
            continue

        dep_tasks = {t["id"]: t for t in tasks}

        # 关键判断：父依赖里必须存在 done 的「修复执行」任务
        # （说明修复确实执行完了，可以复审）
        # 首轮 block 后修复链未建立时，父依赖里没有修复执行任务 → 不 unblock
        repair_execs_done = [
            dep_id for dep_id in deps
            if dep_id in dep_tasks
            and "修复执行" in dep_tasks[dep_id]["title"]
            and dep_tasks[dep_id]["status"] == "done"
        ]

        if not repair_execs_done:
            # 没有已完成修复执行任务 → 不触发（可能是首轮 block 等修复方案，
            # 或修复执行还在 running）
            continue

        # 所有父依赖都 done（含修复执行 done）→ 自动 unblock 进入复审
        all_deps_done = all(
            dep_id in dep_tasks and dep_tasks[dep_id]["status"] == "done"
            for dep_id in deps
        )
        if not all_deps_done:
            continue

        print(f"[复审] builder 修复执行完成，自动 unblock auditor 任务: {auditor['id']}")
        unblock_task(board, auditor["id"], "watchdog: 修复执行任务已完成，自动进入复审")
        comment_task(board, auditor["id"],
                     "Watchdog 自动触发复审：修复执行任务已完成，请重新审计。")
        state["unblocked_auditor_ids"].append(auditor["id"])
        unblocked += 1

    return unblocked


# ========== 功能 4：完成汇报 ==========

def check_completion(board, tasks, state):
    """
    看板全部 done 时发通知。
    如果上次是完成状态但这次有新任务（状态重置），下一次完成时再发。
    """
    active = [t for t in tasks if t["status"] not in ("archived",)]
    if not active:
        return False

    done_ids = sorted(t["id"] for t in active if t["status"] == "done")
    all_done = len(done_ids) == len(active)

    # 上次不是完成状态，这次是 → 发通知
    if all_done and not state["notified_completion"]:
        print(f"[完成] 看板 {board} 全部完成（{len(done_ids)} 个任务）")
        send_notification(
            title=f"✅ Kanban 完成：{board}",
            body=f"共 {len(done_ids)} 个任务全部完成。"
        )
        state["notified_completion"] = True
        state["completed_tasks"] = done_ids
        return True

    # 上次是完成状态，这次不是 → 重置（有新任务开始了）
    if not all_done and state["notified_completion"]:
        state["notified_completion"] = False
        print(f"[重置] 看板 {board} 有新任务，完成通知已重置")

    return False


def send_notification(title, body):
    """发送桌面通知"""
    try:
        # 转义双引号
        safe_title = title.replace('"', '\\"')
        safe_body = body.replace('"', '\\"')
        script = f'display notification "{safe_body}" with title "{safe_title}"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        return True
    except Exception as e:
        print(f"[WARN] 通知发送失败: {e}", file=sys.stderr)
        return False


# ========== 主流程 ==========

def run_check(board):
    """执行一次检查"""
    state = load_state(board)
    # 兼容旧状态文件：补齐缺失的字段
    state.setdefault("unblocked_plan_ids", [])
    state.setdefault("bridged_plan_ids", [])
    state.setdefault("unblocked_auditor_ids", [])
    state.setdefault("completed_tasks", [])
    state.setdefault("notified_completion", False)
    tasks = list_tasks(board)

    if not tasks:
        print(f"[INFO] 看板 {board} 无任务")
        save_state(board, state)
        return

    # 1. 阻塞启动：让被卡住的修复方案跑起来
    unblocked = check_blocked_start(board, tasks, state)

    # 2. 返工桥接：修复方案完成 → 创建 builder 修复执行
    bridged = check_repair_bridge(board, tasks, state)

    # 3. 自动复审：builder 修复执行 done → auditor unblock
    auto_unblocked = check_auto_unblock(board, tasks, state)

    # 4. 完成汇报：全部 done → 通知
    completed = check_completion(board, tasks, state)

    save_state(board, state)

    if unblocked or bridged or auto_unblocked or completed:
        print(f"[OK] 检查完成：启动修复 {unblocked} 个，桥接 {bridged} 个，自动复审 {auto_unblocked} 个，完成通知 {'已发送' if completed else '未触发'}")


def main():
    parser = argparse.ArgumentParser(description="Kanban Watchdog")
    parser.add_argument("--board", required=True, help="看板 slug")
    parser.add_argument("--daemon", action="store_true", help="守护进程模式")
    parser.add_argument("--interval", type=int, default=60, help="守护模式间隔（秒）")
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
