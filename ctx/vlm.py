"""Native Vision-Language LLM (VLM) engine for Auto-Describe & cataloging.

Provides direct, in-process vision-language analysis of large-format scans
without requiring external OCR daemons or CLI installations.

Supported backends:
1. Local Ollama (auto-detects local running instances and vision models such as
   qwen2.5vl, qwen3-vl, glm-ocr, llama3.2-vision, minicpm-v, etc.)
2. Any OpenAI-compatible multimodal endpoint (vLLM, LM Studio, OpenRouter,
   OpenAI GPT-4o / GPT-4o-mini, LocalAI, etc.)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434/v1"
DEFAULT_OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
DEFAULT_TIMEOUT_SEC = 60.0
MAX_IMAGE_DIMENSION_PX = 2048

# Known preferred vision model name substrings in descending priority.
# qwen2.5vl produces fast direct JSON without reasoning tokens overhead.
PREFERRED_VISION_MODELS = [
    "qwen2.5vl:3b",
    "qwen2.5vl",
    "qwen3-vl:2b",
    "qwen3-vl:8b",
    "qwen3-vl",
    "glm-ocr",
    "paddleocr-vl",
    "llama3.2-vision",
    "minicpm-v",
    "llava",
]

DEFAULT_DESCRIBE_PROMPT = """You are an expert archivist and document cataloging specialist.
Analyze this scanned image (which may be an architectural drawing, blueprint, map, fine art, photograph, patent, poster, technical schematic, or text document).

Extract and generate the following catalog metadata:
1. doc_type: Specific document type (e.g. "Architectural-Drawing", "Blueprint", "Floor-Plan", "Sectional-Map", "Fine-Art", "Photograph", "Patent", "Technical-Schematic", "Document").
2. title: Formal title or primary heading found in the document or title block. If untitled, provide a clear concise subject title.
3. description: A clear, professional 1-2 sentence description of the content, visual style, and layout.
4. visible_text: Key prominent text extracted from title blocks, headings, revision notes, legends, or stamps.
5. slug: A concise 3-5 word hyphenated slug suitable for a clean filename (use letters, numbers, and hyphens only; no dates, no punctuation, no file extensions; e.g. "Ground-Floor-Plan", "Manhattan-Zoning-Map", "Water-Lilies-Study").
6. tags: 3-6 relevant search tags/keywords.

