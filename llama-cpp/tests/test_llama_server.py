"""The embedder's width, and the `llama-server` the plugin runs itself.

The managed server is driven with a stand-in: `sys.executable` running a small
HTTP script that answers 503 on `/health` for a while and 200 after, or exits
with a code - what `llama-server` does while loading, once loaded, and when
the model file is missing.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from llama_cpp_testing import FakeClient, LlamaCppEmbedder, plugin

from atlas.sdk.plugin_entry import PluginContext
from atlas.sdk.runtime import ProviderError

# -- the embedder's width ---------------------------------------------------------------


async def test_the_embedder_reads_its_width_from_the_server() -> None:
    asked: list[str] = []

    def probe(root: str) -> int:
        asked.append(root)
        return 3

    client = FakeClient()
    embedder = LlamaCppEmbedder("emb", client=client, probe=probe)
    assert embedder.model == "emb"
    assert embedder.dimensions == 3
    assert embedder.dimensions == 3
    assert asked == ["http://127.0.0.1:8080"], "asked once, then remembered"
    assert await embedder.embed([]) == []
    vectors = await embedder.embed(["a", "  "])
    assert client.request == {"model": "emb", "input": ["a", " "]}
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


async def test_a_declared_width_wins_and_a_wrong_one_is_named() -> None:
    declared = LlamaCppEmbedder("emb", dimensions=4, client=FakeClient(), probe=lambda _: 3)
    assert declared.dimensions == 4
    with pytest.raises(ProviderError, match="set it to 3"):
        await declared.embed(["a"])


async def test_an_unknown_width_is_learned_from_the_first_vector() -> None:
    embedder = LlamaCppEmbedder("emb", client=FakeClient(), probe=lambda _: 0)
    assert embedder.dimensions == 0, "nothing answered, nothing guessed"
    await embedder.embed(["a"])
    assert embedder.dimensions == 3


# -- the managed server -----------------------------------------------------------------


def test_the_argv_is_built_from_the_settings(tmp_path: Path) -> None:
    spec = plugin.ServerSpec(role="chat", binary="llama-server", model="org/repo:Q4", port=8080)
    assert spec.argv() == [
        "llama-server",
        "-hf",
        "org/repo:Q4",
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
    ]
    assert spec.base_url == "http://127.0.0.1:8080/v1"
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    spec = plugin.ServerSpec(
        role="chat",
        binary="/opt/llama-server",
        model=str(gguf),
        port=9000,
        context=8192,
        gpu_layers=99,
        mmproj="/m/proj.gguf",
        args=("--flash-attn", "on"),
    )
    argv = spec.argv()
    assert argv[:3] == ["/opt/llama-server", "-m", str(gguf)]
    assert argv[3:] == [
        "--host",
        "127.0.0.1",
        "--port",
        "9000",
        "-c",
        "8192",
        "-ngl",
        "99",
        "--mmproj",
        "/m/proj.gguf",
        "--flash-attn",
        "on",
    ]
    prefixed = plugin.ServerSpec(role="chat", binary="b", model="hf:org/repo", port=1)
    assert prefixed.argv()[1:3] == ["-hf", "org/repo"]
    embedding = plugin.ServerSpec(role="embedding", binary="b", model="e.gguf", port=8081)
    assert embedding.argv()[-3:] == ["--embedding", "--pooling", "mean"]


FAKE_SERVER = """
import http.server, json, sys, time
delay, port, width = float(sys.argv[-3]), int(sys.argv[-2]), int(sys.argv[-1])
if delay < 0:
    print("no model file", file=sys.stderr); sys.exit(3)
