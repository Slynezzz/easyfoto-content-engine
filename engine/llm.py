"""Клиенты нейросетей через REST: Gemini — основной, OpenAI — запасной.

Без SDK: только requests. Так меньше зависимостей, и всё легко
тестируется подменой HTTP-ответов.
"""
from __future__ import annotations

import base64
import binascii
import io
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

# Ответы, по которым видно: дело в модели (нет у посредника, временно отключена).
MODEL_TROUBLE = re.compile(
    r"unknown model|no such model|invalid model|model_not_found|no available channel|"
    r"temporarily unavailable|model[^.;]{0,60}(not found|not exist|does not exist|unavailable|"
    r"not available|not supported|is not supported)", re.I)

NOT_TEXT = ("image", "embed", "tts", "audio", "whisper", "dall", "realtime", "transcri", "moderation",
            "imagen", "veo", "live", "search", "computer", "sora", "speech", "aqa")


def rank_text_models(provider: str, ids: list[str]) -> list[str]:
    """Текстовые модели из списка посредника — в порядке предпочтения."""
    family = "gemini" if provider == "gemini" else "gpt"
    usable = [m for m in dict.fromkeys(ids)
              if family in m.lower() and not any(x in m.lower() for x in NOT_TEXT)]
    # Сначала быстрые и дешёвые (flash, mini) свежих версий, потом облегчённые, потом тяжёлые.
    # Без жёсткого списка названий — чтобы движок не устаревал вместе с моделями.
    return sorted(usable, key=_speed_rank)


_FAST = re.compile(r"(^|[-_/.])(flash|mini)([-_.]|$)", re.I)   # отдельным словом: «gemini» не считается
_LITE = re.compile(r"(^|[-_/.])(lite|nano)([-_.]|$)", re.I)
_VERSION = re.compile(r"(\d+(?:\.\d+)?)")


def _speed_rank(model: str) -> tuple:
    name = model.split("/")[-1]
    m = _VERSION.search(name)
    version = float(m.group(1)) if m else 0.0
    return (not _FAST.search(name), bool(_LITE.search(name)), -version, name)


@dataclass
class ImageResult:
    data: bytes
    mime: str
    provider: str


