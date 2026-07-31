# Defect管理

- **版本**: 1.0.0
- **描述**: 管理 needs_manual 缺陷单的生命周期：接收→对话→整改→验证→关闭
- **标签**: fullstack_engineer, defect, lifecycle
- **分配给**: fullstack_engineer

---

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