"""Клиенты нейросетей через REST: Gemini — основной, OpenAI — запасной.

Без SDK: только requests. Так меньше зависимостей, и всё легко
тестируется подменой HTTP-ответов.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)

DEFAULT_GEMINI_BASE = "https://generativelanguage.googleapis.com"


class AIError(RuntimeError):
    pass


class AIRefused(AIError):
    """Модель отказала (фильтр безопасности) — повтор тем же запросом бессмыслен."""


class AIHTTPError(AIError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


# Ответы, по которым видно: адрес не понимает запросы в формате Google.
ROUTE_MISMATCH = (400, 404, 405, 415, 501)


@dataclass
class ImageResult:
    data: bytes
    mime: str
    provider: str


def _post(url: str, *, retries: int = 3, timeout: int = 120, **kw) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(url, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = e
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    # по адресу отвечает не API, а, например, сайт посредника
                    raise AIHTTPError(200, f"ответ не JSON: {r.text[:120]}") from None
            body = _error_text(r)
            if r.status_code in (429, 500, 502, 503, 504):
                last = AIHTTPError(r.status_code, body)
            else:
                raise AIHTTPError(r.status_code, body)
        time.sleep(2 * (attempt + 1))
    raise AIError(f"не удалось после {retries} попыток: {last}")


def _error_text(r) -> str:
    """Из ответа с ошибкой берём только суть: message из JSON, а не весь ответ."""
    try:
        err = r.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
    except ValueError:
        pass
    return r.text[:300]


def _image_part(path: str | Path) -> dict:
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(p.read_bytes()).decode()}}


def parse_json(text: str) -> dict | list:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise AIError(f"в ответе нет JSON: {text[:200]}")
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start:end + 1])


def _root(url: str) -> str:
    """Адрес без хвоста /v1 или /v1beta — чтобы было неважно, как его вставили."""
    url = (url or "").strip().rstrip("/")
    return re.sub(r"/v1(beta)?$", "", url)


class AI:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # Формат запросов к Gemini: native (как у Google) или openai (как у OpenAI).
        # auto — определяется при первом запросе и запоминается до перезапуска.
        style = (getattr(cfg, "gemini_api_style", "") or "auto").lower()
        self.gemini_style: str | None = style if style in ("native", "openai") else None

    # ------------------------------------------------------------ адреса
    @property
    def _gemini_root(self) -> str:
        return _root(self.cfg.gemini_base_url or DEFAULT_GEMINI_BASE)

    @property
    def _is_google(self) -> bool:
        return self._gemini_root == DEFAULT_GEMINI_BASE

    def _gemini_url(self, model: str) -> str:
        return f"{self._gemini_root}/v1beta/models/{model}:generateContent"

    def _gemini_headers(self) -> dict:
        h = {"x-goog-api-key": self.cfg.gemini_api_key}
        if not self._is_google:
            # посредники обычно ждут ключ как Bearer-токен
            h["Authorization"] = f"Bearer {self.cfg.gemini_api_key}"
        return h

    def _openai_url(self, path: str) -> str:
        return f"{_root(self.cfg.openai_base_url)}/v1/{path}"

    def _gemini_native(self, body: dict, model: str, timeout: int = 120) -> dict:
        """Запрос к Gemini в формате Google. При auto, если посредник этот формат
        не понимает, помечает Gemini как работающий в формате OpenAI."""
        try:
            data = _post(self._gemini_url(model), headers=self._gemini_headers(), json=body, timeout=timeout)
        except AIHTTPError as e:
            if self.gemini_style is None and not self._is_google and e.status in ROUTE_MISMATCH \
                    and "api key" not in str(e).lower():
                self.gemini_style = "openai"
                raise _SwitchToOpenAI() from None
            raise
        if self.gemini_style is None:
            self.gemini_style = "native"
        return data

    # ------------------------------------------------------------- текст
    def json(self, system: str, prompt: str, images: list[str | Path] | None = None,
             temperature: float = 0.9) -> dict | list:
        """Текстовый запрос с ответом в JSON. Gemini, при сбое — OpenAI."""
        errors = []
        if self.cfg.gemini_api_key:
            try:
                return self._gemini_json(system, prompt, images or [], temperature)
            except AIError as e:
                errors.append(f"gemini: {e}")
                log.warning("Gemini text failed: %s", e)
        if self.cfg.openai_api_key:
            try:
                return self._openai_json(system, prompt, images or [], temperature)
            except AIError as e:
                errors.append(f"openai: {e}")
        raise AIError("; ".join(errors) or "не задан ни GEMINI_API_KEY, ни OPENAI_API_KEY")

    def _gemini_json(self, system, prompt, images, temperature):
        if self.gemini_style == "openai":
            return self._chat_json(f"{self._gemini_root}/v1/chat/completions", self.cfg.gemini_api_key,
                                   self.cfg.gemini_text_model, system, prompt, images, temperature)
        parts = [{"text": prompt}] + [_image_part(p) for p in images]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        try:
            data = self._gemini_native(body, self.cfg.gemini_text_model)
        except _SwitchToOpenAI:
            return self._gemini_json(system, prompt, images, temperature)
        text = "".join(p.get("text", "") for p in _gemini_parts(data))
        return parse_json(text)

    def _openai_json(self, system, prompt, images, temperature):
        return self._chat_json(self._openai_url("chat/completions"), self.cfg.openai_api_key,
                               self.cfg.openai_text_model, system, prompt, images, temperature)

    @staticmethod
    def _chat_json(url, key, model, system, prompt, images, temperature):
        """Запрос в формате OpenAI Chat Completions (его понимают и многие посредники)."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        for p in images:
            part = _image_part(p)["inlineData"]
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{part['mimeType']};base64,{part['data']}"}})
        body = {
            "model": model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        data = _post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
        try:
            return parse_json(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise AIError(f"неожиданный ответ: {str(data)[:200]}") from None

    def research(self, system: str, prompt: str, temperature: float = 0.4) -> tuple[dict | list, list[dict]]:
        """Запрос к Gemini с поиском Google. Возвращает (JSON из ответа, источники).

        Поиск работает только у Gemini; у OpenAI в этом режиме нет доступа к
        свежему интернету, поэтому запасного провайдера здесь нет.
        """
        if not self.cfg.gemini_api_key:
            raise AIError("для разведки трендов нужен GEMINI_API_KEY (поиск Google есть только у Gemini)")
        if self.gemini_style == "openai":
            raise AIError("посредник Gemini принимает запросы только в формате OpenAI, а поиск Google "
                          "доступен лишь в формате Google — разведка трендов через этот адрес недоступна")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": temperature},
        }
        try:
            data = self._gemini_native(body, self.cfg.gemini_text_model, timeout=180)
        except _SwitchToOpenAI:
            return self.research(system, prompt, temperature)
        text = "".join(p.get("text", "") for p in _gemini_parts(data))
        meta = (data.get("candidates") or [{}])[0].get("groundingMetadata") or {}
        sources, seen = [], set()
        for ch in meta.get("groundingChunks") or []:
            web = ch.get("web") or {}
            uri = web.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                sources.append({"title": web.get("title") or uri, "uri": uri})
        return parse_json(text), sources

    def check(self) -> list[str]:
        """Проверка ключей и моделей: по строке на каждого провайдера."""
        out = []
        probe = ('Ответь JSON {"ok": true}', "проверка связи")
        for name, key, fn, model, base in (
            ("Gemini", self.cfg.gemini_api_key, self._gemini_json, self.cfg.gemini_text_model,
             self.cfg.gemini_base_url or DEFAULT_GEMINI_BASE),
            ("OpenAI", self.cfg.openai_api_key, self._openai_json, self.cfg.openai_text_model,
             self.cfg.openai_base_url),
        ):
            host = base.split("://", 1)[-1].split("/", 1)[0]
            if not key:
                out.append(f"{name}: ключ не задан")
                continue
            try:
                fn(*probe, [], 0.0)
                fmt = ""
                if name == "Gemini" and not self._is_google:
                    fmt = ", формат " + ("Google" if self.gemini_style == "native" else "OpenAI")
                out.append(f"✅ {name}: отвечает (модель {model}, {host}{fmt})")
            except (AIError, KeyError, ValueError) as e:
                out.append(f"❌ {name} (модель {model}, {host}): {str(e)[:300]}")
        return out

    # ---------------------------------------------------------- картинки
    def image(self, prompt: str, refs: list[str | Path]) -> ImageResult:
        """Генерация фото по референсам. Gemini, при сбое — OpenAI."""
        errors = []
        if self.cfg.gemini_api_key:
            try:
                return self._gemini_image(prompt, refs)
            except AIError as e:
                errors.append(f"gemini: {e}")
                log.warning("Gemini image failed: %s", e)
        if self.cfg.openai_api_key:
            try:
                return self._openai_image(prompt, refs)
            except AIError as e:
                errors.append(f"openai: {e}")
        raise AIError("; ".join(errors) or "не задан ни GEMINI_API_KEY, ни OPENAI_API_KEY")

    def _gemini_image(self, prompt, refs):
        if self.gemini_style == "openai":
            raise AIError("генерация картинок Gemini через посредника в формате OpenAI не поддерживается")
        parts = [_image_part(p) for p in refs] + [{"text": prompt}]
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}},
        }
        try:
            data = self._gemini_native(body, self.cfg.gemini_image_model, timeout=180)
        except _SwitchToOpenAI:
            return self._gemini_image(prompt, refs)
        for p in _gemini_parts(data):
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return ImageResult(base64.b64decode(inline["data"]),
                                   inline.get("mimeType") or inline.get("mime_type") or "image/png", "gemini")
        raise AIRefused("Gemini не вернул изображение (вероятно, фильтр безопасности)")

    def _openai_image(self, prompt, refs):
        files = []
        for p in refs[:10]:
            p = Path(p)
            files.append(("image[]", (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/jpeg")))
        form = {"model": self.cfg.openai_image_model, "prompt": prompt, "size": "1024x1536",
                "quality": "high", "input_fidelity": "high"}
        headers = {"Authorization": f"Bearer {self.cfg.openai_api_key}"}
        try:
            data = _post(self._openai_url("images/edits"), timeout=240, headers=headers, data=form, files=files)
        except AIError as e:
            # input_fidelity поддерживают не все модели — повторяем без него.
            if "input_fidelity" not in str(e):
                raise
            form.pop("input_fidelity")
            data = _post(self._openai_url("images/edits"), timeout=240, headers=headers, data=form, files=files)
        item = (data.get("data") or [{}])[0]
        if not item.get("b64_json"):
            raise AIRefused("OpenAI не вернул изображение")
        return ImageResult(base64.b64decode(item["b64_json"]), "image/png", "openai")


class _SwitchToOpenAI(Exception):
    """Внутренний сигнал: повторить запрос к Gemini в формате OpenAI."""


def _gemini_parts(data: dict) -> list[dict]:
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "нет кандидатов")
        raise AIRefused(f"Gemini: {reason}")
    content = cands[0].get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        raise AIRefused(f"Gemini: пустой ответ ({cands[0].get('finishReason')})")
    return parts
