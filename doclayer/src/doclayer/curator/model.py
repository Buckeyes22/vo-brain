"""The curator's model client: the one configured endpoint, nothing else.

Pins one endpoint and one model (default: a local OpenAI-compatible server; the
deployed config may name a hosted model). There is no second-provider fallback — if inference fails, the job
preserves progress and reports the failure (the pipeline treats it as
retryable). Configuration is separate from the resolver's settings.

Requests use the OpenAI-compatible chat-completions contract. Responses are
parsed defensively. Critical object tasks explicitly request JSON-object
decoding; tool calling and JSON-schema decoding are not assumed. A
malformed structured answer gets one bounded repair re-ask with the parse
error shown; truncation and empty outputs are recoverable conditions within
the job's call budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

from doclayer.curator.config import CuratorConfig, llama_swap_target
from doclayer.curator.state import BudgetExceeded, Usage

_RECOVERABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

log = logging.getLogger(__name__)

_METRIC_LINE = re.compile(
    r"^(vllm:num_requests_(?:running|waiting))(?:\{[^}]*\})?\s+([-+0-9.eE]+|[-+]?[Nn]a[Nn]|[-+]?[Ii]nf)\s*$"
)

# This process's in-flight requests per idle_metrics_url. Worker threads each
# run their own event loop, so the count is process-wide and lock-guarded.
_OWN_INFLIGHT: dict[str, int] = {}
_OWN_LOCK = threading.Lock()


def _own_inflight(url: str) -> int:
    with _OWN_LOCK:
        return _OWN_INFLIGHT.get(url, 0)


def _own_adjust(url: str, delta: int) -> None:
    with _OWN_LOCK:
        count = max(0, _OWN_INFLIGHT.get(url, 0) + delta)
        if count:
            _OWN_INFLIGHT[url] = count
        else:
            _OWN_INFLIGHT.pop(url, None)


def parse_vllm_load(text: str) -> tuple[float, float] | None:
    """(running, waiting) summed across label sets from a vLLM Prometheus
    exposition, or None when either gauge is absent or not a finite number."""
    totals: dict[str, float] = {}
    for line in text.splitlines():
        match = _METRIC_LINE.match(line.strip())
        if not match:
            continue
        try:
            value = float(match.group(2))
        except ValueError:
            return None
        if value != value or value in (float("inf"), float("-inf")):
            return None
        totals[match.group(1)] = totals.get(match.group(1), 0.0) + value
    if len(totals) != 2:
        return None
    return totals["vllm:num_requests_running"], totals["vllm:num_requests_waiting"]


class ModelError(RuntimeError):
    """Inference failed after bounded retries (retryable at the job level)."""

    def __init__(self, message: str, *, transient: bool = True, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.diagnostic = diagnostic


class ReplyRejected(ModelError):
    """The model answered, but after the task's own bounded repairs its reply is
    still one the task cannot accept: unparseable, wrongly shaped, wrong
    identities, or invalid anchors, destinations or levels. Never raised for
    transport/outage, budget, context-capacity or binding failures. A per-unit
    task drops just its unit for this error (see degrade.py)."""


class UnitTooLarge(ModelError):
    """One unit's complete task (a section, claim, chunk, move or batch) does
    not fit the bounded input context, so it is never dispatched. A per-unit
    task drops just that unit for this error (see degrade.py); job-level
    limits are BudgetExceeded, not this."""

    def __init__(self, message: str, *, transient: bool = False, diagnostic: dict | None = None) -> None:
        super().__init__(message, transient=transient, diagnostic=diagnostic)


class OutputTruncated(ModelError):
    def __init__(self, text: str) -> None:
        super().__init__("model output truncated (finish_reason=length)")
        self.text = text
        self.transient = False


@dataclass
class ModelReply:
    text: str
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    calls_made: int = 0


def probe_request(inference_url: str, api_key: str | None) -> tuple[str, dict[str, str]]:
    """The reachability probe for an inference endpoint: ``GET <url>/models`` with
    the bearer key. Any HTTP answer at all means reachable."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return f"{inference_url.rstrip('/')}/models", headers


