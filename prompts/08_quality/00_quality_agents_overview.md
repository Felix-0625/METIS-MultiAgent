# 质检 Agent 总览（算法实现≠Prompt）

> 来源: AI Multi-Agent System v2.4.0

---

## 质检 Agent 说明

QAAgent、PerfAgent、SecAgent、UXOAgent 的前两层检查（Layer1 语法、Layer2 逻辑）
和三个专项 Agent 的全部检查方法都是**纯算法实现**，不使用 LLM System Prompt。

具体方法：
- QAAgent.inspect() → Layer1 AST解析 + Layer2 AST Walk + Layer3 LLM调用 + Layer4 跨文件检查
- PerfAgent.inspect() → N+1查询扫描 + 同步阻塞检测 + Core Web Vitals
- SecAgent.inspect() → 正则漏洞扫描 + 密钥泄露检测 + 合规检查
- UXOAgent.inspect() → UI审查 + 等待时间检测 + 导航深度检查

仅 Layer3（功能检查）使用了 LLM 调用。
质检的评分逻辑是算法扣分制，不是 LLM 主观判断。
