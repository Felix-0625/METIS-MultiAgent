# PMAgent 方案设计 Prompt

> 来源: AI Multi-Agent System v2.4.0

---

基于以下需求，设计完整的解决方案。

请严格按照以下 JSON 格式输出（不要有其他文字）：
{
  "overview": "整体方案描述",
  "architecture": "系统架构说明",
  "tech_stack": ["技术1", "技术2"],
  "mermaid_flow": "stateDiagram-v2\n  [*] --> 规划\n  规划 --> 执行",
  "modules": [
    {
      "name": "模块名称",
      "description": "模块描述",
      "requirements": ["需求1", "需求2"],
      "tech_stack": ["React", "TypeScript"],
      "priority": "high"
    }
  ]
}

要求：
1. modules 数组必须包含 2-6 个子模块
2. 每个模块的 tech_stack 要具体（如 React、FastAPI、PostgreSQL）
3. mermaid_flow 使用 stateDiagram-v2 格式
4. priority 取值：high / normal / low
