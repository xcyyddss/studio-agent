# 第三方资源说明

Studio 随包保留离线 tokenizer 资源，用于本地上下文容量预检。该预检是代理计量，不代表模型服务端的精确 token 数量。

| 资源 | 随包位置 | 来源记录与许可 |
|---|---|---|
| `o200k_base` 词表 | `studio_runtime/tokenizer_data/o200k_base.tiktoken` | 来源及 SHA-256 记录在同目录 `o200k_base.json`；随附 MIT 许可见下方 |
| 编码定义与来源元数据 | `studio_runtime/tokenizer_data/o200k_base.json` | 记录 tiktoken 编码定义来源；与词表一起使用 |
| tiktoken 许可文本 | `studio_runtime/tokenizer_data/LICENSE.tiktoken` | 保留原有版权署名与完整许可正文 |

随包来源记录指向 [OpenAI 词表资源](https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken) 和 [tiktoken 编码定义](https://github.com/openai/tiktoken/blob/0.14.0/tiktoken_ext/openai_public.py)。词表 SHA-256 为：

```text
446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d
```

请保留 [LICENSE.tiktoken](studio_runtime/tokenizer_data/LICENSE.tiktoken)，并保持词表原始字节不变；运行时会校验哈希。第三方词表许可不代表 Studio 自身已采用同一许可证，项目授权状态见 [README](README.md#许可证)。

Python 依赖由包管理器安装，未将其源码复制进本仓库。Codex CLI、Poppler 和浏览器也不随本项目分发；使用和分发这些组件时应分别查阅其提供方条款。
