"""ContextCompactor — 上下文压缩器，超出 token 阈值时自动压缩。

策略：
  - 消息 < 20 条：简单摘要（保留最近 N 轮，其余一次性 LLM 摘要）
  - 消息 >= 20 条：Map-Reduce（分片摘要，再合并）
  - 压缩前自动提取关键事实到长期记忆

设计参考 PaiCLI 的 Map-Reduce 策略，但简化了实现：
  - PaiCLI 用独立的 ContextCompressor 类，我们合并到 ContextCompactor
  - PaiCLI 的事实提取是独立的 extractFacts 方法，我们合并到压缩流程中
  - 保留最近 N 轮不压缩（可配置，默认 3 轮）
"""

import logging

import settings
import tiktoken
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)


class ContextCompactor:
    """上下文压缩器 — 管理 token 预算，超阈值自动压缩。"""

    def __init__(self, llm=None, memory_manager=None):
        self.threshold = settings.get("session.compaction_threshold", 80000)
        self.retain_recent = settings.get("memory.compaction.retain_recent_rounds", 3)
        self.chunk_size = settings.get("memory.compaction.chunk_size", 5)
        self.llm = llm
        self.memory_manager = memory_manager
        self._enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, messages: list) -> int:
        """计算消息列表的总 token 数。"""
        total = 0
        for m in messages:
            content = m.content if hasattr(m, "content") else str(m)
            if content:
                total += len(self._enc.encode(content))
        return total

    def should_compact(self, messages: list) -> bool:
        """判断是否需要压缩。"""
        return self.count_tokens(messages) > self.threshold

    def compact(self, messages: list) -> list:
        """压缩消息列表。返回压缩后的消息列表。

        流程：
        1. 分离 system 消息和对话消息
        2. 提取旧消息中的关键事实到长期记忆
        3. 根据消息数量选择压缩策略（简单/Map-Reduce）
        4. 保留最近 N 轮不压缩
        """
        if not self.llm or len(messages) < 4:
            return messages

        # 分离 system 消息和对话消息
        system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
        chat_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

        if len(chat_msgs) < 4:
            return messages

        # 保留最近 N 轮（每轮 = 1 human + 1 ai，至少保留 2 条）
        retain_count = max(self.retain_recent * 2, 2)
        if len(chat_msgs) <= retain_count:
            return messages

        old_msgs = chat_msgs[:-retain_count]
        recent_msgs = chat_msgs[-retain_count:]

        # 1. 先提取关键事实到长期记忆
        if self.memory_manager:
            self._extract_facts(old_msgs)

        # 2. 压缩旧消息
        if len(old_msgs) >= 20:
            summary = self._map_reduce_summarize(old_msgs)
        else:
            summary = self._simple_summarize(old_msgs)

        # 3. 组装压缩后的消息列表
        summary_msg = SystemMessage(content=f"[对话历史摘要]\n{summary}")
        result = system_msgs + [summary_msg] + recent_msgs
        logger.info(
            f"上下文压缩完成: {len(messages)} 条 → {len(result)} 条, "
            f"token: {self.count_tokens(messages)} → {self.count_tokens(result)}"
        )
        return result

    def _simple_summarize(self, messages: list) -> str:
        """简单摘要 — 一次性 LLM 调用。"""
        conversation = self._format_messages(messages)
        prompt = (
            "请用中文简洁地总结以下对话的关键信息，保留：\n"
            "- 用户的核心需求和偏好\n"
            "- 做过的重要决策\n"
            "- 未完成的任务\n"
            "不要保留具体代码细节，只保留决策和结论。\n\n"
            f"{conversation}"
        )
        try:
            response = self.llm.invoke([SystemMessage(content=prompt)])
            return response.content
        except Exception as e:
            logger.warning(f"摘要生成失败: {e}")
            return f"[摘要生成失败，原始消息 {len(messages)} 条]"

    def _map_reduce_summarize(self, messages: list) -> str:
        """Map-Reduce 摘要 — 分片后逐片摘要，再合并。

        Map 阶段：每 chunk_size 条消息一组，独立生成摘要
        Reduce 阶段：合并所有分片摘要为最终摘要
        """
        # Map 阶段
        chunks = [
            messages[i : i + self.chunk_size]
            for i in range(0, len(messages), self.chunk_size)
        ]
        chunk_summaries = []
        for chunk in chunks:
            summary = self._simple_summarize(chunk)
            chunk_summaries.append(summary)

        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        # Reduce 阶段
        combined = "\n".join(
            f"[片段 {i + 1}] {s}" for i, s in enumerate(chunk_summaries)
        )
        prompt = (
            "请将以下多个对话片段的摘要合并为一个连贯的总结，去除重复信息：\n\n"
            f"{combined}"
        )
        try:
            response = self.llm.invoke([SystemMessage(content=prompt)])
            return response.content
        except Exception as e:
            logger.warning(f"合并摘要失败: {e}")
            return "\n".join(chunk_summaries)

    def _extract_facts(self, messages: list):
        """从对话中提取关键事实，存入长期记忆。"""
        if not self.memory_manager:
            return

        conversation = self._format_messages(messages)
        prompt = (
            "从以下对话中提取关键事实。每条事实一行，只提取跨会话仍有价值的信息：\n"
            "- 用户偏好（技术栈、编码风格、工具选择）\n"
            "- 项目信息（技术选型、架构决策、配置）\n"
            "- 重要结论和决策\n\n"
            "格式：每行一条事实，不要编号，不要解释。\n"
            "如果没有值得提取的事实，回复'无'。\n\n"
            f"{conversation}"
        )
        try:
            response = self.llm.invoke([SystemMessage(content=prompt)])
            facts_text = response.content.strip()
            if facts_text and facts_text != "无":
                count = 0
                for line in facts_text.split("\n"):
                    line = line.strip().lstrip("- •·*")
                    if line and len(line) > 5:
                        if self.memory_manager.store_fact(line):
                            count += 1
                if count > 0:
                    logger.info(f"自动提取 {count} 条事实到长期记忆")
        except Exception as e:
            logger.warning(f"事实提取失败: {e}")

    @staticmethod
    def _format_messages(messages: list) -> str:
        """格式化消息列表为可读文本。"""
        parts = []
        for m in messages:
            if isinstance(m, HumanMessage):
                parts.append(f"用户: {m.content}")
            elif isinstance(m, AIMessage):
                if m.content:
                    parts.append(f"助手: {m.content}")
                if m.tool_calls:
                    for tc in m.tool_calls:
                        parts.append(f"  [调用工具] {tc['name']}({tc.get('args', {})})")
            elif isinstance(m, ToolMessage):
                content = m.content
                if len(content) > 200:
                    content = content[:200] + "..."
                parts.append(f"  [工具结果] {content}")
        return "\n".join(parts)
