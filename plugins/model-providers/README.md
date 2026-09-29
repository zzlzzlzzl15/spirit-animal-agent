# Model Provider Plugins

每个子目录是一个自包含的 provider profile 插件，移植自 Hermes
`plugins/model-providers/`。目录布局：

```
plugins/model-providers/
├── openai/
│   ├── __init__.py      # 构造并 register_provider(ProviderProfile)
│   └── plugin.yaml      # 清单：name, kind, version, description, author
├── anthropic/
│   ├── __init__.py
│   └── plugin.yaml
└── ...
```

## 发现机制

`spirit/providers/__init__.py::_discover_providers()` 在首次有代码调用
`get_provider_profile()` 或 `list_providers()` 时扫描本目录（以及
`~/.spirit/plugins/model-providers/`），import 每个 `__init__.py`，后者应调用
`spirit.providers.register_provider(profile)`。

`~/.spirit/plugins/model-providers/<name>/` 下的**用户插件覆盖同名内置插件**
（`register_provider()` 里 last-writer-wins）。丢一个文件到那里即可替换内置 profile。

## 新增一个 provider

1. 建 `plugins/model-providers/<your_provider>/__init__.py`：

   ```python
   from spirit.providers import register_provider
   from spirit.providers.base import ProviderProfile

   my_provider = ProviderProfile(
       name="your-provider",
       aliases=("alias1", "alias2"),
       api_mode="chat_completions",   # chat_completions|anthropic_messages|gemini|bedrock
       display_name="Your Provider",
       description="选择器里展示的一行描述",
       signup_url="https://your-provider.example.com/keys",
       env_vars=("YOUR_PROVIDER_API_KEY",),
       base_url="https://api.your-provider.example.com/v1",
   )

   register_provider(my_provider)
   ```

2. 建 `plugins/model-providers/<your_provider>/plugin.yaml`：

   ```yaml
   name: your-provider-profile
   kind: model-provider
   version: 1.0.0
   description: Short sentence about the provider
   author: Your Name
   ```

无需改动其它任何地方：`config.py`（base_url/env_vars 解析）、
`agent/transports/factory.py`（api_mode→transport 路由 + hostname 反查探测）、
`llm_pool.py`（failover 凭据解析）都会自动从注册表读取。

## api_mode 约定

`api_mode` 是 provider→transport 的映射键，取值限定为 Spirit transport 能路由的 4 类：

| api_mode | Transport |
|----------|-----------|
| `chat_completions` | OpenAITransport（含所有 OpenAI 兼容端点） |
| `anthropic_messages` | AnthropicTransport |
| `gemini` | GeminiTransport |
| `bedrock` | BedrockTransport |

## 非平凡 profile

在子类里覆盖 `ProviderProfile` 的 hook 处理各 provider 怪癖 ——
`openrouter/__init__.py` 是 `build_extra_body` 范例，`custom/__init__.py` 是
`build_api_kwargs_extras` + `fetch_models` 范例，`bedrock/__init__.py` 是无 REST
目录时 `fetch_models` 返回 None 的范例。
