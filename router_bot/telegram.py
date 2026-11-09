import requests

from .util import DomainError


class Telegram:
    def __init__(self, token, client=None):
        self.token, self.client = token, client or requests.Session()

    def call(self, method, data=None, timeout=40):
        try:
            r = self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}",
                json=data or {},
                timeout=timeout,
            )
            body = r.json()
            if body.get("ok"):
                return body["result"]
            message = (
                "Telegram: превышен лимит запросов"
                if body.get("error_code") == 429
                else "Telegram: запрос отклонён"
            )
        except Exception:
            message = "Telegram: доставка не подтверждена"
        raise DomainError(message, "network")

    def send(self, chat, text, keyboard=None):
        data = {"chat_id": chat, "text": str(text)[:4000]}
        if keyboard:
            data["reply_markup"] = keyboard
        return self.call("sendMessage", data)

    def answer(self, identifier, text=""):
        return self.call("answerCallbackQuery", {"callback_query_id": identifier, "text": text})

    def file(self, chat, name, content):
        try:
            r = self.client.post(
                f"https://api.telegram.org/bot{self.token}/sendDocument",
                data={"chat_id": chat},
                files={"document": (name, content.encode("utf-8"), "text/csv;charset=utf-8")},
                timeout=30,
            )
            if r.json().get("ok"):
                return
        except Exception:
            pass
        raise DomainError("Telegram: отправка файла не подтверждена", "network")
