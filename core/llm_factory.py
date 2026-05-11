"""LLM 工厂 — 根据 provider 配置创建对应的 LLM 实例。

支持两种模式：
- Anthropic 原生端点 → ChatAnthropic
- OpenAI 兼容端点（MiMo、DeepSeek 等）→ ChatOpenAI
"""

import logging
import settings

logger = logging.getLogger(__name__)

# 已知使用 OpenAI 兼容端点的 provider
OPENAI_COMPAT_PROVIDERS = {"mimo", "deepseek", "openai"}


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

    # 判断走 OpenAI 兼容还是 Anthropic 原生
    if cfg_provider.lower() in OPENAI_COMPAT_PROVIDERS or "/v1" in base_url:
        return _create_openai_compat(use_model, base_url, api_key, max_tokens)
    else:
        return _create_anthropic(use_model, base_url, api_key, max_tokens)


def _create_openai_compat(model, base_url, api_key, max_tokens):
    """创建 OpenAI 兼容端点的 LLM 实例。"""
    from langchain_openai import ChatOpenAI

    # 将 Anthropic 兼容路径转换为 OpenAI 兼容路径
    openai_url = base_url
    if "/anthropic" in openai_url:
        openai_url = openai_url.replace("/anthropic", "/v1")
    elif not openai_url.endswith("/v1"):
        openai_url = openai_url.rstrip("/") + "/v1"

    logger.info(f"使用 OpenAI 兼容端点: {openai_url}, model={model}")
    return ChatOpenAI(
        model=model,
        base_url=openai_url,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=0.7,
    )


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