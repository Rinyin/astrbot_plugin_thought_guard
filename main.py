import re

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import ResultContentType

QUESTION_KEY = "leaks_reasoning"
THINK_TAG_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


class ThoughtGuard(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _detect(self, text: str) -> float | None:
        """调用 Jev 类决策模型，返回“泄露思考内容”的概率；失败返回 None。"""
        cfg = self.config
        api_key = cfg.get("jev_api_key", "")
        base_url = cfg.get("jev_base_url", "").rstrip("/")
        if not api_key or not base_url:
            logger.warning(
                "[thought_guard] 未配置决策模型 base_url 或 api_key，跳过检测"
            )
            return None
        url = base_url + "/" + cfg.get("jev_endpoint", "/v1/systemone").lstrip("/")
        payload = {
            "model": cfg.get("jev_model", "jev-latest"),
            "state": text[: int(cfg.get("max_check_chars", 6000))],
            "questions": {
                QUESTION_KEY: {
                    "type": "noul",
                    "instructions": cfg.get("detect_instructions", ""),
                }
            },
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = aiohttp.ClientTimeout(total=float(cfg.get("jev_timeout", 15)))
        try:
            async with self._get_session().post(
                url, json=payload, headers=headers, timeout=timeout
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    logger.warning(
                        f"[thought_guard] 决策模型返回 {resp.status}: {body}"
                    )
                    return None
                data = await resp.json(content_type=None)
            answer = (data.get("answers") or data)[QUESTION_KEY]
            value = answer.get("noul") if isinstance(answer, dict) else answer
            return float(value)
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

    async def terminate(self):
        if self._session and not self._session.closed:
            await self._session.close()
