"""Optional AI enrichment: synthesise ``reason`` nodes for assistant turns.

When Codex is driven through a third-party provider (for example DeepSeek via
RouterAI) the rollout often contains **no** reasoning items at all.  This module
can ask an OpenAI-compatible endpoint for a one-sentence summary of the
reasoning behind an assistant message, and turn that summary into a ``reason``
node that becomes the parent of the tool calls belonging to that step.

Design constraints (deliberate):

* **Off by default**; it only runs when the user clicks the button / posts to
  ``/api/enrich``.
* Credentials come **only** from the environment.  The key is never written to
  the repository, a config file or a log.
* Results are cached by a hash of the message text, so re-running costs nothing.
* The number of network requests per run is capped.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .graph import Event

# Providers, in the order they are tried when nothing is forced explicitly.
PROVIDERS: Tuple[Tuple[str, str, str], ...] = (
    ("ROUTERAI_API_KEY", "ROUTERAI_BASE_URL", "https://routerai.ru/api/v1"),
    ("AITUNNEL_API_KEY", "AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1"),
)
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

SYSTEM_PROMPT = (
    "You reconstruct the hidden reasoning behind an autonomous coding agent's "
    "step. Given the assistant message and the names of the tools it then "
    "invoked, reply with ONE short sentence (max 220 characters) describing the "
    "intent of this step. Reply with the sentence only: no quotes, no prefix, "
    "no markdown."
)


class EnrichConfig:
    """Resolved enrichment configuration (never stores the key in files)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        provider: str,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> Optional["EnrichConfig"]:
        env = environ if environ is not None else os.environ
        model = env.get("GRAFF_LLM_MODEL") or DEFAULT_MODEL
        for key_var, url_var, default_url in PROVIDERS:
            api_key = env.get(key_var)
            if api_key:
                base_url = env.get(url_var) or default_url
                return cls(api_key=api_key, base_url=base_url, model=model, provider=key_var)
        return None


def message_fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()[:32]


