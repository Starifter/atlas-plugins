"""Shared by the plugin's tests: the plugin loaded from its file, and a fake
`openai` client that records the request and answers as `llama-server` does.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from atlas.sdk.tool_plugin import ToolSpec

HERE = Path(__file__).resolve().parent


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "atlas_plugin_llama_cpp", HERE.parent / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load()
LlamaCppProvider = plugin.LlamaCppProvider
LlamaCppEmbedder = plugin.LlamaCppEmbedder

SPEC = ToolSpec(name="echo", description="Echo.", parameters={"type": "object", "properties": {}})

SERVER_MODEL = {
    "id": "ggml-org/Qwen3-8B-GGUF",
    "object": "model",
    "created": 1735142223,
    "owned_by": "llamacpp",
    "meta": {"n_ctx_train": 40960, "n_params": 8190735360, "size": 4991010816},
}


class FakeClient:
    """Records the request; answers with a whole reply, or a stream of chunks."""

    def __init__(
        self,
        message: Any = None,
        *,
        chunks: list[Any] = (),
        models: list[Any] = (SERVER_MODEL,),
        raises: Exception | None = None,
    ) -> None:
        self.message = message
        self.chunks = list(chunks)
        self.models_data = list(models)
        self.raises = raises
        self.requests: list[dict[str, Any]] = []
        self.listings = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._list)
        self.embeddings = SimpleNamespace(create=self._embed)

    @property
    def request(self) -> dict[str, Any]:
        return self.requests[-1]

    async def _create(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if request.get("stream"):
            return self._stream()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.message)],
            usage={"prompt_tokens": 120, "completion_tokens": 7},
        )

    async def _stream(self) -> Any:
        for chunk in self.chunks:
            yield chunk

    async def _list(self) -> Any:
        self.listings += 1
        for item in self.models_data:
            yield item

    async def _embed(self, **request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3])
                for i, _ in enumerate(request["input"])
            ]
        )


def message(**fields: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": None, **fields}