def endpoint_answers(inference_url: str, api_key: str | None) -> bool:
    """Blocking form of the probe (discovery runs synchronously): False only when
    the connection itself fails (refused, no route, timeout, dropped)."""
    url, headers = probe_request(inference_url, api_key)
    try:
        with httpx.Client(timeout=10.0) as client:
            client.get(url, headers=headers)
    except httpx.TransportError:
        return False
    except httpx.HTTPError:
        return True
    return True


@dataclass
class _Semaphore:
    """Bounds in-flight inference requests to ``inference.concurrency``. The
    worker runs the pipeline in one thread, but curation helpers may issue
    concurrent calls; a call over the limit waits for a release. Waiters are
    futures of whichever event loop is running, so one client survives
    successive ``asyncio.run`` loops."""

    limit: int = 1
    held: int = field(default=0)
    _waiters: deque = field(default_factory=deque)

    def try_acquire(self) -> bool:
        if self.held < self.limit:
            self.held += 1
            return True
        return False

    async def acquire(self) -> None:
        while not self.try_acquire():
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    def release(self) -> None:
        self.held = max(0, self.held - 1)
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                break


class ModelClient:
    """Chat-completions against the pinned local endpoint. Not thread-hungry:
    the pipeline awaits calls from one worker loop."""

    def __init__(
        self,
        cfg: CuratorConfig,
        usage: Usage,
        *,
        limits: dict[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        before_call=None,
        admitted_call=None,
        check_cancel=None,
        before_idle_wait=None,
        after_idle_wait=None,
        wait_out_outages: bool = False,
    ) -> None:
        self.cfg = cfg
        self.usage = usage
        self.max_calls = int((limits or {}).get("max_model_calls", cfg.limits.max_model_calls))
        self._transport = transport
        self._before_call = before_call
        self._admitted_call = admitted_call
        self._check_cancel = check_cancel
        #: Called around each stretch of idle-gate waiting so the caller can keep
        #: that time out of its active-time budget.
        self._before_idle_wait = before_idle_wait
        self._after_idle_wait = after_idle_wait
        #: A connection-level POST failure against an endpoint that proves down
        #: waits (unbounded, cancellable, not active time) instead of consuming a
        #: retry. Only callers that supply cancel and idle hooks (the pipeline) opt
        #: in; one-shot callers keep the bounded retries.
        self._wait_out_outages = wait_out_outages
        #: Cumulative seconds spent waiting at the idle gate (never counted
        #: against inference.timeout_s).
        self.idle_wait_seconds = 0.0
        self._gate_failure_logged = False
        self._gate_unreachable_logged = False
        self._outage_logged = False
        self.concurrency = max(1, cfg.inference.concurrency)
        self._slots = _Semaphore(limit=self.concurrency)

    # -- accounting -------------------------------------------------------- #

    def _charge_call(self) -> None:
        if self._before_call is not None:
            self._before_call()
        if self.usage.model_calls >= self.max_calls:
            raise BudgetExceeded("model_calls")
        self.usage.model_calls += 1
        # Persist admission before dispatch: abrupt process death must not
        # make a possibly executed HTTP attempt free on worker restart.
        if self._admitted_call is not None:
            self._admitted_call()

    def _record_usage(self, reply: httpx.Response) -> None:
        try:
            data = reply.json()
        except ValueError:
            return
        usage = data.get("usage") or {}
        self.usage.model_input_tokens += int(usage.get("prompt_tokens") or 0)
        self.usage.model_output_tokens += int(usage.get("completion_tokens") or 0)

    # -- idle gate ----------------------------------------------------------- #

    def _note_unreachable(self, what: str, exc: Exception) -> None:
        if not self._gate_unreachable_logged:
            self._gate_unreachable_logged = True
            log.warning("idle gate: %s unreachable (%s); waiting for the endpoint to return", what, type(exc).__name__)

    def _fail_open(self, what: str, exc: Exception) -> None:
        if not self._gate_failure_logged:
            self._gate_failure_logged = True
            log.warning("idle gate: %s unusable (%s); proceeding without the gate", what, type(exc).__name__)

    async def _model_state(self, url: str, headers: dict[str, str]) -> str:
        """What llama-swap's ``/running`` says about the configured model:
        "ready" (poll its metrics), "empty" (nothing loaded: our request
        cold-loads the model and evicts nothing), "busy" (another model is
        loaded, or ours is starting/stopping) or "unknown" (no /running to ask,
        or it answered unusably: fail open) or "unreachable" (no connection:
        the endpoint is down, so waiting beats failing the request). Polling ``/upstream/<model>/`` while another
        model holds the GPUs could unload it, so metrics are read only when
        ours is already ready."""
        target = llama_swap_target(url)
        if target is None:
            return "unknown"
        running_url, model = target
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                reply = await client.get(running_url, headers=headers)
            reply.raise_for_status()
            rows = reply.json()["running"]
            if not rows:
                return "empty"
            states = [row.get("state") for row in rows if isinstance(row, dict) and row.get("model") == model]
        except httpx.TransportError as exc:
            self._note_unreachable("running-model check", exc)
            return "unreachable"
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self._fail_open("running-model check", exc)
            return "unknown"
        return "ready" if "ready" in states else "busy"

    async def _others_busy(self, url: str, api_key: str | None) -> str | None:
        """Why a request should wait: "unreachable" when the endpoint cannot be
        reached at all (a request would only fail; wait for it to return), "busy"
        when other users are running or queued, or another model (or ours, not yet
        ready) occupies it; None when it may proceed. Fails open when it answers
        unusably: None."""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        state = await self._model_state(url, headers)
        if state in ("busy", "unreachable"):
            return state
        if state == "empty":
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                reply = await client.get(url, headers=headers)
            reply.raise_for_status()
            load = parse_vllm_load(reply.text)
            if load is None:
                raise ValueError("vllm request gauges missing")
        except httpx.TransportError as exc:
            self._note_unreachable("metrics", exc)
            return "unreachable"
        except (httpx.HTTPError, ValueError) as exc:
            self._fail_open("metrics", exc)
            return None
        running, waiting = load
        return "busy" if waiting > 0 or running > _own_inflight(url) else None

    async def _wait_until_idle(self, api_key: str | None) -> bool:
        """Wait while the gated endpoint is busy or unreachable. True when any
        poll, including the first, found it unreachable (it was down, not merely
        busy). Returned, not stored: concurrent requests share this client."""
        url = self.cfg.inference.idle_metrics_url
        reason = await self._others_busy(url, api_key) if url else None
        if reason is None:
            return False
        saw_unreachable = reason == "unreachable"
        poll = max(0.0, float(self.cfg.inference.idle_poll_s))
        if self._before_idle_wait is not None:
            self._before_idle_wait()  # charge active time up to here, then stop charging
        try:
            while reason is not None:
                if self._check_cancel is not None:
                    self._check_cancel()
                started = time.monotonic()
                try:
                    await asyncio.sleep(poll)
                finally:
                    waited = time.monotonic() - started
                    self.idle_wait_seconds += waited
                    self.usage.idle_wait_seconds += waited
                reason = await self._others_busy(url, api_key)
                saw_unreachable = saw_unreachable or reason == "unreachable"
        finally:
            if self._after_idle_wait is not None:
                self._after_idle_wait()
        return saw_unreachable

    async def _endpoint_reachable(self, api_key: str | None) -> bool:
        """Ungated probe: ``GET <url>/models``. Any HTTP answer at all (200, 401,
        404, ...) means reachable; only a connection-level failure means down."""
        url, headers = probe_request(self.cfg.inference.url, api_key)
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                await client.get(url, headers=headers)
        except httpx.TransportError:
            return False
        except httpx.HTTPError:
            return True  # something answered, just not usefully
        return True

    async def _wait_out_outage(self, exc: httpx.TransportError, api_key: str | None) -> bool:
        """After a connection-level POST failure: True when the endpoint was down
        and has now returned (the caller retries without spending an attempt);
        False when it is reachable (a blip: the caller counts a normal attempt).
        The wait has no timeout, is not active time, and honours cancel. A gate
        wait that only saw the endpoint busy is not an outage: a normal attempt."""
        if not self._wait_out_outages:
            return False
        if self.cfg.inference.idle_metrics_url:
            started = self.usage.idle_wait_seconds
            if not await self._wait_until_idle(api_key):
                return False
            log.info(
                "inference endpoint returned after %.0fs at the idle gate (after %s)",
                self.usage.idle_wait_seconds - started,
                type(exc).__name__,
            )
            return True
        if await self._endpoint_reachable(api_key):
            return False
        if not self._outage_logged:
            self._outage_logged = True
            log.warning("inference endpoint unreachable (%s); waiting for it to return", type(exc).__name__)
        poll = max(0.0, float(self.cfg.inference.idle_poll_s))
        waited_total = 0.0
        if self._before_idle_wait is not None:
            self._before_idle_wait()  # charge active time up to here, then stop charging
        try:
            while True:
                if self._check_cancel is not None:
                    self._check_cancel()
                started = time.monotonic()
                try:
                    await asyncio.sleep(poll)
                finally:
                    waited = time.monotonic() - started
                    waited_total += waited
                    self.idle_wait_seconds += waited
                    self.usage.idle_wait_seconds += waited
                if await self._endpoint_reachable(api_key):
                    break
        finally:
            if self._after_idle_wait is not None:
                self._after_idle_wait()
        self._outage_logged = False  # the next outage warns again
        log.info("inference endpoint returned after %.0fs", waited_total)
        return True

    # -- raw call ------------------------------------------------------------ #

    async def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = True,
        json_object: bool = False,
        max_output_tokens: int | None = None,
        enable_thinking: bool | None = None,
        thinking_token_budget: int | None = None,
    ) -> ModelReply:
        """One chat completion with bounded transient retries. Raises
        BudgetExceeded (caller records it), ModelError (retryable), or
        OutputTruncated (caller splits the work)."""
        if type(json_object) is not bool or (json_object and not json_mode):
            raise ModelError("JSON object decoding requires a JSON task", transient=False)
        payload: dict[str, Any] = {
            "model": self.cfg.inference.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": min(
                max_output_tokens or self.cfg.inference.effective_output_target,
                self.cfg.inference.effective_output_target,
            ),
            "stream": False,
            "chat_template_kwargs": {
                "enable_thinking": (self.cfg.inference.enable_thinking if enable_thinking is None else enable_thinking)
            },
        }
        if thinking_token_budget is not None:
            if type(thinking_token_budget) is not int or not 0 <= thinking_token_budget < payload["max_tokens"]:
                raise ModelError(
                    "thinking token budget must be an integer below the total output target", transient=False
                )
            payload[self.cfg.inference.thinking_budget_field] = thinking_token_budget
        if json_mode:
            payload["temperature"] = 0.2
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        if self.cfg.inference.request_priority is not None:
            payload["priority"] = self.cfg.inference.request_priority
        api_key = self.cfg.inference_api_key()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        last_error: Exception | None = None
        attempt = 0
        while attempt <= self.cfg.inference.retries:
            self._charge_call()
            try:
                await self._slots.acquire()  # at most inference.concurrency requests in flight
                try:
                    await self._wait_until_idle(api_key)
                    gate_url = self.cfg.inference.idle_metrics_url
                    if gate_url:
                        _own_adjust(gate_url, 1)
                    try:
                        async with httpx.AsyncClient(
                            timeout=self.cfg.inference.timeout_s,
                            transport=self._transport,
                        ) as client:
                            reply = await client.post(
                                f"{self.cfg.inference.url.rstrip('/')}/chat/completions",
                                json=payload,
                                headers=headers,
                            )
                    finally:
                        if gate_url:
                            _own_adjust(gate_url, -1)
                finally:
                    self._slots.release()
            except httpx.HTTPError as exc:
                last_error = ModelError(f"inference unreachable: {type(exc).__name__}")
                if isinstance(exc, httpx.TransportError) and await self._wait_out_outage(exc, api_key):
                    continue  # the endpoint was down and is back: retry, attempt not spent
            else:
                if reply.status_code == 200:
                    self._record_usage(reply)
                    try:
                        data = reply.json()
                        choice = (data.get("choices") or [{}])[0]
                        text = choice.get("message", {}).get("content") or ""
                        finish = choice.get("finish_reason")
                    except ValueError:
                        raise ModelError("inference returned non-JSON body")
                    if not isinstance(text, str):
                        raise ModelError("inference returned a non-string message")
                    if finish == "length":
                        raise OutputTruncated(text)
                    return ModelReply(text=text, finish_reason=finish, calls_made=attempt + 1)
                if reply.status_code in _RECOVERABLE_STATUS:
                    last_error = ModelError(f"inference HTTP {reply.status_code} (transient)")
                elif reply.status_code == 401 or reply.status_code == 403:
                    raise ModelError(
                        f"inference rejected credentials: HTTP {reply.status_code}",
                        transient=False,
                    )
                else:
                    raise ModelError(f"inference HTTP {reply.status_code}", transient=False)
            # bounded backoff between attempts
            if attempt < self.cfg.inference.retries:
                await asyncio.sleep(min(2.0 * (attempt + 1), 5.0))
            attempt += 1
        raise last_error or ModelError("inference failed")

    # -- structured JSON with bounded repair --------------------------------- #

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int | None = None,
        repair_attempts: int = 1,
        root_array_key: str | None = None,
        prompt_char_budget: int | None = None,
        json_object: bool = False,
        enable_thinking: bool | None = None,
        thinking_token_budget: int | None = None,
    ) -> dict[str, Any]:
        """A JSON object from the model, with one bounded repair re-ask when
        parsing fails. Parse errors are shown back to the model verbatim."""
        reply = await self.chat(
            system,
            user,
            json_mode=True,
            json_object=json_object,
            max_output_tokens=max_output_tokens,
            enable_thinking=enable_thinking,
            thinking_token_budget=thinking_token_budget,
        )
        parsed, diagnostic = _decode_json(reply.text, root_array_key=root_array_key)
        if parsed is not None:
            return parsed
        if repair_attempts > 0:
            expected = "object/array" if root_array_key else "object"
            header = f"JSON only: {expected}.\nError: {diagnostic['error']}\nKeep IDs/data.\n"
            available = (
                max(0, prompt_char_budget - len(system) - len(user))
                if prompt_char_budget is not None
                else len(header) + 4010
            )
            # Do not spend another request on identical input when actionable
            # feedback cannot fit. Never sacrifice original source/claim input.
            if available >= len(header) + 6:
                suffix = "\n\n---\n" + header
                suffix += ("Previous reply:\n" + reply.text[:4000])[: available - len(suffix)]
                reply = await self.chat(
                    system,
                    user + suffix,
                    json_mode=True,
                    json_object=json_object,
                    max_output_tokens=max_output_tokens,
                    enable_thinking=enable_thinking,
                    thinking_token_budget=thinking_token_budget,
                )
                parsed, diagnostic = _decode_json(reply.text, root_array_key=root_array_key)
                if parsed is not None:
                    return parsed
            else:
                diagnostic["repair_unavailable"] = "insufficient room for actionable JSON feedback"
        raise ReplyRejected(
            "model did not return parseable JSON",
            transient=True,
            diagnostic={
                "operation": "json-parse",
                "root_array_key": root_array_key,
                "first_character": reply.text.lstrip()[:1],
                "response_chars": len(reply.text),
                **diagnostic,
            },
        )

    async def chat_verdicts(self, system: str, user: str, *, char_budget: int | None = None) -> dict[str, Any]:
        """Preserve an explicit whole verdict array; the caller checks its schema."""
        return await self.chat_json(
            system,
            user,
            root_array_key="verdicts",
            prompt_char_budget=char_budget or self.cfg.limits.prompt_char_budget,
        )

    async def chat_bounded_json(
        self,
        system: str,
        user: str,
        *,
        char_budget: int,
        enable_thinking: bool | None = None,
        thinking_token_budget: int | None = None,
        json_object: bool = False,
    ) -> dict[str, Any]:
        """Object task whose parse repair also preserves the caller's input bound."""
        if len(system) + len(user) > char_budget:
            raise UnitTooLarge(
                f"structured task exceeds bounded input context: {len(system) + len(user)} characters with budget {char_budget}"
            )
        return await self.chat_json(
            system,
            user,
            prompt_char_budget=char_budget,
            enable_thinking=enable_thinking,
            thinking_token_budget=thinking_token_budget,
            json_object=json_object,
        )


