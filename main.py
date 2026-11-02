import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import (
    DecisionBooleanAnswer,
    DecisionBooleanQuestion,
    DecisionError,
)
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import ResultContentType

QUESTION_KEY = "leaks_reasoning"
THINK_TAG_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


class ThoughtGuard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def _detect(self, text: str) -> float | None:
        """调用 AstrBot 默认决策模型，返回泄露概率；失败返回 None。"""
        cfg = self.config
        try:
            answers = await self.context.decision.ask(
                {
                    QUESTION_KEY: DecisionBooleanQuestion(
                        instructions=cfg.get("detect_instructions", "")
                    )
                },
                state=text[: int(cfg.get("max_check_chars", 6000))],
            )
            answer = answers[QUESTION_KEY]
            if not isinstance(answer, DecisionBooleanAnswer):
                raise DecisionError("Decision provider returned no boolean answer")
            return answer.probability
        except Exception as e:
            logger.warning(f"[thought_guard] 决策模型调用失败: {type(e).__name__}: {e}")
            return None

    async def _clean(self, event: AstrMessageEvent, text: str) -> str | None:
        """要求指定模型删去思考内容；失败返回 None。"""
        provider_id = self.config.get("cleaner_provider_id", "")
        try:
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(
                    event.unified_msg_origin
                )
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=self.config.get("cleaner_system_prompt", ""),
                prompt=f"<reply>\n{text}\n</reply>",
            )
            cleaned = THINK_TAG_RE.sub("", resp.completion_text or "").strip()
            return cleaned or None
        except Exception as e:
            logger.error(f"[thought_guard] 清理模型调用失败: {type(e).__name__}: {e}")
            return None

    @filter.on_decorating_result()
    async def guard(self, event: AstrMessageEvent):
        result = event.get_result()
        if result is None or not result.chain or not self.config.get("enabled", True):
            return
        if result.result_content_type == ResultContentType.STREAMING_FINISH:
            return  # 流式输出已发送，无法拦截
        if self.config.get("only_llm_result", True) and not result.is_llm_result():
            return
        text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if len(text.strip()) < int(self.config.get("min_chars", 1)):
            return

        prob = await self._detect(text)
        threshold = float(self.config.get("threshold", 0.5))
        if prob is None or prob < threshold:
            return
        logger.info(f"[thought_guard] 检测到思考内容泄露 (p={prob:.3f})，交由模型清理")

        cleaned = await self._clean(event, text)
        if cleaned is None:
            fallback = self.config.get("fallback_message", "")
            logger.warning("[thought_guard] 清理失败，已拦截原消息")
            cleaned = fallback
        new_chain, inserted = [], False
        for comp in result.chain:
            if isinstance(comp, Plain):
                if not inserted and cleaned:
                    new_chain.append(Plain(cleaned))
                inserted = True
            else:
                new_chain.append(comp)
        result.chain = new_chain