started = time.monotonic()
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        loading = time.monotonic() - started < delay
        if self.path == "/health":
            if loading:
                body, status = b'{"error":{"code":503,"message":"Loading model"}}', 503
            else:
                body, status = b'{"status":"ok"}', 200
        elif self.path.startswith("/v1/models"):
            body = json.dumps({"data": [{"id": "fake.gguf", "meta": {"n_embd": width}}]}).encode()
            status = 200
        else:
            body, status = b"{}", 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def fake_server(tmp_path: Path) -> Any:
    """A `llama-server` stand-in, made per test and stopped after it."""
    script = tmp_path / "fake_llama_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    made: list[Any] = []

    def make(
        *, role: str = "chat", delay: float = 0.0, width: int = 3, timeout: float = 20.0
    ) -> Any:
        port = _free_port()
        spec = plugin.ServerSpec(
            role=role,
            # `python -m fake_llama_server --host ... --port ... <delay> <port> <width>`:
            # the model "name" is the script's module, found through PYTHONPATH,
            # and the script reads its own three arguments from the end.
            binary=sys.executable,
            model="fake_llama_server",
            port=port,
            args=(str(delay), str(port), str(width)),
            startup_timeout=timeout,
            log_dir=tmp_path / "logs",
        )
        server = plugin.ManagedServer(spec, environment={**os.environ, "PYTHONPATH": str(tmp_path)})
        made.append(server)
        return server

    yield make
    for server in made:
        server.stop()


async def test_a_managed_server_is_started_on_first_need_and_waited_for(fake_server: Any) -> None:
    server = fake_server(delay=1.0)
    assert not server.running
    await server.ensure()
    assert server.running
    assert plugin._probe_health(server.root) == 200
    assert server.log_path is not None and server.log_path.is_file()
    process = server.process
    await server.ensure()
    assert server.process is process, "already up: not started again"
    server.stop()
    assert not server.running
    assert plugin._probe_health(server.root) is None


async def test_a_server_that_dies_is_reported_with_its_log(fake_server: Any) -> None:
    server = fake_server(delay=-1)
    with pytest.raises(ProviderError, match="exited with code 3") as caught:
        await server.ensure()
    assert "no model file" in str(caught.value)


async def test_a_server_that_never_comes_up_is_a_timeout(fake_server: Any) -> None:
    server = fake_server(delay=60.0, timeout=1.5)
    with pytest.raises(ProviderError, match="did not become ready"):
        await server.ensure()


async def test_a_server_already_on_the_port_is_adopted_not_replaced(fake_server: Any) -> None:
    first = fake_server()
    await first.ensure()
    second = plugin.ManagedServer(first.spec, environment={})
    await second.ensure()
    assert second.adopted and second.process is None
    second.stop()
    assert first.running, "not ours to stop"


def test_a_missing_binary_is_named_when_needed() -> None:
    spec = plugin.ServerSpec(role="chat", binary="", model="m.gguf", port=_free_port())
    with pytest.raises(ProviderError, match="not found on PATH"):
        plugin.ManagedServer(spec, environment={}).start()


async def test_the_embedder_starts_its_server_from_the_width_ask(fake_server: Any) -> None:
    server = fake_server(width=5)
    embedder = LlamaCppEmbedder(server=server, client=FakeClient())
    assert embedder.dimensions == 0, "nothing was up: started, not waited for"
    assert server.running
    await server.ensure()
    assert embedder.dimensions == 5, "up now, and the server said its width"


def test_register_with_a_server_model_owns_the_process(tmp_path: Path) -> None:
    ctx = PluginContext(
        plugin="llama-cpp",
        workspace=tmp_path,
        settings={
            "server_model": "ggml-org/Qwen3-8B-GGUF",
            "server_port": 8090,
            "server_context": 16384,
            "server_binary": "/opt/llama-server",
            "embedding_model": "ggml-org/embeddinggemma-300m-qat-q8_0-GGUF",
        },
        providers=True,
    )
    plugin.LlamaCppPlugin().register(ctx)
    from atlas.providers import provider_class
    from atlas.providers.embedding import known_embedders

    cls = provider_class("llama-cpp")
    assert cls is not None and cls.server is not None
    assert cls.base_url == "http://127.0.0.1:8090/v1"
    assert cls.server.spec.argv()[:3] == ["/opt/llama-server", "-hf", "ggml-org/Qwen3-8B-GGUF"]
    assert "-c" in cls.server.spec.argv()
    assert cls.server.spec.log_dir == tmp_path / ".atlas" / "llama-cpp"
    embedder = known_embedders()["llama-cpp"](client=FakeClient(), probe=lambda _: 0)
    assert embedder._server is not None
    assert embedder._server.spec.port == 8081
    assert embedder._server.spec.argv()[-3:] == ["--embedding", "--pooling", "mean"]
    assert not embedder._server.running and not cls.server.running, "nothing spawned at register"