Respond ONLY with a valid JSON object matching this schema:
{
  "doc_type": "...",
  "title": "...",
  "description": "...",
  "visible_text": "...",
  "slug": "...",
  "tags": ["..."]
}
"""


def clean_slug(text: str) -> str:
    """Normalize a title or slug into a clean filename-safe segment."""
    s = str(text or "").strip()
    # Strip any markdown or file extensions
    s = re.sub(r"\.(pdf|tiff|tif|jpg|jpeg|png)$", "", s, flags=re.IGNORECASE)
    # Remove leading date stamps like YYYY-MM-DD or YYYYMMDD
    s = re.sub(r"^\d{4}[-_]?\d{2}[-_]?\d{2}[-_]?", "", s)
    s = re.sub(r"^\d{8,14}[-_]?", "", s)
    # Replace non-alphanumeric with hyphens
    s = re.sub(r"[^\w\-]+", "-", s)
    s = s.replace("_", "-")
    s = re.sub(r"-{2,}", "-", s).strip("-")
    # Take at most 6 words
    parts = s.split("-")
    if len(parts) > 6:
        s = "-".join(parts[:6])
    return s or "Scan-Document"


def prepare_image_for_vlm(
    source: Union[str, Path, Image.Image],
    max_dimension: int = MAX_IMAGE_DIMENSION_PX,
) -> Tuple[str, Tuple[int, int]]:
    """Load, downscale if needed, and encode image to base64 JPEG data URI.

    Returns:
        (data_uri, (original_width, original_height))
    """
    if isinstance(source, Image.Image):
        img = source
    else:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"Image file not found: {source}")
        img = Image.open(path)

    orig_size = img.size
    # Convert RGBA/Palette/16-bit to 8-bit RGB
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Downscale while preserving aspect ratio if larger than max_dimension
    w, h = img.size
    if max(w, h) > max_dimension:
        scale = max_dimension / float(max(w, h))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", orig_size


@dataclass
class VlmConfig:
    enabled: bool = True
    provider: str = "auto"  # "auto" | "ollama" | "openai" | "disabled"
    endpoint: str = DEFAULT_OLLAMA_ENDPOINT
    model: str = "auto"
    api_key: str = ""
    timeout_sec: float = DEFAULT_TIMEOUT_SEC


class VlmEngine:
    """Client for calling Vision-Language Models for Auto-Describe."""

    def __init__(self, config: Optional[VlmConfig] = None) -> None:
        self.config = config or VlmConfig()
        self._cached_model: Optional[str] = None
        self._last_probe_time: float = 0.0
        self._cached_status: Optional[Dict[str, Any]] = None

    def update_config(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
        self._cached_model = None
        self._cached_status = None

    def discover_ollama_models(self) -> List[str]:
        """Query local Ollama for available models with vision capability."""
        endpoint = self.config.endpoint.rstrip("/")
        # If endpoint ends with /v1, base is one level up
        base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        tags_url = f"{base}/api/tags"

        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(tags_url)
                if resp.status_code != 200:
                    return []
                data = resp.json()
                models = data.get("models") or []
                vision_models: List[str] = []
                for m in models:
                    name = m.get("name") or m.get("model") or ""
                    caps = m.get("capabilities") or []
                    # Check explicit capability or name heuristic
                    if "vision" in caps:
                        vision_models.append(name)
                    elif any(tag in name.lower() for tag in ("vl", "vision", "glm-ocr", "ocr")):
                        vision_models.append(name)
                return vision_models
        except Exception as exc:
            logger.debug("Ollama model discovery failed: %s", exc)
            return []

    def resolve_model(self) -> Optional[str]:
        """Determine which model to invoke."""
        cfg_model = (self.config.model or "").strip()
        if cfg_model and cfg_model != "auto":
            return cfg_model

        if self._cached_model:
            return self._cached_model

        # Auto-discover from Ollama
        discovered = self.discover_ollama_models()
        if discovered:
            # Pick by preference
            for pref in PREFERRED_VISION_MODELS:
                for d in discovered:
                    if pref in d.lower():
                        self._cached_model = d
                        return d
            self._cached_model = discovered[0]
            return discovered[0]

        # Default fallback
        return "qwen2.5vl:3b"

    def status(self, refresh: bool = False) -> Dict[str, Any]:
        """Probe the VLM endpoint and return availability and model info."""
        now = time.monotonic()
        if not refresh and self._cached_status and (now - self._last_probe_time < 15.0):
            return self._cached_status

        if not self.config.enabled or self.config.provider == "disabled":
            res = {
                "available": False,
                "reason": "VLM is disabled in settings",
                "provider": "disabled",
                "endpoint": self.config.endpoint,
            }
            self._cached_status = res
            return res

        model = self.resolve_model()
        endpoint = self.config.endpoint.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            chat_url = f"{endpoint}/chat/completions"
        else:
            chat_url = endpoint

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        available = False
        reason = ""
        discovered = self.discover_ollama_models()
        if discovered:
            available = True
            reason = f"Ollama ready with model '{model}' ({len(discovered)} vision models detected)"
        else:
            try:
                base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
                models_url = f"{endpoint}/models" if endpoint.endswith("/v1") else f"{base}/v1/models"
                with httpx.Client(timeout=3.0) as client:
                    resp = client.get(models_url, headers=headers)
                    if resp.status_code == 200:
                        available = True
                        reason = "OpenAI-compatible VLM endpoint reachable"
                    else:
                        reason = f"Endpoint returned HTTP {resp.status_code}"
            except Exception as exc:
                reason = f"Cannot reach VLM endpoint ({exc})"

        res = {
            "available": available,
            "provider": "ollama" if ("11434" in endpoint) else "openai_compatible",
            "endpoint": self.config.endpoint,
            "model": model,
            "discovered_models": discovered,
            "reason": reason,
        }
        self._last_probe_time = now
        self._cached_status = res
        return res

    def describe(
        self,
        image_source: Union[str, Path, Image.Image],
        prompt: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Send an image to the VLM and return structured document metadata."""
        status = self.status()
        if not status.get("available"):
            raise RuntimeError(f"VLM is unavailable: {status.get('reason', 'endpoint unreachable')}")

        model = self.resolve_model()
        if not model:
            raise RuntimeError("No vision model found or configured")

        data_uri, orig_size = prepare_image_for_vlm(image_source)

        sys_prompt = (prompt or "").strip() or DEFAULT_DESCRIBE_PROMPT

        endpoint = self.config.endpoint.rstrip("/")
        chat_url = endpoint if endpoint.endswith("/chat/completions") else f"{endpoint}/chat/completions"

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": sys_prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 1536,
        }

        call_timeout = float(timeout or self.config.timeout_sec)
        start_t = time.monotonic()

        with httpx.Client(timeout=call_timeout) as client:
            resp = client.post(chat_url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"VLM API returned error {resp.status_code}: {resp.text[:300]}"
                )
            res_data = resp.json()

        latency_ms = int((time.monotonic() - start_t) * 1000)
        choices = res_data.get("choices") or []
        if not choices:
            raise RuntimeError("VLM returned no choices")

        msg = choices[0].get("message") or {}
        content = (msg.get("content") or "").strip()
        if not content and msg.get("reasoning"):
            content = msg.get("reasoning", "").strip()

        # Parse JSON output
        parsed = self._extract_json(content)

        doc_type = clean_slug(parsed.get("doc_type") or "Document")
        title = str(parsed.get("title") or "Untitled Scan").strip()
        description = str(parsed.get("description") or "").strip()
        visible_text = str(parsed.get("visible_text") or "").strip()
        slug_raw = parsed.get("slug") or title or description
        slug = clean_slug(slug_raw)

        tags = parsed.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        tags = [str(t).strip() for t in tags if str(t).strip()]

        stem = f"{doc_type}_{slug}" if (doc_type and doc_type != slug) else slug

        return {
            "ok": True,
            "doc_type": doc_type,
            "title": title,
            "description": description,
            "visible_text": visible_text,
            "slug": slug,
            "stem": stem,
            "tags": tags,
            "model": model,
            "provider": status.get("provider", "vlm"),
            "latency_ms": latency_ms,
            "raw_response": content,
            "image_size": list(orig_size),
        }

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        """Extract JSON object from response text, tolerating markdown fences."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            val = json.loads(text)
            if isinstance(val, dict):
                return val
        except ValueError:
            pass

        # Try regex search for first { ... }
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            try:
                val = json.loads(match.group(1))
                if isinstance(val, dict):
                    return val
            except ValueError:
                pass

        # Fallback dictionary if JSON parsing completely fails
        return {
            "doc_type": "Document",
            "title": text[:60].strip(),
            "description": text[:200].strip(),
            "slug": clean_slug(text[:40]),
            "tags": ["scan"],
        }