def _decode_json(text: str, *, root_array_key: str | None = None):
    """Strict root parsing with bounded, source-free failure diagnostics."""
    if not text or not text.strip():
        return None, {"error": "empty JSON response", "error_kind": "empty"}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        newline = cleaned.find("\n")
        if newline != -1:
            cleaned = cleaned[newline + 1 :]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    candidate = cleaned.strip()
    root_offset = 0
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as original:
        starts = [index for marker in ("{", "[") if (index := candidate.find(marker)) >= 0]
        if not starts:
            return None, {
                "error": original.msg,
                "error_kind": "decode",
                "position": original.pos,
                "line": original.lineno,
                "column": original.colno,
            }
        root_offset = min(starts)
        if root_offset:
            prefix = candidate[:root_offset].strip()
            try:
                json.JSONDecoder().raw_decode(prefix)
            except json.JSONDecodeError:
                pass
            else:
                return None, {"error": "multiple top-level JSON values", "error_kind": "multiple-roots"}
        try:
            data, end = json.JSONDecoder().raw_decode(candidate[root_offset:])
        except json.JSONDecodeError as error:
            position = root_offset + error.pos
            prefix = candidate[:position]
            return None, {
                "error": error.msg,
                "error_kind": "decode",
                "position": position,
                "line": prefix.count("\n") + 1,
                "column": len(prefix.rsplit("\n", 1)[-1]) + 1,
            }
        trailing = candidate[root_offset + end :].lstrip()
        extra = False
        if trailing:
            try:
                json.JSONDecoder().raw_decode(trailing)
            except json.JSONDecodeError:
                pass
            else:
                extra = True
            for match in re.finditer(r"[\{\[]", trailing):
                try:
                    json.JSONDecoder().raw_decode(trailing[match.start() :])
                except json.JSONDecodeError:
                    continue
                extra = True
                break
        if extra:
            return None, {"error": "multiple top-level JSON values", "error_kind": "multiple-roots"}
    if isinstance(data, dict):
        return data, {}
    if isinstance(data, list) and root_array_key:
        return {root_array_key: data}, {}
    return None, {
        "error": "expected JSON object" + (" or whole verdict array" if root_array_key else ""),
        "error_kind": "root-type",
        "root_type": type(data).__name__,
    }


def extract_json(text: str, *, root_array_key: str | None = None) -> dict[str, Any] | None:
    """Preserve one whole root; never expose children of malformed outer JSON."""
    return _decode_json(text, root_array_key=root_array_key)[0]
