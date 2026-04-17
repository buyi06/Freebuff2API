#!/usr/bin/env python3
"""Freebuff OpenAI API 反代代理 (Python 版)"""

import argparse
import asyncio
import codecs
import hmac
import json
import logging
import os
import platform
import random
import signal
import stat
import string
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("请先安装 aiohttp: pip install aiohttp")
    sys.exit(1)

API_BASE = "www.codebuff.com"
DEFAULT_PORT = 1145
DEFAULT_HOST = "127.0.0.1"
POLL_INTERVAL_S = 5
LOGIN_TIMEOUT_S = 300

# 环境变量最高优先级；否则读取 proxy_api_key 文件
PROXY_API_KEY = os.environ.get("FREEBUFF_PROXY_API_KEY", "").strip() or None

MODEL_TO_AGENT = {
    "minimax/minimax-m2.7": "base2-free",
    "z-ai/glm-5.1": "base2-free",
    "google/gemini-2.5-flash-lite": "file-picker",
    "google/gemini-3.1-flash-lite-preview": "file-picker-max",
    "google/gemini-3.1-pro-preview": "thinker-with-files-gemini",
}

# 常见 OpenAI 模型名到内部模型的别名，方便直接替换 base_url 的客户端
MODEL_ALIASES = {
    "gpt-4o-mini": "minimax/minimax-m2.7",
    "gpt-4o": "google/gemini-3.1-pro-preview",
    "gpt-4": "google/gemini-3.1-pro-preview",
    "gpt-3.5-turbo": "minimax/minimax-m2.7",
}

DEFAULT_MODEL = "minimax/minimax-m2.7"

# 全局状态
token: str | None = None
cached_run_id: str | None = None
cached_agent_id: str | None = None
run_lock: asyncio.Lock | None = None

log = logging.getLogger("freebuff")


def setup_logging(level: str = "INFO", log_file: str | None = None):
    fmt = "%(asctime)s %(levelname)s %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level.upper(), format=fmt, handlers=handlers, force=True)


# ============ 工具 ============

def generate_fingerprint_id() -> str:
    chars = string.ascii_lowercase + string.digits
    return f"codebuff-cli-{''.join(random.choices(chars, k=26))}"


def get_config_paths() -> tuple[Path, Path]:
    home = Path.home()
    if platform.system() == "Windows":
        config_dir = Path(os.environ.get("APPDATA", str(home))) / "manicode"
    else:
        config_dir = home / ".config" / "manicode"
    return config_dir, config_dir / "credentials.json"


