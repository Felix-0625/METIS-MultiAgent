"""
完整流程测试 — 数据可视化看板项目
从创建到启动阶段全链路验证
"""
import requests, json, sys, time
from datetime import datetime

API = "http://localhost:8000"
errors = []

def check(step, ok, msg=""):
    status = "[PASS]" if ok else "[FAIL]"
    print(f"  {status} {step}: {msg}")
    if not ok:
        errors.append(step)
    return ok

print("=" * 60)
print("MeTis 数据可视化项目全流程测试")
print(f"开始: {datetime.now().strftime('%H:%M:%S')}")
print("=" * 60)

# ═══ 1. 创建项目 ═══════════════════════════════════════
print("\n--- 1. 创建项目 ---")
r = requests.post(f"{API}/projects", json={
    "name": "数据可视化分析平台",
    "description": "支持从数据库/CSV/自然语言导入数据，Agent自动分析生成图表"
}).json()
pid = r.get("project_id", "")
check("1.1 创建项目", bool(pid), pid)
if not pid:
    sys.exit(1)

# ═══ 2. 模拟 PM 对话 + 生成方案 + 确认 ═══════════════════
print("\n--- 2. PM 对话流程 ---")

# 2.1 发送需求
r = requests.post(f"{API}/projects/{pid}/pm-team/chat", json={
    "message": """开发一个数据可视化分析平台，支持从多种来源导入数据并用 AI Agent 自动分析。

核心功能：
1. 数据导入 - 支持数据库连接(MySQL/PostgreSQL)、CSV/Excel文件上传、自然语言输入
2. AI Agent 分析 - 自然语言提问自动生成SQL/Python脚本，自动发现异常值和趋势
3. 可视化看板 - 核心指标卡片、趋势折线图、品类柱状图、数据表格、CSV导出
4. 用户系统 - 管理员创建看板和邀请成员，查看者只能看

技术栈：React+TypeScript+Ant Design+ECharts 前端，Python FastAPI+PostgreSQL 后端。
响应时间<2秒，支持50人并发。""",
    "history": []
}).json()
check("2.1 PM对话", "reply" in r or "response" in r or "success" in str(r)[:100] or "error" not in str(r).lower(),
     str(r)[:80])

# 2.2 生成方案草稿
r = requests.post(f"{API}/projects/{pid}/pm-team/synthesize", json={
    "requirements": "数据可视化分析平台",
    "fast_mode": True
}).json()
check("2.2 生成方案", "draft_plan" in str(r)[:200] or "plan" in str(r)[:200] or "error" not in str(r).lower(),
     str(r)[:80])

# 2.3 确认总规划（如返回 modified 则再确一次）
r = requests.post(f"{API}/projects/{pid}/pm-team/confirm-plan", json={
    "modifications": "确认规划"
}).json()
status = r.get("status", "")
if status == "modified":
    r = requests.post(f"{API}/projects/{pid}/pm-team/confirm-plan", json={
        "modifications": ""
    }).json()
    status = r.get("status", "")
plan_confirmed = status == "confirmed" or r.get("plan_confirmed")
check("2.3 确认规划", plan_confirmed,
     f"status={status}")

# ═══ 3. 验证子项目结构 ═════════════════════════════════
print("\n--- 3. 验证子项目 ---")
r = requests.get(f"{API}/projects/{pid}").json()
sps = r.get("subprojects", [])
check("3.1 子项目存在", len(sps) > 0, f"count={len(sps)}")

all_progress_ok = all(s.get("progress") is not None for s in sps) if sps else False
if sps:
    for i, s in enumerate(sps):
        pg = s.get("progress", "MISSING")
        print(f"    子项目{i}: {s.get('name','?')} progress={pg}")
check("3.2 progress字段", len(sps) == 0 or all_progress_ok,
     f"progress_ok={all_progress_ok}")

# ═══ 4. 验证阶段结构 ═════════════════════════════════
print("\n--- 4. 验证阶段 ---")
r = requests.get(f"{API}/projects/{pid}/phases").json()
phases = r.get("phases", [])
check("4.1 阶段列表", len(phases) > 0, f"count={len(phases)}")
if phases:
    p = phases[0]
    check("4.2 plan_reviews", "plan_reviews" in p or "review_chain_status" in p,
         f"has_reviews={'plan_reviews' in p}")

# ═══ 5. 启动阶段 ═════════════════════════════════════
print("\n--- 5. 启动阶段 ---")
if phases:
    pid_phase = phases[0].get("phase_id", "")
    if pid_phase:
        r = requests.post(f"{API}/projects/{pid}/phases/{pid_phase}/start", json={}).json()
        agents_created = len(r.get("created_agents", []))
        check("5.1 启动阶段", r.get("success", False) or agents_created > 0,
             f"agents={agents_created}" if r.get("success") else str(r)[:80])

        if agents_created > 0:
            first_agent = r["created_agents"][0]
            check("5.2 Agent has expert_id", "expert_id" in first_agent or "expert_type" in first_agent,
                 f"role={first_agent.get('role','?')}")
            check("5.3 Agent has lock_id", first_agent.get("lock_id") is not None,
                 "lock created" if first_agent.get("lock_id") else "MISSING lock_id")

# ═══ 6. 验证锁机制 ═════════════════════════════════
print("\n--- 6. ExpertLock 验证 ---")
r = requests.get(f"{API}/projects/{pid}/locks").json()
lock_count = r.get("count", 0)
check("6.1 锁表非空", lock_count > 0 if phases else True,
     f"locks={lock_count}")

# ═══ 7. 指标系统 ═════════════════════════════════
print("\n--- 7. 指标系统 ---")
r = requests.get(f"{API}/projects/{pid}/metrics").json()
check("7.1 指标可获取", isinstance(r, dict), "ok")

r = requests.get(f"{API}/projects/{pid}/qc/results").json()
check("7.2 QC结果", isinstance(r, dict), "ok")

# ═══ 8. 全栈工程师 ═════════════════════════════════
print("\n--- 8. 全栈工程师 ---")
r = requests.get(f"{API}/engineer/{pid}/status")
check("8.1 工程师状态", r.status_code == 200, f"HTTP {r.status_code}")

r = requests.get(f"{API}/engineer/{pid}/defects")
check("8.2 缺陷列表", r.status_code == 200, f"HTTP {r.status_code}")

# ═══ 9. 清理 ════════════════════════════════════
print("\n--- 9. 清理 ---")
r = requests.delete(f"{API}/projects/{pid}").json()
check("9.1 删除项目", r.get("success", False), str(r)[:60])

# ═══ 总结 ════════════════════════════════════════
print(f"\n{'='*60}")
print(f"结束: {datetime.now().strftime('%H:%M:%S')}")
if errors:
    print(f"[FAIL] {len(errors)} failures:")
    for e in errors:
        print(f"   - {e}")
    sys.exit(1)
else:
    print("[PASS] 全流程通过")