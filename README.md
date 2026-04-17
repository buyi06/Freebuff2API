# Freebuff2API

将 [Freebuff / Codebuff](https://www.codebuff.com) 的 Agent 接口反向代理成 OpenAI 兼容的 `/v1/chat/completions` API，方便任何支持 OpenAI 协议的客户端（Cherry Studio、Chatbox、LobeChat、OpenWebUI、LangChain、OpenAI SDK 等）直接接入。

## 特性

- OpenAI 兼容：`/v1/chat/completions`、`/v1/models`
- 支持流式 (SSE) 与非流式
- 透传 `tool_calls`、`reasoning_content`（思维链）
- 自动登录 / Token 持久化（首启动走浏览器扫码）
- Agent Run 缓存与失效自动重建
- 可选 API Key 鉴权，避免公网裸跑
- 常见 OpenAI 模型名别名（`gpt-4o` / `gpt-4o-mini` 等）

## 可用模型

| model 名 | 内部 agent |
|---|---|
| `minimax/minimax-m2.7` | base2-free |
| `z-ai/glm-5.1` | base2-free |
| `google/gemini-2.5-flash-lite` | file-picker |
| `google/gemini-3.1-flash-lite-preview` | file-picker-max |
| `google/gemini-3.1-pro-preview` | thinker-with-files-gemini |

> 上游对 free 档实际可用模型可能变化，如返回 500/空 body，多半是该模型在上游不开放。

## 安装

```bash
pip install aiohttp
```

要求 Python ≥ 3.10。

## 快速开始

```bash
python code.py --host 0.0.0.0 --port 1145
```

首次启动如未登录，会输出登录 URL 并尝试打开浏览器，完成登录后回车继续即可，凭据写入：

- Windows: `%APPDATA%\manicode\credentials.json`
- Linux/macOS: `~/.config/manicode/credentials.json`

## 设置访问 API Key（强烈建议）

监听 `0.0.0.0` 时务必设置 Key，否则局域网/公网任何人都能用你的额度。

```bash
# 方式 1：环境变量（最高优先级）
export FREEBUFF_PROXY_API_KEY=sk-xxxxxx

# 方式 2：持久化到配置文件
python code.py set-api-key sk-xxxxxx
python code.py show-api-key
python code.py clear-api-key
```

客户端以下任一 Header 均可：

```
Authorization: Bearer sk-xxxxxx
x-api-key: sk-xxxxxx
api-key:    sk-xxxxxx
```

## 调用示例

```bash
curl http://127.0.0.1:1145/v1/chat/completions \
  -H "Authorization: Bearer sk-xxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "minimax/minimax-m2.7",
    "messages": [{"role":"user","content":"你好"}],
    "stream": false
  }'
```

OpenAI SDK：

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://127.0.0.1:1145/v1",
    api_key="sk-xxxxxx",
)
resp = client.chat.completions.create(
    model="minimax/minimax-m2.7",
    messages=[{"role": "user", "content": "9.11 和 9.9 哪个大？"}],
)
print(resp.choices[0].message.content)
# 若模型返回思维链，可访问 resp.choices[0].message.reasoning_content
```

## 路由

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | 主聊天接口 |
| GET  | `/v1/models` | 模型列表 |
| POST | `/v1/reset-run` | 清除缓存的 Agent Run，强制下次重建 |
| POST | `/v1/reload-key` | 用旧 Key 鉴权后，从磁盘/环境重读 API Key |
| GET  | `/health` | 健康检查 |

## 命令行参数

```
--host         监听地址 (默认 127.0.0.1)
--port         监听端口 (默认 1145)
--log-level    DEBUG/INFO/WARNING/ERROR
--log-file     日志文件路径
--lazy         不预热 Agent Run，首个请求时再创建
```

## 常见问题

- **某模型一直返回 500 / 空错误体**：上游 `codebuff.com` 不接受该 `model` 名或不对 free 档开放，与代理无关。
- **看不到思维链**：上游返回 `reasoning_content` 时已透传；客户端侧需要读取 `message.reasoning_content`（非流式）或每个 chunk `delta.reasoning_content`（流式）。
- **`登录超时`**：5 分钟内未完成浏览器登录；重新运行即可。
- **Windows 下路径含空格/中文**：用引号包住路径再启动 Python。

## Freebuff 支持哪些国家/地区？

目前 Freebuff 仅在以下国家/地区开放使用：

美国、加拿大、英国、澳大利亚、新西兰、挪威、瑞典、荷兰、丹麦、德国、芬兰、比利时、卢森堡、瑞士、爱尔兰、冰岛。

不在上述地区的用户需要自备相应节点的网络环境才能正常登录与调用。

## 免责声明

本项目仅供学习与个人研究。请遵守上游服务条款；因滥用造成的账号或法律风险由使用者自行承担。