def secure_write(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    if platform.system() != "Windows":
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass


def resolve_model(name: str) -> str:
    if name in MODEL_TO_AGENT:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    return DEFAULT_MODEL


def load_token() -> str | None:
    _, creds_path = get_config_paths()
    if creds_path.exists():
        try:
            creds = json.loads(creds_path.read_text(encoding="utf-8"))
            return creds.get("default", {}).get("authToken")
        except Exception:
            pass
    return None


def load_proxy_api_key() -> str | None:
    global PROXY_API_KEY
    if PROXY_API_KEY:
        return PROXY_API_KEY
    config_dir, _ = get_config_paths()
    key_file = config_dir / "proxy_api_key"
    if key_file.exists():
        try:
            k = key_file.read_text(encoding="utf-8").strip()
            if k:
                PROXY_API_KEY = k
                return k
        except Exception:
            pass
    return None


def save_proxy_api_key(key: str):
    config_dir, _ = get_config_paths()
    secure_write(config_dir / "proxy_api_key", key.strip())


def check_api_key(request) -> bool:
    if not PROXY_API_KEY:
        return True
    candidate = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        candidate = auth[7:].strip()
    if not candidate:
        candidate = request.headers.get("x-api-key", "").strip() \
            or request.headers.get("api-key", "").strip()
    if not candidate:
        return False
    return hmac.compare_digest(candidate, PROXY_API_KEY)


def require_auth(handler):
    async def wrapper(request):
        if not check_api_key(request):
            return web.json_response({"error": {"message": "Unauthorized"}}, status=401)
        return await handler(request)
    return wrapper


# ============ HTTP 请求 ============

async def api_request(session, hostname, path, body=None, auth_token=None, method="POST"):
    url = f"https://{hostname}{path}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "freebuff-proxy/1.0",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=30)}
    if body is not None and method == "POST":
        kwargs["json"] = body

    async with session.request(method, url, **kwargs) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = await resp.text()
        return {"status": resp.status, "data": data}


# ============ 登录流程 ============

async def do_login(session) -> str:
    log.info("需要登录 Freebuff...")
    fp_id = generate_fingerprint_id()
    log.info("指纹: %s...", fp_id[:30])

    res = await api_request(session, "freebuff.com", "/api/auth/cli/code", {"fingerprintId": fp_id})
    if res["status"] != 200 or "loginUrl" not in res["data"]:
        raise RuntimeError(f"获取登录 URL 失败: {res['data']}")

    d = res["data"]
    login_url, fp_hash, expires = d["loginUrl"], d["fingerprintHash"], d["expiresAt"]

    print(f"\n请在浏览器中打开:\n{login_url}\n")
    try:
        webbrowser.open(login_url)
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, input, "完成登录后按回车继续...")
    log.info("等待登录完成...")

    start = time.time()
    while time.time() - start < LOGIN_TIMEOUT_S:
        try:
            path = (
                f"/api/auth/cli/status?fingerprintId={quote(str(fp_id))}"
                f"&fingerprintHash={quote(str(fp_hash))}&expiresAt={quote(str(expires))}"
            )
            sr = await api_request(session, "freebuff.com", path, method="GET")
            if sr["status"] == 200 and "user" in sr["data"]:
                user = sr["data"]["user"]
                _, creds_path = get_config_paths()
                creds = {
                    "default": {
                        "id": user["id"], "name": user["name"], "email": user["email"],
                        "authToken": user.get("authToken") or user.get("auth_token"),
                        "credits": user.get("credits", 0),
                    }
                }
                secure_write(creds_path, json.dumps(creds, indent=2))
                log.info("登录成功: %s (%s)", user["name"], user["email"])
                return creds["default"]["authToken"]
        except Exception as e:
            log.warning("轮询出错: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)

    raise RuntimeError("登录超时")


# ============ Freebuff API ============

async def create_agent_run(session, auth_token, agent_id) -> str:
    t = time.time()
    res = await api_request(session, API_BASE, "/api/v1/agent-runs",
                            {"action": "START", "agentId": agent_id}, auth_token)
    ms = int((time.time() - t) * 1000)
    if res["status"] != 200 or "runId" not in res["data"]:
        raise RuntimeError(f"创建 Agent Run 失败: {json.dumps(res['data'], ensure_ascii=False)}")
    log.info("创建 Agent Run: %s (%dms)", res["data"]["runId"], ms)
    return res["data"]["runId"]


async def get_or_create_agent_run(session, auth_token, agent_id) -> str:
    global cached_run_id, cached_agent_id
    async with run_lock:
        if cached_agent_id != agent_id:
            cached_run_id = None
            cached_agent_id = agent_id
        if cached_run_id:
            return cached_run_id
        cached_run_id = await create_agent_run(session, auth_token, agent_id)
        return cached_run_id


async def reset_and_create_run(session, auth_token, agent_id) -> str:
    global cached_run_id, cached_agent_id
    async with run_lock:
        cached_run_id = None
        cached_agent_id = agent_id
        cached_run_id = await create_agent_run(session, auth_token, agent_id)
        return cached_run_id


async def finish_agent_run(session, auth_token, run_id):
    await api_request(session, API_BASE, "/api/v1/agent-runs", {
        "action": "FINISH", "runId": run_id, "status": "completed",
        "totalSteps": 1, "directCredits": 0, "totalCredits": 0,
    }, auth_token)


def make_freebuff_body(openai_body, run_id):
    body = dict(openai_body)
    body["codebuff_metadata"] = {
        "run_id": run_id,
        "client_id": f"freebuff-proxy-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}",
        "cost_mode": "free",
    }
    return body


def sanitize_tool_calls(tool_calls):
    """剥除上游返回里非标的顶层 name 字段，保留 OpenAI 规范的 id/type/function/index。"""
    if not isinstance(tool_calls, list):
        return tool_calls
    allowed = {"id", "type", "function", "index"}
    return [
        {k: v for k, v in tc.items() if k in allowed} if isinstance(tc, dict) else tc
        for tc in tool_calls
    ]


def build_openai_response(run_id, model, choice_data, usage_data=None):
    choice = choice_data or {}
    message = choice.get("message", {})
    has_tool_calls = bool(message.get("tool_calls"))
    finish_reason = choice.get("finish_reason") or ("tool_calls" if has_tool_calls else "stop")
    resp = {
        "id": f"freebuff-{run_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": message.get("content") if not has_tool_calls else message.get("content"),
                **({"reasoning_content": message["reasoning_content"]}
                   if message.get("reasoning_content") else {}),
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": (usage_data or {}).get("prompt_tokens", 0),
            "completion_tokens": (usage_data or {}).get("completion_tokens", 0),
            "total_tokens": (usage_data or {}).get("total_tokens", 0),
        },
    }
    if has_tool_calls:
        resp["choices"][0]["message"]["tool_calls"] = sanitize_tool_calls(message["tool_calls"])
        if resp["choices"][0]["message"]["content"] is None:
            pass  # OpenAI 规范允许 null
    else:
        if resp["choices"][0]["message"]["content"] is None:
            resp["choices"][0]["message"]["content"] = ""
    return resp


