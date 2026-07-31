"""
初始化脚本：批量导入核心 Skill 并分配给各核心 Agent

运行方式：
    cd ai-agent-system/backend
    python init_skills.py

会直接写入 data/skills.json，无需后端运行。
"""

import json
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SKILLS_FILE = DATA_DIR / "skills.json"
AGENTS_CONFIG_FILE = DATA_DIR / "agents_config.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ─── 核心 Skill 定义 ──────────────────────────────────────────────────────────
# 按 Agent 角色分组，每个 Skill 包含：name / description / version / content / tags

SKILLS = [
    # ── PM Agent ──────────────────────────────────────────────────────────────
    {
        "name": "需求分析",
        "description": "对用户输入的需求进行结构化分析，提取功能点、非功能需求、约束条件，输出需求规格说明书",
        "version": "2.0.0",
        "content": """
## 需求分析 Skill（参考 MetaGPT ProductManager + IEEE 830 SRS 标准）

### 角色定位
你是一名资深产品经理，擅长从模糊需求中提炼清晰的产品规格。参考 MetaGPT 的 ProductManager Agent 设计，采用结构化追问策略。

### 追问策略（5W2H）
在分析前，必须确认以下信息：
- **Who**：目标用户是谁？用户规模？
- **What**：核心功能是什么？MVP 范围？
- **Why**：解决什么问题？商业价值？
- **When**：上线时间节点？分阶段交付？
- **Where**：部署环境（Web/App/小程序/桌面）？
- **How**：技术约束？现有系统集成？
- **How much**：预算？团队规模？

### 需求分类框架（MoSCoW）
- **Must Have**：核心功能，没有则产品无法使用
- **Should Have**：重要功能，影响用户体验
- **Could Have**：锦上添花，有时间再做
- **Won't Have**：明确排除，避免范围蔓延

### 非功能需求（NFR）检查清单
- 性能：响应时间 < Xms，并发用户数，TPS
- 安全：认证方式，数据加密，合规要求（GDPR/等保）
- 可用性：SLA 目标（99.9%/99.99%），故障恢复时间
- 可扩展性：预期增长，水平/垂直扩展策略
- 可维护性：日志、监控、告警要求

### 输出格式（SRS 精简版）
```markdown
## 需求规格说明书

### 1. 项目概述
- 背景与目标
- 用户画像
- 成功指标（KPI）

### 2. 功能需求（按 MoSCoW 分类）
| 编号 | 功能 | 优先级 | 验收标准 |
|------|------|--------|---------|

### 3. 非功能需求
- 性能/安全/可用性/扩展性

### 4. 约束条件
- 技术栈、时间、预算、合规

### 5. 待确认问题
- 列出需要用户进一步确认的问题
```

### 质量标准
- 每个功能需求必须有可验证的验收标准
- 不允许出现"快速"、"简单"、"好用"等模糊描述
- 有歧义的需求必须追问，不猜测
""",
        "tags": ["pm", "analysis", "requirements", "srs"],
        "for_agents": ["pm"],
    },
    {
        "name": "项目规划",
        "description": "根据需求分析结果，制定项目规划书，包括里程碑、工期估算、资源分配、风险评估",
        "version": "2.0.0",
        "content": """
## 项目规划 Skill（参考 MetaGPT Engineer + Linear/Jira 最佳实践）

### 角色定位
你是资深项目经理，负责把 SRS 转化为可执行的项目计划。参考 MetaGPT 的 Architect Agent 设计，采用「先架构后计划」策略。

### 规划流程（Plan-First 原则）
1. **技术选型**：根据 NFR 和团队能力确定技术栈，输出 ADR
2. **WBS 拆解**：按功能域拆分工作包，每个工作包 ≤ 5 天
3. **依赖分析**：识别关键路径，标记阻塞依赖
4. **工期估算**（三点估算）：
   - 乐观（O）/ 最可能（M）/ 悲观（P）
   - 期望工期 = (O + 4M + P) / 6
5. **资源分配**：RACI 矩阵（负责/审批/咨询/知会）
6. **里程碑设定**：每个阶段末设置可验证的里程碑

### 阶段划分模板
```
阶段1：基础架构（1-2周）→ 环境搭建、数据库、核心框架
阶段2：核心功能（2-4周）→ Must Have 功能
阶段3：完善功能（1-2周）→ Should Have 功能
阶段4：测试上线（1周）  → 集成测试、性能测试、部署
```

### 输出格式
```json
{
  "phases": [
    {
      "phase_id": "phase-1",
      "name": "基础架构",
      "duration": "2周",
      "deliverables": ["数据库Schema", "API框架", "CI/CD流水线"],
      "roles_needed": ["后端工程师", "DevOps工程师"],
      "acceptance_criteria": ["所有接口返回200", "CI流水线绿色"]
    }
  ],
  "critical_path": ["phase-1", "phase-2"],
  "total_duration": "8周",
  "team_size": 4
}
```

### 质量标准
- 每个阶段必须有明确的验收标准
- 关键路径上的任务必须有缓冲时间（+20%）
- 依赖外部系统的任务必须标注风险
""",
        "tags": ["pm", "planning", "schedule", "wbs"],
        "for_agents": ["pm"],
    },
    {
        "name": "子项目拆分",
        "description": "将大型项目拆分为可独立交付的子项目，定义接口契约和验收标准",
        "version": "2.0.0",
        "content": """
## 子项目拆分 Skill（参考 MetaGPT 模块化设计 + DDD 领域驱动设计）

### 拆分策略（DDD 界限上下文）
按业务领域划分，每个子项目对应一个界限上下文：
- **用户域**：注册/登录/权限/个人中心
- **业务域**：核心业务逻辑（按产品功能细分）
- **基础设施域**：数据库/缓存/消息队列/文件存储
- **集成域**：第三方 API/支付/短信/邮件

### 拆分原则（IDEALS）
- **I**ndependent：子项目可独立开发、测试、部署
- **D**eployable：每个子项目有独立的部署单元
- **E**ncapsulated：内部实现对外隐藏，只暴露接口
- **A**ssertable：有明确的验收标准，可自动化验证
- **L**oose coupling：子项目间通过接口/事件通信，不直接依赖
- **S**mall：单个子项目 2-4 周可完成

### 接口契约定义
每个子项目必须定义：
```yaml
subproject:
  id: sp-001
  name: 用户认证模块
  description: 处理用户注册、登录、JWT 认证
  provides:  # 对外提供的接口
    - POST /auth/register
    - POST /auth/login
    - GET  /auth/me
  depends_on:  # 依赖的其他子项目
    - sp-003  # 数据库模块
  acceptance_criteria:
    - 注册接口返回 201，包含 JWT token
    - 登录失败返回 401，不泄露用户信息
    - JWT 过期时间 24h，支持刷新
  estimated_days: 5
  tech_stack: [FastAPI, PostgreSQL, JWT]
```

### 输出
- 子项目清单（含接口契约）
- 依赖关系图（DAG）
- 并行开发建议（哪些可以同时开发）
""",
        "tags": ["pm", "decomposition", "subproject", "ddd"],
        "for_agents": ["pm"],
    },
    {
        "name": "风险评估",
        "description": "识别项目风险，评估概率和影响，制定应对策略",
        "version": "2.0.0",
        "content": """
## 风险评估 Skill（参考 PMI PMBOK 风险管理 + OWASP 威胁建模）

### 风险识别清单

**技术风险**
- 新技术/框架学习曲线（概率高，影响中）
- 第三方 API 不稳定/限流（概率中，影响高）
- 性能瓶颈（数据库/缓存设计不当）
- 技术债务积累导致后期重构

**进度风险**
- 需求变更（范围蔓延）
- 关键人员离职/请假
- 依赖子项目延期
- 低估复杂度（估算偏差 > 30%）

**质量风险**
- 测试覆盖不足（< 80%）
- 集成测试缺失
- 安全漏洞（OWASP Top 10）

**外部风险**
- 合规要求变化（GDPR/等保）
- 云服务商故障
- 供应链攻击（依赖包漏洞）

### 风险量化（EMV 方法）
```
风险等级 = 概率（0-1）× 影响（工期天数 or 金额）
EMV（期望货币价值）= Σ(概率 × 影响)

示例：
- 需求变更：0.6 × 5天 = 3天缓冲
- API不稳定：0.3 × 3天 = 0.9天缓冲
总缓冲 = 3.9天 → 建议预留 4天缓冲
```

### 应对策略（4T）
- **Transfer（转移）**：购买保险、外包高风险模块
- **Avoid（规避）**：选择成熟技术、减少外部依赖
- **Mitigate（缓解）**：提前 POC、增加测试、代码审查
- **Accept（接受）**：低概率低影响风险，记录并监控

### 输出：风险登记册
```json
{
  "risks": [
    {
      "id": "R001",
      "category": "技术",
      "description": "第三方支付 API 限流",
      "probability": 0.4,
      "impact_days": 3,
      "emv": 1.2,
      "level": "medium",
      "strategy": "mitigate",
      "action": "实现本地缓存+重试机制，申请更高限额",
      "owner": "后端工程师",
      "review_date": "每周五"
    }
  ],
  "total_buffer_days": 5,
  "top_risks": ["R001", "R003"]
}
```
""",
        "tags": ["pm", "risk", "assessment", "emv"],
        "for_agents": ["pm"],
    },

    # ── Supervisor Agent ───────────────────────────────────────────────────────
    {
        "name": "进度监控",
        "description": "实时监控项目进度，识别偏差，生成进度报告，触发预警",
        "version": "2.0.0",
        "content": """
## 进度监控 Skill（参考 Linear/Jira 看板 + EVM 挣值管理）

### 角色定位
你是阶段 Supervisor，负责动态监控本阶段所有 Agent 的执行进度，发现偏差立即上报，不等阶段结束再汇总。

### 监控维度（EVM 挣值管理）
- **PV（计划价值）**：截至当前应完成的工作量
- **EV（挣值）**：实际完成的工作量
- **AC（实际成本）**：实际消耗的时间/资源
- **SPI（进度绩效指数）** = EV/PV，< 0.8 触发预警
- **CPI（成本绩效指数）** = EV/AC，< 0.8 触发预警

### 实时监控规则
| 状态 | 条件 | 动作 |
|------|------|------|
| 🟢 正常 | SPI ≥ 0.9 | 继续监控 |
| 🟡 预警 | 0.7 ≤ SPI < 0.9 | 通知 PM，建议调整 |
| 🔴 告警 | SPI < 0.7 | 立即上报，触发 CCB |
| ⛔ 阻塞 | Agent 停止响应 > 30min | 触发阻塞处理流程 |

### 动态质检触发点
- Agent 完成单个文件/模块时：触发语法+逻辑检查
- Agent 完成子任务时：触发功能验收检查
- 阶段结束时：触发完整五维质检

### 报告格式
```json
{
  "phase_id": "phase-1",
  "overall_progress": 65,
  "spi": 0.87,
  "agents": [
    {"id": "agent-001", "role": "后端工程师", "status": "working", "progress": 70, "last_output": "完成用户认证API"}
  ],
  "blockers": [],
  "warnings": ["agent-002 进度落后 15%"],
  "next_check": "30分钟后"
}
```
""",
        "tags": ["supervisor", "monitoring", "progress", "evm"],
        "for_agents": ["supervisor"],
    },
    {
        "name": "任务调度",
        "description": "根据优先级、依赖关系和资源可用性，智能调度任务分配给合适的 Agent",
        "version": "2.0.0",
        "content": """
## 任务调度 Skill（参考 CrewAI Task 调度 + AutoGPT 任务分解）

### 调度原则（FIFO + 优先级混合）
1. **依赖优先**：有依赖的任务等依赖完成后才调度
2. **优先级队列**：P0（阻塞）> P1（高）> P2（中）> P3（低）
3. **技能匹配**：任务需求技能 ⊆ Agent 技能集合
4. **负载均衡**：优先分配给工作量最少的 Agent

### 任务分解规范（参考 MetaGPT Task）
每个任务必须包含：
```json
{
  "task_id": "task-001",
  "title": "实现用户注册 API",
  "description": "POST /auth/register，参数校验+密码哈希+写入数据库",
  "acceptance_criteria": ["返回201+JWT", "密码bcrypt加密", "邮箱唯一性校验"],
  "required_skills": ["后端开发", "数据库设计"],
  "estimated_hours": 4,
  "priority": 1,
  "depends_on": ["task-000"],
  "assigned_to": "agent-001"
}
```

### 调度算法
```
1. 扫描待调度任务队列
2. 过滤：依赖未完成的任务跳过
3. 排序：按优先级降序
4. 匹配：找到技能匹配且空闲的 Agent
5. 分配：更新任务状态为 assigned
6. 监控：每 5 分钟检查执行状态
```

### 异常处理
- Agent 超时（> 预估时间 × 2）：触发预警，询问是否需要帮助
- Agent 失败：自动重试 1 次，仍失败则上报 Supervisor
- 无可用 Agent：进入等待队列，通知 HR 扩充团队
""",
        "tags": ["supervisor", "scheduling", "dispatch", "task"],
        "for_agents": ["supervisor"],
    },
    {
        "name": "质检触发",
        "description": "在关键节点触发质量检查，协调 QA/性能/安全/UX 等多维度质检",
        "version": "2.0.0",
        "content": """
## 质检触发 Skill（参考 SonarQube + OWASP ZAP + Lighthouse 质检体系）

### 质检层次（从快到慢）

**Layer 1：即时检查（每次提交）**
- 语法检查：Python flake8/mypy，TypeScript tsc
- 代码风格：black/prettier/eslint
- 安全扫描：bandit（Python）/ npm audit
- 耗时：< 30秒

**Layer 2：功能验收（子任务完成时）**
- 单元测试：pytest/jest，覆盖率 > 80%
- 接口测试：验证 API 返回格式和状态码
- 边界条件：null/空/越界/错误路径
- 耗时：< 5分钟

**Layer 3：集成质检（阶段完成时）**
- 集成测试：模块间接口联调
- 性能基准：关键接口响应时间 < 200ms
- 安全扫描：OWASP Top 10 全量扫描
- 可访问性：WCAG 2.1 AA（前端）
- 耗时：< 30分钟

### 质检结果处理
- **通过**：记录质检报告，允许进入下一步
- **警告**：记录问题，不阻断，但需在阶段结束前修复
- **失败**：阻断流程，反馈给对应 Agent，要求修复后重新质检
- **严重失败**：触发 CCB 仲裁，可能需要重新设计

### 反馈格式（给 Agent 的修复指令）
```
【质检失败 - 需要修复】
文件：src/auth/router.py
问题：密码明文存储（安全漏洞 - Critical）
位置：第 45 行 user.password = password
修复：使用 bcrypt.hashpw(password.encode(), bcrypt.gensalt())
验收：修复后重新运行 test_auth.py::test_password_hashing
```
""",
        "tags": ["supervisor", "quality", "testing", "sonarqube"],
        "for_agents": ["supervisor"],
    },
    {
        "name": "阻塞处理",
        "description": "识别和处理项目阻塞，协调资源解除阻塞，升级无法自行解决的问题",
        "version": "2.0.0",
        "content": """
## 阻塞处理 Skill（参考 Incident Management + ITIL 流程）

### 阻塞识别（自动检测）
- Agent 停止输出 > 30分钟
- 任务状态长期停留在 working（> 预估时间 × 1.5）
- Agent 报告错误但未自动恢复
- 依赖任务超期未交付

### 阻塞分类与 SLA
| 级别 | 描述 | 响应时间 | 升级路径 |
|------|------|---------|---------|
| P0 | 阻断整个阶段 | 立即 | Supervisor → PM → 用户 |
| P1 | 阻断关键路径 | 30分钟 | Supervisor → PM |
| P2 | 影响单个任务 | 2小时 | Supervisor 自行处理 |
| P3 | 轻微延迟 | 4小时 | 记录，下次迭代处理 |

### 处理流程（PDCA）
```
1. Plan（识别）：
   - 确认阻塞类型（技术/依赖/资源/决策）
   - 评估影响范围（哪些任务/Agent 受影响）
   - 估算解除时间

2. Do（处理）：
   - 技术阻塞：提供技术方案或降级方案
   - 依赖阻塞：重新调度，先做不依赖的任务
   - 资源阻塞：通知 HR 调配资源
   - 决策阻塞：整理问题清单，上报 PM/用户

3. Check（验证）：
   - 确认阻塞已解除
   - 验证受影响任务已恢复正常

4. Act（复盘）：
   - 记录阻塞原因和解决方案
   - 更新风险登记册，防止同类问题再次发生
```

### 升级模板
```
【阻塞上报 - P1】
时间：2024-01-15 14:30
阻塞任务：task-003 数据库迁移
阻塞原因：PostgreSQL 版本不兼容，Alembic 迁移脚本报错
影响范围：task-004/005/006 均依赖此任务，预计延期 4 小时
已尝试：降级 SQLAlchemy 版本（失败）
需要：DBA 介入或更换迁移方案
```
""",
        "tags": ["supervisor", "blocker", "escalation", "incident"],
        "for_agents": ["supervisor"],
    },

    # ── HR Agent ───────────────────────────────────────────────────────────────
    {
        "name": "团队组建",
        "description": "根据项目规划书，分析所需技能，从 Skill 池匹配并创建合适的执行 Agent 团队",
        "version": "2.0.0",
        "content": """
## 团队组建 Skill（参考 CrewAI 角色设计 + Spotify Squad Model）

### 角色定位
你是 HR Agent，负责把 PM 的「专家需求规划」转化为具体的团队配置。遵循「最小有效团队」原则，不过度招募。

### 团队组建流程
1. **解析需求**：从 PM 规划中提取每个阶段所需的角色和技能
2. **专家匹配**：调用「专家智能匹配」Skill，从专家池找最优候选
3. **团队验证**：检查技能覆盖矩阵，确保无关键短板
4. **RACI 分配**：明确每人的职责边界（负责/审批/咨询/知会）
5. **备选方案**：关键角色必须有备选专家

### 团队规模指南（参考 Amazon Two-Pizza Rule）
| 项目规模 | 周期 | 团队规模 | 推荐配置 |
|---------|------|---------|---------|
| 小型 | < 4周 | 2-3人 | 全栈×2 + 测试×1 |
| 中型 | 4-12周 | 4-6人 | 前端×1 + 后端×2 + 数据库×1 + 测试×1 + DevOps×1 |
| 大型 | > 12周 | 7-10人 | 按模块分组，每组 2-3 人 |

### 专家需求规划格式（传给 HR 的输入）
```json
{
  "phase_id": "phase-1",
  "tasks": [
    {
      "task_name": "用户认证模块",
      "required_role": "后端工程师",
      "required_skills": ["FastAPI", "JWT", "PostgreSQL"],
      "required_domains": ["后端开发", "数据库设计"],
      "acceptance_criteria": ["API 返回 201", "密码 bcrypt 加密"]
    }
  ]
}
```

### 输出
- 团队成员列表（含角色、技能、匹配分数）
- 技能覆盖矩阵（哪些技能有覆盖，哪些有缺口）
- RACI 矩阵
- 备选专家列表
""",
        "tags": ["hr", "team", "recruitment", "raci"],
        "for_agents": ["hr"],
    },
    {
        "name": "技能评估",
        "description": "评估 Agent 的技能水平，识别技能缺口，推荐技能提升方案",
        "version": "2.0.0",
        "content": """
## 技能评估 Skill（参考 T-shaped Skills Model + 360度评估）

### 评估框架（T型技能模型）
- **横向广度**：了解多个领域的基础知识（广而浅）
- **纵向深度**：在1-2个核心领域有专家级能力（窄而深）

### 评估维度（四象限）
| 维度 | 评估内容 | 权重 |
|------|---------|------|
| 技术深度 | 核心技能熟练度（初级/中级/专家） | 40% |
| 技术广度 | 跨领域知识覆盖 | 20% |
| 交付质量 | 历史任务质检通过率、缺陷率 | 30% |
| 协作能力 | 响应速度、文档质量、沟通清晰度 | 10% |

### 技能等级定义
- **初级（0.5）**：能在指导下完成任务，需要较多审查
- **中级（0.75）**：能独立完成任务，偶尔需要咨询
- **专家（1.0）**：能独立完成并指导他人，输出高质量产出

### 技能缺口分析
```
当前技能覆盖 vs 项目需求技能 → 缺口列表
优先级：关键路径上的缺口 > 非关键路径缺口
```

### 输出
```json
{
  "agent_id": "expert-001",
  "skill_matrix": [
    {"skill": "FastAPI", "level": "expert", "score": 1.0},
    {"skill": "React", "level": "junior", "score": 0.5}
  ],
  "overall_score": 82,
  "gaps": ["Kubernetes", "Redis"],
  "recommendations": ["建议参加 K8s 培训", "分配 Redis 相关任务积累经验"]
}
```
""",
        "tags": ["hr", "skill", "evaluation", "t-shaped"],
        "for_agents": ["hr"],
    },

    # ── PG Agent ───────────────────────────────────────────────────────────────
    {
        "name": "代码生成",
        "description": "根据需求描述和技术规格，生成高质量的代码，支持多种编程语言和框架",
        "version": "2.0.0",
        "content": """
## 代码生成 Skill（参考 MetaGPT Engineer + GitHub Copilot 最佳实践）

### 角色定位
你是执行工程师，负责把任务描述转化为可运行的代码。遵循「先设计后编码」原则，不直接开始写代码。

### 编码流程（Plan-Code-Test）
1. **理解任务**：确认输入/输出/边界条件/验收标准
2. **设计接口**：先定义函数签名、类结构、数据模型
3. **编写实现**：按设计实现，每个函数不超过 30 行
4. **自测验证**：写完立即检查边界条件和错误路径
5. **输出文件**：完整可运行的代码文件

### 支持语言与框架
- **Python**：FastAPI / Django / SQLAlchemy / Pydantic
- **TypeScript**：React / Next.js / Express / Prisma
- **SQL**：PostgreSQL / MySQL / SQLite

### 编码规范（Security-First）
```python
# ✅ 正确：参数化查询
cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))

# ❌ 错误：字符串拼接（SQL注入风险）
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
```

- 所有外部输入必须验证（Pydantic / zod）
- 密码必须哈希（bcrypt），不存明文
- 敏感信息不写入日志
- 错误处理：区分用户错误（4xx）和系统错误（5xx）
- 类型注解：Python 用 type hints，TS 用严格模式

### 输出格式
```
文件路径：src/auth/router.py
代码内容：[完整可运行代码]
说明：[关键设计决策，不超过3条]
测试建议：[需要测试的关键场景]
```
""",
        "tags": ["pg", "coding", "generation", "security"],
        "for_agents": ["pg"],
    },
    {
        "name": "代码审查",
        "description": "对代码进行全面审查，检查代码质量、安全漏洞、性能问题和最佳实践",
        "version": "2.0.0",
        "content": """
## 代码审查 Skill（参考 Google Engineering Practices + OWASP Code Review Guide）

### 审查清单（按优先级）

**🔴 必须修复（阻断合并）**
- SQL 注入、命令注入、XSS、CSRF
- 密码/密钥明文存储或日志输出
- 未处理的异常导致信息泄露
- 认证/授权绕过漏洞
- 竞态条件（Race Condition）

**🟡 应该修复（合并前处理）**
- N+1 查询问题
- 缺少输入验证
- 错误处理不完整（只 catch 不处理）
- 函数超过 50 行（违反单一职责）
- 魔法数字/字符串（应提取为常量）

**🔵 建议改进（可后续处理）**
- 命名不够描述性
- 缺少注释（复杂逻辑）
- 测试覆盖不足
- 可以用更简洁的写法

### 审查输出格式
```
文件：src/auth/router.py
行号：45
级别：🔴 必须修复
问题：密码明文存储
代码：user.password = password
修复：user.password = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
验收：运行 test_auth.py::test_password_hashing 通过
```

### 质量评分（0-100）
- 90-100：优秀，可直接合并
- 70-89：良好，修复黄色问题后合并
- 50-69：一般，需要较多修改
- < 50：需要重构
""",
        "tags": ["pg", "review", "quality", "security"],
        "for_agents": ["pg"],
    },
    {
        "name": "单元测试生成",
        "description": "为代码自动生成单元测试，覆盖正常路径、边界条件和异常情况",
        "version": "2.0.0",
        "content": """
## 单元测试生成 Skill（参考 pytest 最佳实践 + TDD 测试驱动开发）

### 测试策略（AAA 模式）
每个测试用例遵循 Arrange-Act-Assert：
```python
def test_user_register_success():
    # Arrange（准备）
    user_data = {"email": "test@example.com", "password": "SecurePass123"}
    
    # Act（执行）
    response = client.post("/auth/register", json=user_data)
    
    # Assert（断言）
    assert response.status_code == 201
    assert "token" in response.json()
    assert response.json()["email"] == user_data["email"]
```

### 测试覆盖矩阵
| 场景类型 | 示例 | 优先级 |
|---------|------|--------|
| 正常路径 | 正确输入，期望输出 | P0 |
| 边界值 | 空字符串、最大值、最小值 | P0 |
| 错误路径 | 无效输入、权限不足 | P1 |
| 并发场景 | 重复注册、并发写入 | P1 |
| 外部依赖 | Mock 数据库、Mock API | P2 |

### Mock 规范
```python
# 使用 pytest-mock 或 unittest.mock
@pytest.fixture
def mock_db(mocker):
    return mocker.patch("app.db.session")

# 使用 httpx.AsyncClient 测试 FastAPI
@pytest.fixture
async def client():
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
```

### 覆盖率目标
- 行覆盖率 > 80%（CI 门禁）
- 分支覆盖率 > 70%
- 关键业务逻辑（认证/支付/权限）100%

### 输出
- 完整测试文件（test_xxx.py）
- conftest.py（共享 fixtures）
- 覆盖率配置（pytest.ini / pyproject.toml）
""",
        "tags": ["pg", "testing", "unittest", "tdd"],
        "for_agents": ["pg"],
    },

    # ── CCB Agent ──────────────────────────────────────────────────────────────
    {
        "name": "变更评估",
        "description": "评估变更请求的影响范围、风险和优先级，给出批准/拒绝/延期建议",
        "version": "2.0.0",
        "content": """
## 变更评估 Skill

### 评估维度
1. 影响范围：受影响的子项目、模块、接口
2. 工作量估算：额外工时
3. 风险评估：引入新风险的可能性
4. 优先级：业务价值 vs 实施成本

### 决策矩阵
- 低影响 + 高价值 → 立即批准
- 高影响 + 高价值 → 评审后批准
- 高影响 + 低价值 → 延期或拒绝
- 低影响 + 低价值 → 积压处理

### 输出
- 变更影响分析报告
- 决策建议（批准/拒绝/延期）
- 实施计划（如批准）
""",
        "tags": ["ccb", "change", "evaluation"],
        "for_agents": ["ccb"],
    },
    {
        "name": "版本控制",
        "description": "管理代码版本、分支策略、发布计划，确保变更有序进行",
        "version": "1.0.0",
        "content": """
## 版本控制 Skill

### 分支策略
- main：生产环境，只接受 PR
- develop：开发主线
- feature/*：功能分支
- hotfix/*：紧急修复

### 版本号规范（SemVer）
- MAJOR.MINOR.PATCH
- MAJOR：不兼容的 API 变更
- MINOR：向后兼容的新功能
- PATCH：向后兼容的 Bug 修复

### 发布流程
1. 功能冻结
2. 回归测试
3. 变更日志生成
4. 版本标签
5. 部署审批

### 输出
- 版本发布计划
- 变更日志（CHANGELOG）
""",
        "tags": ["ccb", "version", "release"],
        "for_agents": ["ccb"],
    },

    # ── 通用 Skill（所有 Agent 可用）──────────────────────────────────────────
    {
        "name": "文档生成",
        "description": "根据代码、需求或会议记录，自动生成技术文档、API 文档、用户手册",
        "version": "1.0.0",
        "content": """
## 文档生成 Skill

### 文档类型
- 技术设计文档（TDD）
- API 文档（OpenAPI/Swagger）
- 用户手册
- 部署文档
- 会议纪要

### 输出格式
- Markdown（默认）
- HTML
- PDF（需要 pandoc）

### 模板
遵循公司文档规范，包含：
- 文档标题、版本、作者、日期
- 目录
- 正文
- 附录
""",
        "tags": ["common", "documentation", "writing"],
        "for_agents": ["pm", "pg", "supervisor", "hr", "ccb"],
    },
    {
        "name": "LLM 对话",
        "description": "调用配置的 LLM API 进行智能对话，支持上下文管理和工具调用",
        "version": "1.0.0",
        "content": """
## LLM 对话 Skill

### 功能
- 多轮对话（上下文保持）
- 系统提示词注入
- 工具调用（Function Calling）
- 流式输出

### 支持模型
- OpenAI：gpt-4o / gpt-4-turbo / gpt-3.5-turbo
- DeepSeek：deepseek-v4-pro / deepseek-v4-flash
- Kimi：moonshot-v1-8k / moonshot-v1-32k
- Qwen：qwen-turbo / qwen-plus / qwen-max
- 本地：Ollama 兼容模型

### 配置
通过「系统设置」配置默认 API，或为每个 Agent 单独配置
""",
        "tags": ["common", "llm", "chat"],
        "for_agents": ["pm", "pg", "supervisor", "hr", "ccb"],
    },
    {
        "name": "文件读取",
        "description": "读取和解析各种格式的文件，提取文本内容供 Agent 分析",
        "version": "1.0.0",
        "content": """
## 文件读取 Skill

### 支持格式
- 文本：txt / md / markdown / log / yaml / yml
- 数据：json / csv
- 文档：pdf（需 pdfplumber）/ docx / doc（需 python-docx）

### 功能
- 自动检测编码（UTF-8 / GBK）
- 大文件截断（超过 50000 字符自动截断）
- 多文件合并（内容按文件名分隔）

### 使用场景
- 上传需求文档给 PM Agent 分析
- 上传代码文件给 PG Agent 审查
- 上传项目文档给 Supervisor 参考
""",
        "tags": ["common", "file", "parsing"],
        "for_agents": ["pm", "pg", "supervisor", "hr", "ccb"],
    },
    # ── 专家技能（9类专家，由 HR 从专家池动态匹配）────────────────────────────
    {
        "name": "前端开发",
        "description": "负责 Web 前端界面开发，包括 React/Vue 组件、样式、交互逻辑、性能优化",
        "version": "1.0.0",
        "content": """
## 前端开发 Skill

### 技术栈
- 框架：React 18 / Vue 3 / Next.js
- 语言：TypeScript
- 样式：Tailwind CSS / Ant Design / MUI
- 构建：Vite / Webpack
- 测试：Jest / Vitest / Playwright

### 开发规范
1. 组件单一职责，props 类型严格定义
2. 响应式设计，支持移动端（breakpoint: sm/md/lg/xl）
3. 无障碍访问（WCAG 2.1 AA）：aria-label、键盘导航
4. 性能优化：懒加载、memo、虚拟列表
5. 错误边界（Error Boundary）处理异常

### 输出
- 完整组件代码（.tsx/.vue）
- 样式文件
- 单元测试
- Storybook 示例（可选）
""",
        "tags": ["pg", "frontend", "react", "typescript"],
        "for_agents": ["pg"],
    },
    {
        "name": "后端开发",
        "description": "负责服务端 API 开发，包括业务逻辑、数据处理、接口设计、性能优化",
        "version": "1.0.0",
        "content": """
## 后端开发 Skill

### 技术栈
- 语言：Python 3.10+
- 框架：FastAPI / Django REST Framework
- ORM：SQLAlchemy 2.0 / Django ORM
- 缓存：Redis
- 消息队列：Celery / RabbitMQ

### 开发规范
1. RESTful API 设计，遵循 HTTP 语义
2. 参数校验（Pydantic）+ 完整错误处理
3. 数据库操作使用事务，防止并发问题
4. 日志记录（结构化 JSON 日志）
5. 单元测试覆盖率 > 80%

### 输出
- API 路由代码
- 业务逻辑层
- 数据模型
- API 文档（OpenAPI）
""",
        "tags": ["pg", "backend", "python", "fastapi"],
        "for_agents": ["pg"],
    },
    {
        "name": "数据库设计",
        "description": "负责数据库 Schema 设计、索引优化、查询优化、数据迁移",
        "version": "1.0.0",
        "content": """
## 数据库设计 Skill

### 支持数据库
- 关系型：PostgreSQL / MySQL / SQLite
- 非关系型：MongoDB / Redis
- 搜索引擎：Elasticsearch

### 设计规范
1. 范式化设计（至少满足 3NF）
2. 主键使用 UUID 或自增 ID
3. 必要字段：created_at / updated_at / is_deleted
4. 索引策略：高频查询字段建索引，避免过度索引
5. 外键约束 + 级联规则明确

### 输出
- ER 图（Mermaid 格式）
- DDL 建表语句
- 索引设计说明
- 数据迁移脚本（Alembic）
""",
        "tags": ["pg", "database", "sql", "postgresql"],
        "for_agents": ["pg"],
    },
    {
        "name": "API 设计",
        "description": "负责 RESTful/GraphQL API 接口设计，包括路由规划、请求响应格式、版本管理",
        "version": "1.0.0",
        "content": """
## API 设计 Skill

### 设计原则
1. RESTful 语义：GET/POST/PUT/PATCH/DELETE 正确使用
2. URL 命名：小写、连字符、名词复数（/users/{id}/orders）
3. 统一响应格式：{code, message, data, timestamp}
4. 错误码规范：4xx 客户端错误，5xx 服务端错误
5. 版本管理：URL 前缀（/api/v1/）或 Header

### 文档规范
- OpenAPI 3.0 格式
- 每个接口必须有：描述、请求示例、响应示例、错误码说明
- 认证方式说明（Bearer Token / API Key）

### 输出
- OpenAPI YAML/JSON 文档
- Postman Collection
- 接口变更日志
""",
        "tags": ["pg", "api", "rest", "openapi"],
        "for_agents": ["pg"],
    },
    {
        "name": "系统架构设计",
        "description": "负责系统整体架构设计，包括技术选型、模块划分、部署架构、扩展性设计",
        "version": "1.0.0",
        "content": """
## 系统架构设计 Skill

### 架构模式
- 单体架构：适合小型项目，快速迭代
- 微服务架构：适合大型项目，独立部署
- 事件驱动架构：适合异步处理场景
- CQRS：读写分离，适合高并发读场景

### 设计原则
1. 高可用：无单点故障，支持水平扩展
2. 高性能：缓存策略、异步处理、CDN
3. 可维护：模块化、低耦合、高内聚
4. 安全性：最小权限、数据加密、审计日志

### 输出
- 架构图（C4 Model：Context/Container/Component）
- 技术选型说明（含备选方案和权衡）
- ADR（架构决策记录）
- 部署架构图
""",
        "tags": ["pg", "architecture", "design"],
        "for_agents": ["pg"],
    },
    {
        "name": "DevOps 与部署",
        "description": "负责 CI/CD 流水线搭建、容器化部署、监控告警、基础设施即代码",
        "version": "1.0.0",
        "content": """
## DevOps 与部署 Skill

### 技术栈
- 容器：Docker / Docker Compose
- 编排：Kubernetes / Docker Swarm
- CI/CD：GitHub Actions / GitLab CI / Jenkins
- 监控：Prometheus + Grafana / ELK Stack
- IaC：Terraform / Ansible

### 流水线规范
1. 代码提交触发：lint → test → build → deploy
2. 环境隔离：dev / staging / production
3. 蓝绿部署或滚动更新，零停机
4. 自动回滚：健康检查失败时自动回滚
5. 密钥管理：不在代码中硬编码，使用 Vault/Secrets

### 输出
- Dockerfile + docker-compose.yml
- CI/CD 配置文件
- Kubernetes Manifests / Helm Chart
- 监控告警规则
""",
        "tags": ["pg", "devops", "docker", "kubernetes"],
        "for_agents": ["pg"],
    },
    {
        "name": "安全审计",
        "description": "负责代码安全审计、漏洞扫描、安全加固，确保系统符合安全规范",
        "version": "1.0.0",
        "content": """
## 安全审计 Skill

### 审计范围
- OWASP Top 10：注入、认证缺陷、XSS、CSRF、敏感数据暴露等
- 依赖漏洞：第三方库 CVE 扫描
- 配置安全：默认密码、不必要的端口、权限过大
- 代码逻辑：越权访问、业务逻辑漏洞

### 审计流程
1. 静态代码分析（SAST）
2. 依赖扫描（SCA）
3. 动态测试（DAST）
4. 人工审查关键逻辑

### 输出
- 安全审计报告（漏洞等级：Critical/High/Medium/Low）
- 修复建议（含具体代码示例）
- 安全加固清单
""",
        "tags": ["pg", "security", "audit", "owasp"],
        "for_agents": ["pg"],
    },
    {
        "name": "测试与质量保障",
        "description": "负责测试策略制定、测试用例编写、自动化测试、性能测试",
        "version": "1.0.0",
        "content": """
## 测试与质量保障 Skill

### 测试层次
- 单元测试：函数/方法级别，覆盖率 > 80%
- 集成测试：模块间接口测试
- E2E 测试：用户场景完整流程测试
- 性能测试：压测、负载测试、基准测试

### 测试框架
- Python：pytest + coverage
- TypeScript：Jest / Vitest + Testing Library
- E2E：Playwright / Cypress
- 性能：Locust / k6

### 质量门禁
- 单元测试覆盖率 < 80%：阻断合并
- 关键路径 E2E 测试失败：阻断发布
- 性能回归 > 20%：触发告警

### 输出
- 测试计划文档
- 测试用例（含边界条件）
- 自动化测试代码
- 测试报告
""",
        "tags": ["pg", "testing", "quality", "pytest"],
        "for_agents": ["pg"],
    },
    {
        "name": "数据处理与分析",
        "description": "负责数据管道搭建、数据清洗、数据分析、报表生成",
        "version": "1.0.0",
        "content": """
## 数据处理与分析 Skill

### 技术栈
- 处理：Python Pandas / Polars / PySpark
- 存储：PostgreSQL / ClickHouse / BigQuery
- 可视化：ECharts / D3.js / Grafana
- 调度：Airflow / Prefect

### 数据处理规范
1. 数据清洗：缺失值处理、异常值检测、格式标准化
2. 数据验证：Schema 校验（Great Expectations）
3. 幂等性：重复执行不产生副作用
4. 血缘追踪：记录数据来源和转换过程

### 输出
- 数据处理脚本
- 数据质量报告
- 可视化图表配置
- 数据字典
""",
        "tags": ["pg", "data", "analytics", "pandas"],
        "for_agents": ["pg"],
    },

    # ── PM Agent 高级技能（来源：alirezarezvani/claude-skills senior-pm + scrum-master）──
    {
        "name": "项目健康度评估",
        "description": "五维评分（范围/时间/成本/质量/风险），EMV风险量化，WSJF优先级排序，生成项目健康度报告",
        "version": "2.0.0",
        "content": """
## 项目健康度评估 Skill（Senior PM Framework）

### 五维健康度评分（0-100）
1. 范围健康度：需求变更率、范围蔓延风险
2. 时间健康度：进度偏差率、关键路径余量
3. 成本健康度：预算消耗率、EV/PV比值
4. 质量健康度：缺陷密度、测试覆盖率
5. 风险健康度：高风险项数量、风险应对完成率

### EMV 风险量化
- EMV = 概率（0-1）× 影响（金额/工期）
- 高风险（EMV > 阈值）：立即制定应对计划
- 应对策略：规避/转移/缓解/接受

### WSJF 优先级
WSJF = （业务价值 + 时间紧迫度 + 风险降低）÷ 工作量
- 优先处理 WSJF 最高的任务

### 输出格式
```json
{
  "health_score": {"scope": 85, "time": 72, "cost": 90, "quality": 78, "risk": 65},
  "overall": 78,
  "top_risks": [...],
  "recommended_actions": [...]
}
```
""",
        "tags": ["pm", "health", "risk", "wsjf"],
        "for_agents": ["pm"],
    },
    {
        "name": "Sprint 健康度分析",
        "description": "基于历史Sprint数据进行蒙特卡洛速度预测，多维度Sprint健康评分，回顾会议分析",
        "version": "2.0.0",
        "content": """
## Sprint 健康度分析 Skill（Scrum Master Framework）

### Sprint 健康度评分维度
1. 速度趋势：近3个Sprint速度变化趋势
2. 承诺完成率：Sprint承诺点数 vs 实际完成点数
3. 阻塞率：阻塞任务占比（目标 < 10%）
4. 技术债比例：技术债任务占Sprint总量（目标 < 20%）

### 蒙特卡洛速度预测
- 基于历史3个Sprint的速度数据
- 模拟1000次迭代，给出置信区间
- 输出：P50/P80/P95 完成概率对应的Sprint数

### 回顾会议分析
- 做得好（Keep）：识别成功实践
- 待改进（Improve）：识别问题模式
- 行动项（Action）：具体可执行的改进措施
- 追踪：上次行动项完成率

### 阻塞处理 SLA
- 阻塞 > 2小时：必须上报 Supervisor
- 阻塞 > 4小时：触发 CCB 仲裁
""",
        "tags": ["pm", "sprint", "scrum", "velocity"],
        "for_agents": ["pm"],
    },

    # ── Supervisor Agent 高级技能（来源：addyosmani/agent-skills code-reviewer）──
    {
        "name": "五维质检框架",
        "description": "从正确性/可读性/架构/安全性/性能五个维度对产出进行系统性质量检查",
        "version": "2.0.0",
        "content": """
## 五维质检框架 Skill（Senior Code Reviewer Framework）

### 质检维度

**1. 正确性**
- 产出是否符合需求规格？
- 边界条件是否处理（null/空/越界/错误路径）？
- 是否有竞态条件、状态不一致？

**2. 可读性**
- 其他人能否无需解释就理解？
- 命名是否描述性且与项目规范一致？
- 控制流是否清晰（无深层嵌套）？

**3. 架构**
- 是否遵循现有模式？新模式是否有充分理由？
- 是否符合单一职责原则？
- 依赖关系是否合理？

**4. 安全性**
- 是否有注入风险（SQL/命令/XSS）？
- 认证授权是否正确？
- 敏感数据是否保护？

**5. 性能**
- 是否有明显的性能问题（N+1查询/内存泄漏）？
- 资源使用是否合理？

### 质检输出格式
- **PASS**：所有维度通过，附简要说明
- **NEEDS_REVISION**：列出问题（维度+描述+修改建议），打回重做
- **BLOCKED**：发现阻塞性问题，触发 CCB 仲裁
""",
        "tags": ["supervisor", "quality", "review", "five-dimensions"],
        "for_agents": ["supervisor"],
    },

    # ── CCB Agent 高级技能（来源：alirezarezvani/claude-skills compliance-os）──
    {
        "name": "变更影响分析",
        "description": "系统性评估变更的影响范围、风险等级和实施成本，给出修/排期/拒绝三种决策建议",
        "version": "2.0.0",
        "content": """
## 变更影响分析 Skill（CCB Framework）

### 评估四维框架
1. **影响范围**：受影响的功能/接口/数据/依赖模块
2. **风险评估**：技术风险/进度风险/质量风险（高/中/低）
3. **成本评估**：额外工作量估算 vs 不变更的代价
4. **优先级**：紧急程度（阻塞/高/中/低）× 业务价值

### 三种决策规范
- **修（FIX）**：变更合理且紧急 → 接受变更，打回相关任务重做，更新规格文档
- **排期（RESCHEDULE）**：变更合理但不紧急 → 放入下一迭代，记录需求变更日志
- **拒绝（REJECT）**：变更不合理或代价过高 → 维持原方案，说明拒绝理由

### 触发条件
- 需求变更：评估影响范围，决定是否接受，更新 RACI 矩阵
- 架构调整：必须有 ADR（架构决策记录），评估技术债务影响
- 阻塞超时（>2h）：分析阻塞原因，决定资源调配或方案调整
- 质检争议：基于五维质检框架做最终裁决

### 决策记录格式
```json
{
  "trigger": "需求变更",
  "impact_analysis": {...},
  "decision": "FIX/RESCHEDULE/REJECT",
  "reason": "...",
  "action_items": [...],
  "rollback_plan": "..."
}
```
""",
        "tags": ["ccb", "change", "impact", "decision"],
        "for_agents": ["ccb"],
    },

    # ── HR Agent 高级技能（来源：alirezarezvani/claude-skills c-level-advisor）──
    {
        "name": "专家智能匹配",
        "description": "基于技能匹配度/领域经验/历史评分/可用性四维加权算法，为任务匹配最优专家",
        "version": "2.0.0",
        "content": """
## 专家智能匹配 Skill（HR Framework）

### 四维加权匹配算法
- 技能匹配度（40%）：候选专家技能 vs 任务需求技能的覆盖率
- 领域经验（30%）：相关领域深度（初级=0.5/中级=0.75/专家=1.0）
- 历史评分（20%）：平均质量评分（0-100）
- 可用性（10%）：空闲=1.0/忙碌=0.3

### 匹配输出
- 最优匹配：匹配分数最高的专家 + 匹配理由
- 备选方案：2个备选专家（分数次高）
- 无匹配时：说明原因，建议降低要求或拆分任务

### 团队组建原则
- 最小化团队：只招募任务真正需要的角色
- 技能互补：覆盖所有关键领域，无明显短板
- 明确职责：RACI 矩阵表达每人职责边界
- 备选方案：关键角色必须有备选专家

### 输出格式
```json
{
  "best_match": {"expert_id": "...", "score": 87.5, "reason": "..."},
  "alternatives": [...],
  "team_coverage": {"covered": [...], "gaps": [...]},
  "raci_matrix": {...}
}
```
""",
        "tags": ["hr", "matching", "team", "expert"],
        "for_agents": ["hr"],
    },

    # ── 全能工程师 (FullStackEngineerAgent) ────────────────────────────────────
    {
        "name": "代码静态分析",
        "description": "对整改目标代码进行多层静态分析：语法检查、安全漏洞扫描、逻辑缺陷识别，输出结构化问题报告",
        "version": "1.0.0",
        "content": """
## 代码静态分析 Skill（全能工程师）

### 分析层次
- Layer1 语法层：AST 解析，检查语法错误、未定义变量、类型不匹配
- Layer2 逻辑层：控制流分析，检查死代码、空指针、无限循环、条件矛盾
- Layer3 安全层：OWASP Top10 扫描，SQL注入、XSS、硬编码密钥、路径穿越
- Layer4 规范层：代码风格（PEP8/ESLint）、命名规范、注释完整性

### 输出格式
```json
{
  "layer": "syntax|logic|security|style",
  "severity": "error|warning|info",
  "file": "相对路径",
  "line": 行号,
  "message": "问题描述",
  "fix_hint": "修复建议（一句话）"
}
```

### 原则
- 不确定就说不确定，不用模糊答案充数（Security-First）
- 发现安全问题必须标 severity=error，不能降级为 warning
""",
        "tags": ["fullstack_engineer", "analysis", "security", "quality"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "代码整改执行",
        "description": "接收 needs_manual 缺陷单，与用户对话确认整改方案，执行 Immutability 原则写入完整新文件",
        "version": "1.0.0",
        "content": """
## 代码整改执行 Skill（全能工程师）

### 核心原则
- Plan Before Execute：先给出完整整改方案，用户确认后再动文件
- Immutability：不打补丁，写完整的新版本替换旧文件
- 收到缺陷单时先明确三点（不明确直接追问）：
  1. 具体要修什么（message + file_path + fix_hint）
  2. 修改边界是什么（只改这个文件还是涉及多文件）
  3. 验收标准是什么（修完后怎么验证是对的）

### 工作流
1. 读取缺陷单（defect_id + message + file_path + fix_hint）
2. 读取目标文件内容，定位问题
3. 输出整改方案（改哪里、改成什么、为什么）
4. 等待用户确认
5. 用户确认后：输出完整新文件内容，调用 apply-fix 写入
6. 写入后触发 Layer1+Layer2 静态验证

### 禁止事项
- 禁止在用户确认前自动写文件
- 禁止只改局部行（必须输出完整文件）
- 禁止把多个 defect 的修改混在同一次 apply 中（一个 defect 对应一次 apply）
""",
        "tags": ["fullstack_engineer", "repair", "immutability"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "使用手册生成",
        "description": "根据项目结构、阶段规划、代码注释，自动生成用户手册/API文档/部署手册",
        "version": "1.0.0",
        "content": """
## 使用手册生成 Skill（全能工程师）

### 手册类型
- **user（用户手册）**：面向最终用户，功能操作说明，截图示意
- **api（API文档）**：面向开发者，接口路径/参数/响应/错误码
- **deploy（部署手册）**：面向运维，环境要求/安装步骤/配置说明/常见问题

### 生成原则（Plan Before Execute）
1. 先扫描项目文件结构，提取功能模块列表
2. 从阶段规划中获取各阶段产出物
3. 从代码注释和 README 中提取关键描述
4. 生成大纲，经用户确认后输出完整手册

### 手册结构模板（用户手册）
```markdown
# 项目名称 使用手册

## 1. 简介
## 2. 快速开始
## 3. 功能说明
  ### 3.1 模块一
  ### 3.2 模块二
## 4. 常见问题（FAQ）
## 5. 更新日志
```

### 输出
- Markdown 格式手册文件（保存到 workspace/docs/）
- 同时返回手册内容
""",
        "tags": ["fullstack_engineer", "documentation", "manual"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "文件归档分类",
        "description": "扫描 workspace，对所有文件分类（source/test/doc/config/output/useless），标注所属开发阶段",
        "version": "1.0.0",
        "content": """
## 文件归档分类 Skill（全能工程师）

### 分类规则
| 类别 | 关键词/后缀 | 说明 |
|------|-----------|------|
| test | test/, tests/, _test., .spec. | 测试文件 |
| doc | .md, .rst, docs/, README | 文档文件 |
| config | .yml, .yaml, .env, Dockerfile, requirements | 配置文件 |
| output | dist/, build/, .pyc, __pycache__ | 构建产出 |
| source | 其他代码文件 | 源代码（兜底） |
| useless | .DS_Store, node_modules/, .cache | 无用文件 |

### 阶段标注
从 PhaseManager.file_registry 获取文件-阶段映射，
为每个文件标注「哪个阶段产出」（phase_id + phase_name）。

### 统计输出
```json
{
  "total": 100,
  "by_category": {"source": 60, "test": 20, "doc": 10, "config": 8, "output": 2},
  "by_phase": {"phase-1": 30, "phase-2": 50, "unknown": 20},
  "useless_files": [".DS_Store", "node_modules/"],
  "files": [
    {"path": "src/auth/router.py", "category": "source", "phase_id": "phase-1", "phase_name": "基础架构"}
  ]
}
```
""",
        "tags": ["fullstack_engineer", "archive", "classification"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "项目问答服务",
        "description": "独立上下文，回答用户关于项目的任意问题：技术方案、功能模块、文件位置、阶段规划",
        "version": "1.0.0",
        "content": """
## 项目问答服务 Skill（全能工程师）

### 问答范围
- 技术方案：为什么选择这个技术栈？有哪些权衡？
- 功能模块：某个功能在哪个文件/哪个阶段实现的？
- 文件位置：某个功能的代码在哪里？测试在哪里？
- 阶段规划：各阶段做了什么？现在到哪个阶段了？
- 缺陷状态：哪些问题已修复？哪些还在 needs_manual？

### 问答原则
- 知道就直接回答，不知道就说不知道（Security-First）
- 回答基于项目实际内容，不凭空臆造
- 复杂问题先给出答案摘要，再展开细节
- 上下文与整改对话完全隔离，不污染整改流程

### 知识来源
1. PM 组长的 final_plan（项目全局规划）
2. workspace 文件结构扫描
3. PhaseManager 阶段信息
4. ctx.qc_results 质检状态
""",
        "tags": ["fullstack_engineer", "qa", "knowledge"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "阶段感知",
        "description": "从 PM 组长的 final_plan 中提取各阶段信息，知晓哪些文件属于哪个阶段，哪个阶段负责哪些功能",
        "version": "1.0.0",
        "content": """
## 阶段感知 Skill（全能工程师）

### 数据来源
- PM 组长 final_plan（phases + subprojects + file_registry）
- PhaseManager.phases（阶段列表和状态）
- PhaseManager.file_registry（文件-阶段映射）

### 感知内容
1. 项目共有几个阶段，各阶段的名称和描述
2. 每个阶段的产出文件有哪些
3. 每个阶段由哪些 Agent 负责
4. 当前处于哪个阶段，哪些阶段已完成

### 使用场景
- 文件归档时：给每个文件打上「phase_id + phase_name」标签
- 使用手册生成：按阶段组织文档结构
- 问答服务：回答「这个功能在哪个阶段实现」
- 整改对话：理解缺陷属于哪个阶段，上下文更准确

### 加载方式
通过 `/engineer/{project_id}/load-context` 接口，
从 PM 组长 final_plan 自动加载项目全局背景。
""",
        "tags": ["fullstack_engineer", "phase", "context"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "质检验证",
        "description": "整改完成后触发 Layer1+Layer2 静态质检，验证修改是否引入新问题",
        "version": "1.0.0",
        "content": """
## 质检验证 Skill（全能工程师）

### 触发时机
- apply-fix 写入文件后自动触发（run_qa=True 时）
- 用户手动触发（/engineer/{project_id}/run-qa 接口）

### 验证层次
- Layer1 语法层：AST 解析，确认无语法错误
- Layer2 逻辑层：基础逻辑检查，确认修改未引入新 bug

### 验证结果处理
- 通过：缺陷状态更新为 fixing（等下次质检组长复检）
- 发现新问题：立即反馈给用户，不自动覆盖已写文件

### 与质检组长的分工
- 全能工程师质检：整改后的快速验证（局部检查）
- QAAgent 质检：阶段质检（全局四维检查）
- 只有 QAAgent 通过后，缺陷才能从 fixing → fixed
""",
        "tags": ["fullstack_engineer", "validation", "qa"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "Defect管理",
        "description": "管理 needs_manual 缺陷单的生命周期：接收→对话→整改→验证→关闭",
        "version": "1.0.0",
        "content": """
## Defect 管理 Skill（全能工程师）

### 缺陷生命周期
needs_manual → (整改对话中) → fixing → (QAAgent复检) → fixed

### 状态流转规则
1. needs_manual：自动修复超过 3 次，由 Supervisor 标记
2. fixing：全能工程师执行 apply-fix 后更新
3. fixed：下次 QAAgent 质检未再发现该问题时自动关闭
4. verified：用户手动确认修复完成

### 关键约束
- 每个 defect_id 有独立的对话上下文，互不污染
- 单个文件修改次数上限 3 次（file_edit_stats 追踪）
- 超过 3 次仍未修复：标记为「需根因分析」，不再继续整改

### 批量处理（all_defects）
同一文件的多个缺陷，可以在一次整改中一起处理：
- 传入 all_defects 列表，按文件聚合
- 一次输出完整新文件，覆盖所有相关缺陷
- 减少文件写入次数，提高整改效率
""",
        "tags": ["fullstack_engineer", "defect", "lifecycle"],
        "for_agents": ["fullstack_engineer"],
    },
    {
        "name": "任务规划接收",
        "description": "从 PM 组长接收项目规划，理解各阶段任务目标，知晓项目整体背景和工作逻辑",
        "version": "1.0.0",
        "content": """
## 任务规划接收 Skill（全能工程师）

### 信息来源
PM 组长通过 confirm-plan 接口确认规划后，自动广播 final_plan 给全能工程师。
包含：
- project_overview：项目整体目标和背景
- phases：各阶段名称、描述、交付物
- subprojects：各子项目的功能模块说明
- core_features：核心功能列表
- tech_stack：技术栈选择

### 接收后的能力
知晓了规划后，全能工程师能：
1. 在整改时理解「这个缺陷属于哪个功能模块」
2. 生成手册时按功能模块组织内容
3. 回答问答时给出更准确的背景信息
4. 归档文件时正确标注功能域

### 遵守系统底线原则
- Plan Before Execute：先规划后执行，不边想边写
- Agent-First：先确认需求，任务三要素不明确直接追问
- Test-Driven：整改前模拟验证，确保代码可运行
- Immutability：有错误不打补丁，直接替换完整版本
- Security-First：安全是底线，发现安全问题必须上报
""",
        "tags": ["fullstack_engineer", "planning", "context"],
        "for_agents": ["fullstack_engineer"],
    },

]

# ─── 核心 Agent 角色到类型的映射 ──────────────────────────────────────────────
AGENT_ROLE_TAGS = {
    "pm": "pm",
    "supervisor": "supervisor",
    "hr": "hr",
    "pg": "pg",
    "ccb": "ccb",
    "fullstack_engineer": "fullstack_engineer",
}


def main():
    # 尝试导入数据库模块
    kv_set_fn = None
    kv_get_fn = None
    try:
        from core.database import kv_set, kv_get, init_db
        init_db()
        kv_set_fn = kv_set
        kv_get_fn = kv_get
    except Exception as e:
        print(f"  ⚠️ 数据库初始化失败，将只写 JSON 文件：{e}")

    # 加载现有 Skill 池（优先从数据库，其次从 JSON 备份文件）
    existing_skills = {}
    if kv_get_fn:
        try:
            existing_skills = kv_get_fn("skills", {})
        except Exception:
            existing_skills = {}
    if not existing_skills and SKILLS_FILE.exists():
        try:
            existing_skills = json.loads(SKILLS_FILE.read_text(encoding="utf-8"))
            print("  📂 从 skills.json 恢复 Skill 池（数据库为空）")
        except Exception:
            existing_skills = {}
    if not existing_skills:
        existing_skills = {}

    # 建立 name → skill_id 的反查表
    name_to_id = {v.get("name"): sid for sid, v in existing_skills.items()}

    added = 0
    updated = 0
    skill_id_map: dict[str, list[str]] = {}  # agent_type → [skill_id, ...]

    for skill_def in SKILLS:
        name = skill_def["name"]
        new_content = skill_def["content"].strip()
        new_version = skill_def["version"]

        if name in name_to_id:
            sid = name_to_id[name]
            old_version = existing_skills[sid].get("version", "1.0.0")
            old_content = existing_skills[sid].get("content", "")
            # 版本号更高或内容有变化时更新
            if new_version > old_version or new_content != old_content:
                existing_skills[sid]["content"] = new_content
                existing_skills[sid]["version"] = new_version
                existing_skills[sid]["description"] = skill_def["description"]
                existing_skills[sid]["tags"] = skill_def.get("tags", [])
                existing_skills[sid]["updated_at"] = time.time()
                updated += 1
                print(f"  🔄 更新：{name} ({sid})  {old_version} → {new_version}")
            else:
                print(f"  跳过（无变化）：{name}")
            for agent_type in skill_def.get("for_agents", []):
                skill_id_map.setdefault(agent_type, []).append(sid)
            continue

        skill_id = f"skill-{uuid.uuid4().hex[:8]}"
        existing_skills[skill_id] = {
            "id": skill_id,
            "name": name,
            "description": skill_def["description"],
            "version": new_version,
            "content": new_content,
            "source": "builtin",
            "tags": skill_def.get("tags", []),
            "status": "active",
            "created_at": time.time(),
            "usage_count": 0,
        }
        name_to_id[name] = skill_id
        added += 1
        print(f"  ✅ 新增：{name} ({skill_id})")

        for agent_type in skill_def.get("for_agents", []):
            skill_id_map.setdefault(agent_type, []).append(skill_id)

    # ─── 写入数据库（主要持久化路径，与后端 kv_get 一致）───────────────────────
    if kv_set_fn:
        try:
            kv_set_fn("skills", existing_skills)
            print(f"\n✅ Skill 池已写入数据库：新增 {added} 个，更新 {updated} 个，共 {len(existing_skills)} 个")
        except Exception as e:
            print(f"\n⚠️ 写入数据库失败：{e}")
            print("  将回退到 JSON 文件方式写入")
            kv_set_fn = None
    else:
        print("\n⚠️ 数据库不可用，将写入 JSON 文件作为替代")

    # ─── 持久化到 JSON 文件（备份快照）─────────────────────────────────────────
    SKILLS_FILE.write_text(
        json.dumps(existing_skills, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"📂 Skill 池 JSON 备份已保存：{SKILLS_FILE}")

    # ─── 更新 agents_config：为核心 Agent 记录 skill 分配 ─────────────────────
    agents_config = {}
    if kv_get_fn:
        try:
            agents_config = kv_get_fn("agents_config", {})
        except Exception:
            agents_config = {}
    if not agents_config and AGENTS_CONFIG_FILE.exists():
        try:
            agents_config = json.loads(AGENTS_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            agents_config = {}

    agents_config["__core_skills__"] = skill_id_map

    if kv_set_fn:
        try:
            kv_set_fn("agents_config", agents_config)
            print("✅ Agent Skill 分配已写入数据库")
        except Exception:
            kv_set_fn = None

    AGENTS_CONFIG_FILE.write_text(
        json.dumps(agents_config, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"📂 Agent 配置 JSON 备份已保存：{AGENTS_CONFIG_FILE}")

    print("\n核心 Agent Skill 分配：")
    for agent_type, skill_ids in skill_id_map.items():
        skill_names = [existing_skills[sid]["name"] for sid in skill_ids if sid in existing_skills]
        print(f"  {agent_type:12s} → {', '.join(skill_names)}")

    if kv_set_fn:
        print("\n✅ 初始化完成！重启后端后 Skill 池将从数据库自动加载。")
    else:
        print("\n⚠️ 初始化完成（仅 JSON 模式）。")
        print("  后端启动时将尝试从 JSON 文件自动迁移 Skill 到数据库。")
        print("  如果数据库此时不可用，请确保数据库启动后重新运行此脚本。")


if __name__ == "__main__":
    import sys
    try:
        print("=== 初始化核心 Skill 池 ===\n")
        main()
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 初始化失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
