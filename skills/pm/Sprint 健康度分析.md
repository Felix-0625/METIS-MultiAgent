# Sprint 健康度分析

- **版本**: 2.0.0
- **描述**: 基于历史Sprint数据进行蒙特卡洛速度预测，多维度Sprint健康评分，回顾会议分析
- **标签**: pm, sprint, scrum, velocity
- **分配给**: pm

---

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