import aiohttp
import logging

logger = logging.getLogger(__name__)


class TelegramNotifier:
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str):
        self.token    = token
        self.chat_id  = chat_id
        self._url     = self.API_URL.format(token=token)

    async def send(self, text: str):
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(f"[Telegram] {resp.status}: {body}")
                    else:
                        logger.info(f"[Telegram] Sent OK")
        except Exception as e:
            logger.error(f"[Telegram] Error: {e}")
