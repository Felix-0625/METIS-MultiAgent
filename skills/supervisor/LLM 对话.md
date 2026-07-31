# LLM 对话

- **版本**: 1.0.0
- **描述**: 调用配置的 LLM API 进行智能对话，支持上下文管理和工具调用
- **标签**: common, llm, chat
- **分配给**: pm, pg, supervisor, hr, ccb

---

## LLM 对话 Skill

### 功能
- 多轮对话（上下文保持）
- 系统提示词注入
- 工具调用（Function Calling）
- 流式输出

### 支持模型
- OpenAI：gpt-4o / gpt-4-turbo / gpt-3.5-turbo
- DeepSeek：deepseek-chat / deepseek-coder
- Kimi：moonshot-v1-8k / moonshot-v1-32k
- Qwen：qwen-turbo / qwen-plus / qwen-max
- 本地：Ollama 兼容模型

### 配置
通过「系统设置」配置默认 API，或为每个 Agent 单独配置