# ============ 流式转发 ============

async def stream_to_openai_format(session, freebuff_body, auth_token, response, model, include_usage=False):
    url = f"https://{API_BASE}/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}",
        "Accept": "text/event-stream",
        "User-Agent": "freebuff-proxy/1.0",
    }
    response_id = f"freebuff-{int(time.time() * 1000)}"
    finish_reason = "stop"
    last_usage = None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    timeout = aiohttp.ClientTimeout(total=120)
    async with session.post(url, json=freebuff_body, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"上游 HTTP {resp.status}: {err[:500]}")

        buffer = ""
        async for chunk in resp.content.iter_any():
            buffer += decoder.decode(chunk)
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                trimmed = line.strip()
                if not trimmed or not trimmed.startswith("data: "):
                    continue
                json_str = trimmed[6:].strip()
                if json_str == "[DONE]":
                    continue
                try:
                    parsed = json.loads(json_str)
                except json.JSONDecodeError as e:
                    log.warning("SSE JSON 解析失败: %s: %s", e, json_str[:120])
                    continue
                try:
                    if parsed.get("usage"):
                        last_usage = parsed["usage"]
                    choice0 = (parsed.get("choices") or [{}])[0]
                    delta = choice0.get("delta", {})
                    cfr = choice0.get("finish_reason")
                    if cfr:
                        finish_reason = cfr

                    delta_obj = {}
                    if "content" in delta and delta["content"] is not None:
                        delta_obj["content"] = delta["content"]
                    if "reasoning_content" in delta and delta["reasoning_content"] is not None:
                        delta_obj["reasoning_content"] = delta["reasoning_content"]
                    if "tool_calls" in delta and delta["tool_calls"] is not None:
                        delta_obj["tool_calls"] = sanitize_tool_calls(delta["tool_calls"])
                    if delta.get("role"):
                        delta_obj["role"] = delta["role"]

                    if delta_obj:
                        openai_chunk = {
                            "id": response_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [{"index": 0, "delta": delta_obj, "finish_reason": None}],
                        }
                        await response.write(f"data: {json.dumps(openai_chunk)}\n\n".encode())
                except Exception as e:
                    log.warning("SSE 处理异常: %s", e)

        final_chunk = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
        if include_usage and last_usage:
            final_chunk["usage"] = last_usage
        await response.write(f"data: {json.dumps(final_chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()


# ============ 路由 ============

@require_auth
async def handle_chat_completion(request):
    start = time.time()
    session = request.app["client_session"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON body"}}, status=400)

    model = resolve_model(body.get("model", DEFAULT_MODEL))
    body["model"] = model
    agent_id = MODEL_TO_AGENT.get(model, "base2-free")
    is_stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    log.info("请求: model=%s agent=%s messages=%d stream=%s",
             model, agent_id, len(body.get("messages", [])), is_stream)

    try:
        run_id = await get_or_create_agent_run(session, token, agent_id)
    except Exception as e:
        return web.json_response({"error": {"message": str(e)}}, status=502)

    fb_body = make_freebuff_body(body, run_id)

    if is_stream:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream",
                     "Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
        try:
            await response.prepare(request)
            await stream_to_openai_format(session, fb_body, token, response, model, include_usage)
            log.info("流式完成 %dms", int((time.time() - start) * 1000))
        except (ConnectionResetError, asyncio.CancelledError):
            log.info("客户端断开")
        except Exception as e:
            log.error("流式失败: %s", e)
            try:
                err_chunk = {"error": {"message": str(e)}}
                await response.write(f"data: {json.dumps(err_chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
                await response.write_eof()
            except Exception:
                pass
        return response

    try:
        res = await api_request(session, API_BASE, "/api/v1/chat/completions", fb_body, token)
        if res["status"] == 200:
            choice = (res["data"].get("choices") or [{}])[0]
            resp = build_openai_response(run_id, model, choice, res["data"].get("usage"))
            log.info("完成 %dms", int((time.time() - start) * 1000))
            return web.json_response(resp)
        if res["status"] in (400, 404):
            log.warning("Agent Run 失效，重建")
            run_id = await reset_and_create_run(session, token, agent_id)
            fb_body["codebuff_metadata"]["run_id"] = run_id
            retry = await api_request(session, API_BASE, "/api/v1/chat/completions", fb_body, token)
            if retry["status"] == 200:
                choice = (retry["data"].get("choices") or [{}])[0]
                resp = build_openai_response(run_id, model, choice, retry["data"].get("usage"))
                log.info("重试成功 %dms", int((time.time() - start) * 1000))
                return web.json_response(resp)
            return web.json_response({"error": {"message": retry["data"]}}, status=retry["status"])
        return web.json_response({"error": {"message": res["data"]}}, status=res["status"])
    except Exception as e:
        log.exception("请求失败")
        return web.json_response({"error": {"message": str(e)}}, status=500)


@require_auth
async def handle_models(request):
    items = []
    for m in MODEL_TO_AGENT:
        items.append({"id": m, "object": "model", "created": 1700000000, "owned_by": "freebuff"})
    for alias in MODEL_ALIASES:
        items.append({"id": alias, "object": "model", "created": 1700000000, "owned_by": "freebuff-alias"})
    return web.json_response({"object": "list", "data": items})


@require_auth
async def handle_reset_run(request):
    global cached_run_id
    async with run_lock:
        cached_run_id = None
    log.info("Agent Run 缓存已清除")
    return web.json_response({"status": "cleared"})


@require_auth
async def handle_reload_key(request):
    """用旧 key 鉴权通过后，从磁盘/环境重新加载 API Key。"""
    global PROXY_API_KEY
    env_key = os.environ.get("FREEBUFF_PROXY_API_KEY", "").strip()
    if env_key:
        PROXY_API_KEY = env_key
    else:
        config_dir, _ = get_config_paths()
        key_file = config_dir / "proxy_api_key"
        if key_file.exists():
            try:
                k = key_file.read_text(encoding="utf-8").strip()
                PROXY_API_KEY = k or None
            except Exception as e:
                return web.json_response({"error": {"message": str(e)}}, status=500)
        else:
            PROXY_API_KEY = None
    return web.json_response({"status": "reloaded", "enabled": bool(PROXY_API_KEY)})


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "model": DEFAULT_MODEL,
        "cachedRunId": cached_run_id,
        "cachedAgentId": cached_agent_id,
        "apiKeyEnabled": bool(PROXY_API_KEY),
    })


# ============ 主入口 ============

async def run_server(host: str, port: int, lazy_warmup: bool):
    global token, cached_run_id, cached_agent_id, run_lock

    run_lock = asyncio.Lock()
    token = load_token()
    load_proxy_api_key()
    connector = aiohttp.TCPConnector(limit=64)
    session = aiohttp.ClientSession(connector=connector)
    runner: web.AppRunner | None = None

    try:
        if not token:
            token = await do_login(session)
        else:
            log.info("已加载 Token: %s...", token[:30])

        if not lazy_warmup:
            log.info("预热: 创建 Agent Run")
            default_agent = MODEL_TO_AGENT.get(DEFAULT_MODEL, "base2-free")
            try:
                cached_run_id = await create_agent_run(session, token, default_agent)
                cached_agent_id = default_agent
                log.info("预热完成")
            except Exception as e:
                log.warning("预热失败，改为懒加载: %s", e)

        app = web.Application()
        app["client_session"] = session
        app.router.add_post("/v1/chat/completions", handle_chat_completion)
        app.router.add_get("/v1/models", handle_models)
        app.router.add_post("/v1/reset-run", handle_reset_run)
        app.router.add_post("/v1/reload-key", handle_reload_key)
        app.router.add_get("/health", handle_health)
        app.router.add_get("/", handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        log.info("Freebuff OpenAI Proxy 监听 http://%s:%d", host, port)
        log.info("  POST /v1/chat/completions")
        log.info("  GET  /v1/models")
        log.info("  POST /v1/reset-run")
        log.info("  POST /v1/reload-key")
        log.info("  GET  /health")
        if PROXY_API_KEY:
            log.info("API Key 鉴权已启用 (长度 %d)", len(PROXY_API_KEY))
        else:
            log.warning("未设置 API Key")
            if host == "0.0.0.0":
                log.warning("监听 0.0.0.0 且无 API Key，局域网任何人可访问！")
        log.info("可用模型: %s", ", ".join(MODEL_TO_AGENT))
        if MODEL_ALIASES:
            log.info("别名: %s", ", ".join(MODEL_ALIASES))

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, AttributeError):
                pass  # Windows
        await stop_event.wait()
    finally:
        if cached_run_id and token:
            log.info("结束 Agent Run...")
            try:
                await finish_agent_run(session, token, cached_run_id)
            except Exception as e:
                log.warning("结束失败: %s", e)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass
        await session.close()


def cmd_set_api_key(value: str | None):
    if value and value.strip():
        save_proxy_api_key(value.strip())
        print("已保存 API Key。")
        return
    import getpass
    k = getpass.getpass("输入新的 API Key（留空取消）: ").strip()
    if k:
        save_proxy_api_key(k)
        print("已保存 API Key。")
    else:
        print("已取消。")


def cmd_clear_api_key():
    cfg_dir, _ = get_config_paths()
    kf = cfg_dir / "proxy_api_key"
    if kf.exists():
        kf.unlink()
        print("已清除 API Key。")
    else:
        print("未设置 API Key。")


def cmd_show_api_key():
    load_proxy_api_key()
    if PROXY_API_KEY:
        masked = PROXY_API_KEY[:4] + "*" * max(0, len(PROXY_API_KEY) - 8) + PROXY_API_KEY[-4:]
        print(f"当前 API Key: {masked} (长度 {len(PROXY_API_KEY)})")
    else:
        print("未设置 API Key。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="freebuff-proxy", description="Freebuff OpenAI API 反代")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址 (默认 {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认 {DEFAULT_PORT})")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default=None, help="日志文件路径（可选）")
    p.add_argument("--lazy", action="store_true", help="不预热 Agent Run")

    sub = p.add_subparsers(dest="command")
    sp = sub.add_parser("set-api-key", help="设置代理 API Key")
    sp.add_argument("value", nargs="?", help="留空则交互输入")
    sub.add_parser("clear-api-key", help="清除代理 API Key")
    sub.add_parser("show-api-key", help="查看当前 API Key（已遮蔽）")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(args.log_level, args.log_file)

    if args.command == "set-api-key":
        cmd_set_api_key(args.value)
        return
    if args.command == "clear-api-key":
        cmd_clear_api_key()
        return
    if args.command == "show-api-key":
        cmd_show_api_key()
        return

    try:
        asyncio.run(run_server(args.host, args.port, args.lazy))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
