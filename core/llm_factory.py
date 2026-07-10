"""LLM 工厂 — 根据 provider 配置创建对应的 LLM 实例。

支持两种模式：
- Anthropic 原生端点 → ChatAnthropic
- OpenAI 兼容端点（MiMo、DeepSeek、Moonshot 等）→ ChatOpenAI / MiMoChatOpenAI
"""

import logging
from typing import Optional

from langchain_openai import ChatOpenAI

import settings
from core.multimodal.model_capabilities import supports_vision

logger = logging.getLogger(__name__)

# 已知使用 OpenAI 兼容端点的 provider
OPENAI_COMPAT_PROVIDERS = {"mimo", "deepseek", "openai", "moonshot"}


def create_llm(provider: str = None, model: str = None, max_tokens: int = 4096):
    """创建 LLM 实例。

    根据 config.yaml 中的 provider 配置自动选择 ChatOpenAI 或 ChatAnthropic。
    """
    cfg_provider = provider or settings.get("llm.provider", "mimo")
    cfg_model = model or settings.get("llm.model", "mimo-v2.5-pro")
    cfg_temperature = settings.get("llm.temperature", 0)

    # 查找 provider 配置
    providers_cfg = settings.get("providers", {})
    provider_cfg = providers_cfg.get(cfg_provider, {})

    if not provider_cfg:
        logger.warning(f"未找到 provider 配置: {cfg_provider}，使用默认 MiMo")
        provider_cfg = providers_cfg.get("mimo", {})

    base_url = provider_cfg.get("base_url", "")
    api_key = _resolve_key(provider_cfg.get("api_key_env", ""))
    default_model = provider_cfg.get("default_model", cfg_model)

    # 确定使用的模型
    use_model = model or default_model or cfg_model

    # 若当前模型支持 vision 且 provider 配置了 vision 专用 endpoint，则切换
    if supports_vision(use_model):
        vision_base_url = provider_cfg.get("vision_base_url", "")
        if vision_base_url:
            base_url = vision_base_url
            logger.info(f"模型 {use_model} 支持 vision，使用 vision endpoint: {base_url}")

    # 判断走 OpenAI 兼容还是 Anthropic 原生
    if cfg_provider.lower() in OPENAI_COMPAT_PROVIDERS or "/v1" in base_url:
        return _create_openai_compat(use_model, base_url, api_key, max_tokens)
    else:
        return _create_anthropic(use_model, base_url, api_key, max_tokens)


class MiMoChatOpenAI(ChatOpenAI):
    """ChatOpenAI 子类 — 从原始 API 响应中捕获 MiMo 的 reasoning_content。

    MiMo thinking 模式返回的 reasoning_content 被 LangChain 丢弃，
    此子类在 _create_chat_result 中拦截原始响应，将其保存到 additional_kwargs。
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._last_reasoning_content: Optional[str] = None

    def _create_chat_result(self, response, generation_info=None):
        # 先从原始响应中提取 reasoning_content（在 LangChain 丢弃之前）
        reasoning_by_index = {}
        if hasattr(response, "choices"):
            for i, choice in enumerate(response.choices):
                msg = choice.message
                if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                    reasoning_by_index[i] = msg.reasoning_content

        # 调用父类完成正常转换
        result = super()._create_chat_result(response, generation_info)

        # 将 reasoning_content 注入回 AIMessage 的 additional_kwargs
        for i, reasoning in reasoning_by_index.items():
            if i < len(result.generations):
                gen = result.generations[i]
                if hasattr(gen, "message") and hasattr(gen.message, "additional_kwargs"):
                    gen.message.additional_kwargs["reasoning_content"] = reasoning
                    self._last_reasoning_content = reasoning

        return result

    def _stream(self, *args, **kwargs):
        """禁用 streaming，强制走 invoke → _create_chat_result 路径。

        MiMo thinking 模式的 reasoning_content 在 streaming 路径中被
        langchain-openai 的 _convert_delta_to_message_chunk 彻底丢弃。
        禁用 streaming 后，agent_loop 会回退到 invoke()，
        调用 _create_chat_result → 我们的重写能正确捕获 reasoning_content。
        """
        yield from ()

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        """重写消息序列化，确保 reasoning_content 传递到 API 请求。

        LangChain 的 _convert_message_to_dict 不处理 additional_kwargs["reasoning_content"]，
        导致 MiMo thinking 模式要求的 reasoning_content 在序列化时被丢弃。
        此方法在父类完成序列化后，将 reasoning_content 注入回消息 dict。
        """
        from langchain_core.messages import AIMessage

        # 先将输入转为 BaseMessage 列表，记录每个 AIMessage 的 reasoning_content
        messages = self._convert_input(input_).to_messages()
        reasoning_map = {}
        for i, m in enumerate(messages):
            if isinstance(m, AIMessage):
                rc = m.additional_kwargs.get("reasoning_content")
                if rc:
                    reasoning_map[i] = rc

        # 调用父类生成 payload（消息被序列化为 dict）
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # 将 reasoning_content 注入到序列化后的消息 dict 中
        msg_list = payload.get("messages", [])
        for i, rc in reasoning_map.items():
            if i < len(msg_list) and isinstance(msg_list[i], dict) and msg_list[i].get("role") == "assistant":
                msg_list[i]["reasoning_content"] = rc

        return payload


def _create_openai_compat(model, base_url, api_key, max_tokens):
    """创建 OpenAI 兼容端点的 LLM 实例。"""

    # 将 Anthropic 兼容路径转换为 OpenAI 兼容路径
    openai_url = base_url
    if "/anthropic" in openai_url:
        openai_url = openai_url.replace("/anthropic", "/v1")
    elif not openai_url.endswith("/v1"):
        openai_url = openai_url.rstrip("/") + "/v1"

    is_mimo = "xiaomimimo" in openai_url or "mimo" in model.lower()

    logger.info(f"使用 OpenAI 兼容端点: {openai_url}, model={model}")
    kwargs = {
        "model": model,
        "base_url": openai_url,
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    # MiMo 模型需要开启 thinking 模式（返回 reasoning_content）
    if is_mimo:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return MiMoChatOpenAI(**kwargs)
    return ChatOpenAI(**kwargs)


def _create_anthropic(model, base_url, api_key, max_tokens):
    """创建 Anthropic 原生端点的 LLM 实例。"""
    from langchain_anthropic import ChatAnthropic

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    if api_key:
        kwargs["anthropic_api_key"] = api_key
    if base_url:
        kwargs["anthropic_api_url"] = base_url

    logger.info(f"使用 Anthropic 原生端点: {base_url}, model={model}")
    return ChatAnthropic(**kwargs)


def _resolve_key(key_or_env: str) -> str:
    """解析 API key：如果是环境变量名则读取，否则直接返回。"""
    import os
    if not key_or_env:
        return os.environ.get("ANTHROPIC_API_KEY", "")
    resolved = os.environ.get(key_or_env, key_or_env)
    return resolved