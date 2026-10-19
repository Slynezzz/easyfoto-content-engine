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
                return r.json()
            body = _error_text(r)
            if r.status_code in (429, 500, 502, 503, 504):
                last = AIError(f"HTTP {r.status_code}: {body}")
            else:
                raise AIError(f"HTTP {r.status_code}: {body}")
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


class AI:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------ адреса
    def _gemini_url(self, model: str) -> str:
        base = self.cfg.gemini_base_url or DEFAULT_GEMINI_BASE
        return f"{base}/v1beta/models/{model}:generateContent"

    def _gemini_headers(self) -> dict:
        h = {"x-goog-api-key": self.cfg.gemini_api_key}
        if (self.cfg.gemini_base_url or DEFAULT_GEMINI_BASE) != DEFAULT_GEMINI_BASE:
            # посредники обычно ждут ключ как Bearer-токен
            h["Authorization"] = f"Bearer {self.cfg.gemini_api_key}"
        return h

    def _openai_url(self, path: str) -> str:
        return f"{self.cfg.openai_base_url}/{path}"

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
        parts = [{"text": prompt}] + [_image_part(p) for p in images]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        data = _post(self._gemini_url(self.cfg.gemini_text_model),
                     headers=self._gemini_headers(), json=body)
        text = "".join(p.get("text", "") for p in _gemini_parts(data))
        return parse_json(text)

    def _openai_json(self, system, prompt, images, temperature):
        content: list[dict] = [{"type": "text", "text": prompt}]
        for p in images:
            part = _image_part(p)["inlineData"]
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{part['mimeType']};base64,{part['data']}"}})
        body = {
            "model": self.cfg.openai_text_model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        data = _post(self._openai_url("chat/completions"), headers={"Authorization": f"Bearer {self.cfg.openai_api_key}"}, json=body)
        return parse_json(data["choices"][0]["message"]["content"])

    def research(self, system: str, prompt: str, temperature: float = 0.4) -> tuple[dict | list, list[dict]]:
        """Запрос к Gemini с поиском Google. Возвращает (JSON из ответа, источники).

        Поиск работает только у Gemini; у OpenAI в этом режиме нет доступа к
        свежему интернету, поэтому запасного провайдера здесь нет.
        """
        if not self.cfg.gemini_api_key:
            raise AIError("для разведки трендов нужен GEMINI_API_KEY (поиск Google есть только у Gemini)")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": temperature},
        }
        data = _post(self._gemini_url(self.cfg.gemini_text_model),
                     headers=self._gemini_headers(), json=body, timeout=180)
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
                out.append(f"✅ {name}: отвечает (модель {model}, {host})")
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
        parts = [_image_part(p) for p in refs] + [{"text": prompt}]
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}},
        }
        data = _post(self._gemini_url(self.cfg.gemini_image_model),
                     headers=self._gemini_headers(), json=body, timeout=180)
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