class EnrichCache:
    """Tiny JSON-backed cache: fingerprint -> summary."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    self._data = loaded
            except Exception:
                self._data = {}

    def get(self, fingerprint: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(fingerprint)
        if isinstance(entry, dict):
            value = entry.get("summary")
            return value if isinstance(value, str) else None
        return None

    def put(self, fingerprint: str, summary: str, model: str) -> None:
        with self._lock:
            self._data[fingerprint] = {"summary": summary, "model": model, "ts": time.time()}
            if self.path:
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
                    tmp = self.path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as handle:
                        json.dump(self._data, handle, ensure_ascii=False)
                    os.replace(tmp, self.path)
                except Exception:
                    pass


class EnrichmentError(Exception):
    pass


class Enricher:
    """Turns assistant messages into ``reason`` nodes using an LLM."""

    def __init__(
        self,
        config: EnrichConfig,
        cache: Optional[EnrichCache] = None,
        timeout: float = 45.0,
    ) -> None:
        self.config = config
        self.cache = cache or EnrichCache(None)
        self.timeout = timeout

    # -- HTTP -------------------------------------------------------------- #

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.config.base_url + path
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.api_key,
                "Accept": "application/json",
                # Some gateways sit behind Cloudflare and reject the default UA.
                "User-Agent": "GRAFF/0.2 (+https://localhost)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            raise EnrichmentError("HTTP %s from %s: %s" % (exc.code, url, detail))
        except urllib.error.URLError as exc:
            raise EnrichmentError("cannot reach %s: %s" % (url, exc.reason))
        except Exception as exc:  # pragma: no cover - defensive
            raise EnrichmentError("request failed: %s" % exc)
        try:
            return json.loads(raw)
        except Exception:
            raise EnrichmentError("provider returned non-JSON response")

    def _summarise(self, message_text: str, tool_names: Sequence[str]) -> str:
        tools = ", ".join(sorted(set(tool_names))) or "(none)"
        user = "Assistant message:\n%s\n\nTools invoked next: %s" % (message_text[:4000], tools)

        # Preferred: OpenAI Chat Completions (widely supported by gateways).
        chat = self._post("/chat/completions", {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 120,
            "stream": False,
        })
        text = _extract_chat_text(chat)
        if text:
            return text

        # Fallback: the Responses API.
        responses = self._post("/responses", {
            "model": self.config.model,
            "instructions": SYSTEM_PROMPT,
            "input": user,
            "max_output_tokens": 120,
        })
        text = _extract_responses_text(responses)
        if text:
            return text
        raise EnrichmentError("provider returned an empty summary")

    # -- planning ---------------------------------------------------------- #

    @staticmethod
    def build_plan(events: Sequence[Event]) -> List[Dict[str, Any]]:
        """Group events into (assistant message -> following tool steps)."""

        plan: List[Dict[str, Any]] = []
        target: Optional[Event] = None
        children: List[Event] = []
        for event in events:
            if event.type == "reason":
                # Existing reasoning ends the current step.
                if target is not None and children:
                    plan.append({"message": target, "children": children})
                target, children = None, []
                continue
            if event.type == "message" and event.actor != "user":
                if target is not None and children:
                    plan.append({"message": target, "children": children})
                target, children = event, []
                continue
            if target is not None and event.type in ("tool_call", "tool_result", "file_edit", "error"):
                children.append(event)
        if target is not None and children:
            plan.append({"message": target, "children": children})
        return plan

    # -- execution --------------------------------------------------------- #

    def enrich(
        self,
        events: Sequence[Event],
        limit: int = 40,
        progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Return an enrichment plan, calling the LLM for uncached messages."""

        plan = self.build_plan(events)
        results: List[Dict[str, Any]] = []
        requests_made = 0
        cache_hits = 0
        errors: List[str] = []
        skipped = 0

        for step in plan:
            message: Event = step["message"]
            children: List[Event] = step["children"]
            text = (message.text or "").strip()
            if not text:
                skipped += 1
                continue

            fingerprint = message_fingerprint(text)
            summary = self.cache.get(fingerprint)
            if summary:
                cache_hits += 1
            elif requests_made >= limit:
                skipped += 1
                continue
            else:
                names = [str(child.extra.get("name") or child.type) for child in children]
                try:
                    summary = self._summarise(text, names)
                except EnrichmentError as exc:
                    errors.append(str(exc))
                    if len(errors) >= 3:
                        break
                    continue
                requests_made += 1
                self.cache.put(fingerprint, summary, self.config.model)

            results.append(
                {
                    "reason": {
                        # One reason node per assistant step (deterministic and
                        # unique); the LLM *cache* is still keyed by the text
                        # fingerprint, so identical texts never cost two requests.
                        "id": "enr:%s:%s" % (fingerprint, message.id),
                        "ts": float(message.ts or 0.0) + 0.001,
                        "text": summary,
                    },
                    "parent_id": message.id,
                    "children": [child.id for child in children],
                }
            )
            if progress is not None:
                progress({"done": len(results), "requests": requests_made, "total": len(plan)})

        return {
            "steps": len(plan),
            "enriched": len(results),
            "requests": requests_made,
            "cache_hits": cache_hits,
            "skipped": skipped,
            "errors": errors[:3],
            "results": results,
            "model": self.config.model,
            "provider": self.config.provider,
        }


def _extract_chat_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return part["text"].strip()
        text = choices[0].get("text") if isinstance(choices[0], dict) else None
        if isinstance(text, str) and text.strip():
            return text.strip()
    output = data.get("output_text")
    if isinstance(output, str) and output.strip():
        return output.strip()
    return ""


def _extract_responses_text(data: Dict[str, Any]) -> str:
    output = data.get("output")
    if isinstance(output, list):
        chunks: List[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
        if chunks:
            return " ".join(chunks).strip()
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return _extract_chat_text(data)
