# API 设计

- **版本**: 1.0.0
- **描述**: 负责 RESTful/GraphQL API 接口设计，包括路由规划、请求响应格式、版本管理
- **标签**: pg, api, rest, openapi
- **分配给**: pg

---

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