def _post(url: str, *, retries: int = 3, timeout: int = 120, retry_timeouts: bool = True, **kw) -> dict:
    """POST с повторами при сбоях. retry_timeouts=False — для платной генерации картинок:
    если ответа не дождались, посредник мог уже сгенерировать и списать токены,
    поэтому повторять не стоит."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.post(url, timeout=timeout, **kw)
        except requests.Timeout as e:
            if not retry_timeouts:
                raise AIError(f"посредник не ответил за {timeout} с (повтор не делаю, чтобы не платить дважды)") from None
            last = e
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
        # Рабочие текстовые модели. Если заданная недоступна у посредника, движок сам
        # подберёт другую из его списка и запомнит её до перезапуска.
        self.models = {"gemini": cfg.gemini_text_model, "openai": cfg.openai_text_model}

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

    def _with_model(self, provider: str, call):
        """Вызывает call(model). Если модель недоступна у посредника — пробует
        другие из его списка (не больше 4) и запоминает первую рабочую."""
        current = self.models[provider]
        try:
            return call(current)
        except AIError as e:
            if not MODEL_TROUBLE.search(str(e)):
                raise
            first_error = e
        try:
            candidates = [m for m in rank_text_models(provider, self.list_models(provider)) if m != current]
        except AIError as e:
            raise AIError(f"{first_error} (список моделей посредника получить не удалось: {e})") from None
        for model in candidates[:4]:
            try:
                result = call(model)
            except AIError as e:
                if MODEL_TROUBLE.search(str(e)):
                    continue
                raise
            log.warning("%s: модель %s недоступна, переключился на %s", provider, current, model)
            self.models[provider] = model
            return result
        names = ", ".join(candidates[:6]) or "подходящих нет"
        raise AIError(f"{first_error}; другие модели посредника тоже не ответили ({names})") from None

    def _gemini_json(self, system, prompt, images, temperature):
        call = lambda m: self._gemini_json_model(m, system, prompt, images, temperature)  # noqa: E731
        try:
            return self._with_model("gemini", call)
        except AIError as e:
            # Посредник может отдавать модели в формате Google «недоступными», а в формате
            # OpenAI — рабочими. Если формат ещё не определён, пробуем второй.
            if self.gemini_style is not None or self._is_google or not MODEL_TROUBLE.search(str(e)):
                raise
            self.gemini_style = "openai"
            try:
                return self._with_model("gemini", call)
            except AIError as e2:
                self.gemini_style = None
                raise AIError(f"{e}; в формате OpenAI тоже не вышло: {e2}") from None

    def _gemini_json_model(self, model, system, prompt, images, temperature):
        if self.gemini_style == "openai":
            return self._chat_json(f"{self._gemini_root}/v1/chat/completions", self.cfg.gemini_api_key,
                                   model, system, prompt, images, temperature)
        parts = [{"text": prompt}] + [_image_part(p) for p in images]
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        try:
            data = self._gemini_native(body, model)
        except _SwitchToOpenAI:
            return self._gemini_json_model(model, system, prompt, images, temperature)
        text = "".join(p.get("text", "") for p in _gemini_parts(data))
        return parse_json(text)

    def _openai_json(self, system, prompt, images, temperature):
        return self._with_model("openai", lambda m: self._chat_json(
            self._openai_url("chat/completions"), self.cfg.openai_api_key, m, system, prompt, images, temperature))

    # ------------------------------------------------------------ модели
    def list_models(self, provider: str) -> list[str]:
        """Модели, которые отдаёт посредник (или сам Google/OpenAI)."""
        if provider == "openai":
            urls = [(f"{_root(self.cfg.openai_base_url)}/v1/models",
                     {"Authorization": f"Bearer {self.cfg.openai_api_key}"})]
        else:
            urls = [(f"{self._gemini_root}/v1beta/models", self._gemini_headers()),
                    (f"{self._gemini_root}/v1/models", {"Authorization": f"Bearer {self.cfg.gemini_api_key}"})]
            if self.gemini_style == "openai":
                urls.reverse()
        last = None
        for url, headers in urls:
            try:
                r = requests.get(url, headers=headers, timeout=30)
                data = r.json() if r.status_code == 200 else None
            except (requests.RequestException, ValueError) as e:
                last = e
                continue
            if not isinstance(data, dict):
                last = f"HTTP {r.status_code}"
                continue
            if isinstance(data.get("models"), list):  # формат Google
                return [m["name"].split("/")[-1] for m in data["models"] if m.get("name")
                        and "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]
            if isinstance(data.get("data"), list):    # формат OpenAI
                return [m["id"] for m in data["data"] if isinstance(m, dict) and m.get("id")]
            last = "неизвестный формат списка"
        raise AIError(f"список моделей недоступен: {last}")

    def models_report(self) -> str:
        lines = []
        for name, provider, key in (("Gemini", "gemini", self.cfg.gemini_api_key),
                                    ("OpenAI", "openai", self.cfg.openai_api_key)):
            if not key:
                lines.append(f"{name}: ключ не задан")
                continue
            try:
                ids = self.list_models(provider)
            except AIError as e:
                lines.append(f"{name}: {e}")
                continue
            text = rank_text_models(provider, ids)
            lines.append(f"{name} — сейчас используется: {self.models[provider]}\n"
                         f"Текстовые модели посредника ({len(text)}): " + (", ".join(text[:25]) or "не найдено")
                         + (f"\nВсего моделей: {len(ids)}" if len(ids) != len(text) else ""))
        return "\n\n".join(lines)

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
        """Запрос к Gemini со встроенным поиском Google. Возвращает (JSON, источники).

        Поиск Google есть только в формате Google, поэтому пробуем по очереди:
          1. основной Gemini (если посредник или Google принимает формат Google);
          2. Gemini напрямую у Google по отдельному ключу GOOGLE_API_KEY.
        Если не вышло — AIError с причинами; разведчик тогда ищет сам (scout.py).
        """
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": temperature},
        }
        errors = []
        if self.cfg.gemini_api_key and self.gemini_style != "openai":
            try:
                return _grounded(self._gemini_native(body, self.models["gemini"], timeout=180))
            except _SwitchToOpenAI:
                errors.append("посредник Gemini работает только в формате OpenAI, без поиска Google")
            except AIError as e:
                errors.append(f"основной Gemini: {e}")
        elif self.gemini_style == "openai":
            errors.append("посредник Gemini работает только в формате OpenAI, без поиска Google")
        google_key = getattr(self.cfg, "google_api_key", "")
        if google_key:
            model = getattr(self.cfg, "google_research_model", "") or "gemini-2.5-flash"
            try:
                data = _post(f"{DEFAULT_GEMINI_BASE}/v1beta/models/{model}:generateContent",
                             headers={"x-goog-api-key": google_key}, json=body, timeout=180)
                return _grounded(data)
            except AIError as e:
                errors.append(f"Gemini напрямую по GOOGLE_API_KEY: {e}")
        else:
            errors.append("GOOGLE_API_KEY не задан")
        raise AIError("; ".join(errors))

    def check(self) -> list[str]:
        """Проверка ключей и моделей: по строке на каждого провайдера."""
        out = []
        probe = ('Ответь JSON {"ok": true}', "проверка связи")
        for name, provider, key, fn, base in (
            ("Gemini", "gemini", self.cfg.gemini_api_key, self._gemini_json,
             self.cfg.gemini_base_url or DEFAULT_GEMINI_BASE),
            ("OpenAI", "openai", self.cfg.openai_api_key, self._openai_json, self.cfg.openai_base_url),
        ):
            model = self.models[provider]
            host = base.split("://", 1)[-1].split("/", 1)[0]
            if not key:
                out.append(f"{name}: ключ не задан")
                continue
            try:
                fn(*probe, [], 0.0)
                fmt = ""
                if name == "Gemini" and not self._is_google:
                    fmt = ", формат " + ("Google" if self.gemini_style == "native" else "OpenAI")
                used = self.models[provider]
                auto = f"; заданная {model} недоступна, выбрана автоматически" if used != model else ""
                out.append(f"✅ {name}: отвечает (модель {used}, {host}{fmt}{auto})")
            except (AIError, KeyError, ValueError) as e:
                out.append(f"❌ {name} (модель {model}, {host}): {str(e)[:300]}")
        provider = (getattr(self.cfg, "search_provider", "") or "").lower()
        cse = bool(getattr(self.cfg, "google_api_key", "") and getattr(self.cfg, "google_cse_cx", ""))
        if provider and provider != "none":
            line = f"{provider} (ключ {'задан' if getattr(self.cfg, 'search_api_key', '') else 'НЕ задан'})"
        elif cse:
            line = "поисковик Google (GOOGLE_API_KEY + GOOGLE_CSE_CX)"
        else:
            line = "не настроен — разведка даёт только идеи без поиска; задай SEARCH_PROVIDER и SEARCH_API_KEY"
        out.append(f"Поиск для разведки трендов: {line}. Проверка — /trends")
        return out

    # ---------------------------------------------------------- картинки
    def image_routes(self) -> list[tuple[str, str, str, str, str]]:
        """Возможные пути генерации: (название, режим, адрес, ключ, модель).

        Режимы: edits — OpenAI images/edits (референсы файлами);
                chat — OpenAI chat/completions, картинка приходит в ответе
                       (так многие посредники отдают Gemini Image);
                native — Gemini в формате Google.
        """
        cfg = self.cfg
        routes = []
        img_key = getattr(cfg, "image_api_key", "")
        if img_key:
            base = _root(getattr(cfg, "image_api_base_url", "") or cfg.openai_base_url)
            model = getattr(cfg, "image_model", "") or cfg.openai_image_model
            routes += [("IMAGE_API", "edits", base, img_key, model), ("IMAGE_API", "chat", base, img_key, model)]
        if cfg.gemini_api_key:
            if self.gemini_style == "openai":
                routes.append(("Gemini", "chat", self._gemini_root, cfg.gemini_api_key, cfg.gemini_image_model))
            else:
                routes.append(("Gemini", "native", self._gemini_root, cfg.gemini_api_key, cfg.gemini_image_model))
        if cfg.openai_api_key:
            base = _root(cfg.openai_base_url)
            routes += [("OpenAI", "edits", base, cfg.openai_api_key, cfg.openai_image_model),
                       ("OpenAI", "chat", base, cfg.openai_api_key, cfg.openai_image_model)]
        # сработавший раньше путь — первым и с той моделью, что сработала
        if getattr(self, "image_route", None):
            name, mode, base, model = self.image_route
            routes = [(r[0], r[1], r[2], r[3], model) if r[:3] == (name, mode, base) else r for r in routes]
            routes.sort(key=lambda r: r[:3] != (name, mode, base))
        return routes

    def image(self, prompt: str, refs: list[str | Path]) -> ImageResult:
        """Генерация фото по референсам: перебирает пути, пока один не сработает."""
        errors = []
        refused = 0
        for name, mode, base, key, model in self.image_routes():
            try:
                res = self._image_via(mode, base, key, model, prompt, refs)
            except AIRefused as e:
                refused += 1
                errors.append(f"{name}/{mode}: {e}")
                continue
            except AIError as e:
                errors.append(f"{name}/{mode}: {str(e)[:200]}")
                if MODEL_TROUBLE.search(str(e)) and mode == "chat":
                    alt = self._pick_image_model(base, key, exclude=model)
                    if alt:
                        try:
                            res = self._image_via(mode, base, key, alt, prompt, refs)
                            model = alt
                        except AIError as e2:
                            errors.append(f"{name}/{mode} ({alt}): {str(e2)[:200]}")
                            continue
                    else:
                        continue
                else:
                    continue
            self.image_route = (name, mode, base, model)
            res.provider = f"{name}/{mode}/{model}"
            return res
        if not errors:
            raise AIError("не задан ни один ключ для генерации картинок")
        cls = AIRefused if refused == len(errors) else AIError
        raise cls("; ".join(errors))

    def _image_via(self, mode, base, key, model, prompt, refs) -> ImageResult:
        if mode == "native":
            return self._gemini_image_native(model, prompt, refs)
        if mode == "edits":
            return self._edits_image(base, key, model, prompt, refs)
        return self._chat_image(base, key, model, prompt, refs)

    def _gemini_image_native(self, model, prompt, refs):
        parts = [_image_part(p) for p in refs] + [{"text": prompt}]
        body = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "9:16"}},
        }
        try:
            data = self._gemini_native(body, model, timeout=180)
        except _SwitchToOpenAI:
            return self._chat_image(self._gemini_root, self.cfg.gemini_api_key, model, prompt, refs)
        for p in _gemini_parts(data):
            inline = p.get("inlineData") or p.get("inline_data")
            if inline and inline.get("data"):
                return ImageResult(base64.b64decode(inline["data"]),
                                   inline.get("mimeType") or inline.get("mime_type") or "image/png", "gemini")
        raise AIRefused("Gemini не вернул изображение (вероятно, фильтр безопасности)")

    @staticmethod
    def _edits_image(base, key, model, prompt, refs):
        files = []
        for p in refs[:10]:
            p = Path(p)
            files.append(("image[]", (p.name, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/jpeg")))
        form = {"model": model, "prompt": prompt, "size": "1024x1536", "quality": "high", "input_fidelity": "high"}
        headers = {"Authorization": f"Bearer {key}"}
        url = f"{base}/v1/images/edits"
        try:
            data = _post(url, timeout=240, headers=headers, data=form, files=files, retries=2,
                         retry_timeouts=False)
        except AIError as e:
            # input_fidelity и quality понимают не все модели — повторяем без них.
            if not any(x in str(e) for x in ("input_fidelity", "quality")):
                raise
            form.pop("input_fidelity", None)
            form.pop("quality", None)
            data = _post(url, timeout=240, headers=headers, data=form, files=files, retries=2,
                         retry_timeouts=False)
        img = extract_image(data)
        if not img:
            raise AIRefused("посредник не вернул изображение")
        return ImageResult(img[0], img[1], "edits")

    def _chat_image(self, base, key, model, prompt, refs):
        content: list[dict] = []
        for p in refs[:8]:
            part = _image_part(p)["inlineData"]
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{part['mimeType']};base64,{part['data']}"}})
        content.append({"type": "text", "text": prompt + "\n\nВерни одно изображение, вертикальное 9:16."})
        body = {"model": model, "messages": [{"role": "user", "content": content}]}
        url = f"{base}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        needs = getattr(self, "_needs_modalities", set())
        self._needs_modalities = needs
        if (base, model) in needs:
            body["modalities"] = ["image", "text"]
        data = _post(url, headers=headers, json=body, timeout=240, retries=2, retry_timeouts=False)
        img = extract_image(data)
        if not img and "modalities" not in body and _empty_content(data):
            # Пустой ответ: некоторые посредники отдают картинку, только если явно попросить.
            # Текстовый ответ уже оплачен — его не повторяем.
            data = _post(url, headers=headers, json={**body, "modalities": ["image", "text"]},
                         timeout=240, retries=1, retry_timeouts=False)
            img = extract_image(data)
            if img:
                needs.add((base, model))
        if not img:
            text = ""
            try:
                text = str(data["choices"][0]["message"].get("content"))[:150]
            except (KeyError, IndexError, TypeError, AttributeError):
                pass
            raise AIRefused(f"модель ответила без картинки{': ' + text if text else ''}")
        return ImageResult(img[0], img[1], "chat")

    def _pick_image_model(self, base, key, exclude: str = "") -> str | None:
        """Модель для картинок из списка посредника (gemini-*-image или gpt-image)."""
        try:
            r = requests.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
            ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        except (requests.RequestException, ValueError, AttributeError):
            return None
        return next(iter(rank_image_models(ids, exclude)), None)

    def image_models_report(self) -> str:
        lines = []
        seen = set()
        for name, mode, base, key, model in self.image_routes():
            if (base, key) in seen or mode == "native":
                continue
            seen.add((base, key))
            try:
                r = requests.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
                ids = [m["id"] for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id")]
            except (requests.RequestException, ValueError, AttributeError) as e:
                lines.append(f"{name}: список недоступен ({str(e)[:80]})")
                continue
            imgs = rank_image_models(ids)
            host = base.split("://", 1)[-1]
            lines.append(f"{name} ({host}): модели картинок — " + (", ".join(imgs[:12]) or "не найдено"))
        return "\n".join(lines) or "Ключи для картинок не заданы"


IMAGE_HINT = re.compile(r"image|imagen|dall|flux|seedream|banana", re.I)


def rank_image_models(ids: list[str], exclude: str = "") -> list[str]:
    """Модели, умеющие делать картинки по референсам: Gemini Image и gpt-image — первыми."""
    cands = [m for m in dict.fromkeys(ids) if IMAGE_HINT.search(m) and m != exclude
             and not re.search(r"embed|preview-tts|vision-only", m, re.I)]

    def key(m: str):
        low = m.lower()
        family = 0 if ("gemini" in low and "image" in low) else 1 if "gpt-image" in low else 2
        v = _VERSION.search(m.split("/")[-1])
        return (family, -(float(v.group(1)) if v else 0.0), m)
    return sorted(cands, key=key)


_DATA_URL = re.compile(r"data:(image/[a-zA-Z+.-]+);base64,([A-Za-z0-9+/=\r\n]+)")
_HTTP_IMG = re.compile(r"https?://[^\s)\]\"']+\.(?:png|jpe?g|webp)(?:\?[^\s)\]\"']*)?", re.I)


def _from_url(url: str) -> tuple[bytes, str] | None:
    m = _DATA_URL.match(url or "")
    if m:
        return base64.b64decode(re.sub(r"\s", "", m.group(2))), m.group(1)
    if url and url.startswith("http"):
        try:
            r = requests.get(url, timeout=60)
        except requests.RequestException:
            return None
        if r.status_code == 200 and r.content:
            return r.content, r.headers.get("Content-Type", "image/png").split(";")[0]
    return None


def _empty_content(data) -> bool:
    try:
        content = data["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return True
    return not content or (isinstance(content, str) and not content.strip())


def _is_image(raw: bytes) -> bool:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
        return True
    except Exception:  # noqa: BLE001 — любой сбой разбора значит «не картинка»
        return False


def extract_image(data) -> tuple[bytes, str] | None:
    """Картинка из ответа посредника в любом из распространённых форматов.
    Битые данные и ссылки не на картинку считаются отсутствием картинки."""
    try:
        got = _extract_image_raw(data)
    except (binascii.Error, ValueError, TypeError):
        return None
    if got and not _is_image(got[0]):
        return None
    return got


def _extract_image_raw(data) -> tuple[bytes, str] | None:
    if not isinstance(data, dict):
        return None
    for item in data.get("data") or []:                          # images API
        if isinstance(item, dict):
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"]), "image/png"
            got = _from_url(item.get("url", ""))
            if got:
                return got
    for ch in data.get("choices") or []:                          # chat/completions
        msg = (ch or {}).get("message") or {}
        for im in msg.get("images") or []:                        # формат OpenRouter
            url = (im.get("image_url") or {}).get("url") if isinstance(im, dict) else None
            got = _from_url(url or "")
            if got:
                return got
        content = msg.get("content")
        parts = content if isinstance(content, list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) \
                else part.get("image_url") or part.get("url")
            if isinstance(url, str):
                got = _from_url(url)
                if got:
                    return got
            inline = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline, dict) and inline.get("data"):
                return base64.b64decode(inline["data"]), inline.get("mime_type") or inline.get("mimeType") or "image/png"
            if part.get("b64_json"):
                return base64.b64decode(part["b64_json"]), "image/png"
        text = content if isinstance(content, str) else " ".join(
            p.get("text", "") for p in parts if isinstance(p, dict))
        m = _DATA_URL.search(text or "")
        if m:
            return base64.b64decode(re.sub(r"\s", "", m.group(2))), m.group(1)
        m = _HTTP_IMG.search(text or "")
        if m:
            got = _from_url(m.group(0))
            if got:
                return got
    return None


def _grounded(data: dict) -> tuple[dict | list, list[dict]]:
    """Текст ответа и источники поиска Google из ответа Gemini."""
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
