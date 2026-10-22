# astrbot_plugin_thought_guard

在 AstrBot 发送消息前，使用 [Jev](https://typesafe.ai) 类决策模型检测回复是否泄露了思考内容（思维链、自言自语、对设定的分析等）：

- 未泄露：直接放行；
- 泄露：截断原回复，交由配置中指定的模型删去思考内容后再发送。

决策模型使用独立的 HTTP 协议（非 OpenAI 兼容），AstrBot 内无法直接添加，因此其 Base URL 与 Key 在插件配置中填写。

## 安装

将本仓库放入 `data/plugins/astrbot_plugin_thought_guard/`，或在 AstrBot WebUI 中通过仓库地址安装，然后重载插件。依赖：`aiohttp`（AstrBot 已自带）。

## 配置

在 WebUI 插件配置中设置，主要项如下（示例已脱敏）：

| 配置项 | 说明 | 示例 |
| --- | --- | --- |
| `jev_base_url` | 决策模型服务地址 | `https://api.typesafe.ai` |
| `jev_endpoint` | 决策接口路径 | `/v1/systemone` |
| `jev_api_key` | 决策模型 Key（Bearer） | `sk-xxxx` |
| `jev_model` | 决策模型名称 | `jev-latest` |
| `threshold` | 概率 ≥ 该值视为泄露 | `0.5` |
| `cleaner_provider_id` | 清理思考内容所用模型，留空为当前会话模型 | — |
| `fallback_message` | 清理失败时发送的文本，留空则丢弃文字部分 | — |

插件向决策接口发送如下请求，并读取 `answers.leaks_reasoning.noul`（或顶层 `leaks_reasoning.noul`）作为泄露概率：

```json
{
  "model": "jev-latest",
  "state": "<待发送的回复文本>",
  "questions": {
    "leaks_reasoning": { "type": "noul", "instructions": "<判定指令>" }
  }
}
```

兼容同一请求/响应格式的其他决策服务也可使用。

## 已知限制

- 开启流式输出时，消息在钩子执行前已发出，插件无法拦截，会直接跳过。
- 仅检查和替换消息链中的纯文本部分；图片等其他组件保持不变。
- 决策模型不可用或未配置时默认放行（fail-open），并在日志中给出警告。
- 每条被检查的回复会多一次决策请求；判定为泄露时再多一次清理模型调用。

## 许可证

本项目使用 [AGPL-3.0](LICENSE) 协议。
