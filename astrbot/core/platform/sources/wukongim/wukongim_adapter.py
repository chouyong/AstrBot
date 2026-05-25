"""WuKongIM (悟空IM) Platform adapter for AstrBot.

Status:       DRAFT v2 — adds wake-up rules + handoff for 3-mode AI design.
Architecture: Webhook (POST → us) for inbound; HTTP REST (`/message/send`)
              for outbound.
Modeled on:   ``astrbot.core.platform.sources.lark.lark_adapter``.

Three AI intervention modes (v1.1 architecture doc §5)
------------------------------------------------------
* Mode A — Customer-service direct (channel_type = 3 客服频道):
    Bot responds to every inbound message. Used for entry-point CS chat.
* Mode B — AI in groups (channel_type = 2 群组):
    Bot responds ONLY when @bot or when a configured wake keyword is hit.
    Otherwise the message is dropped and never reaches the LLM.
* Mode C — AI in buyer/seller 1:1 (channel_type = 1 点对点):
    Same wake rule as Mode B. Business backend pre-adds ``bot_uid`` to the
    ``deal_<orderId>`` channel right after payment, then either party can
    @ the bot to invoke it.

What this adapter owns
----------------------
* Hosts an aiohttp web server that receives WuKongIM 第三方 webhook
  events (``msg.notify`` etc.) at a configurable path.
* Decodes the WuKongIM payload envelope (base64 → JSON → text/image/...)
  into an ``AstrBotMessage`` and pushes it onto AstrBot's event queue.
* **Applies the wake-up rule** before enqueuing — non-woken inbound is
  dropped early so the LLM is not spammed by every group message.
* Sends outbound messages by calling
  ``POST {api_base}/message/send`` against the WuKongIM core HTTP API
  using the bot's fixed UID + system token.
* Exposes ``request_handoff()`` so plugins / LLM tools can hand a session
  off to a human operator via the business backend's ``/api/im/handoff``
  endpoint.

What this adapter does NOT own
------------------------------
* Token issuance for end users — your PHP backend mints WuKongIM tokens
  for human users via WuKongIM's ``/user/token`` endpoint.
* The ``/datasource`` callback that WuKongIM hits to look up
  user/channel/blocklist info — that is also served by the PHP backend.
* Authentication / friend graph — WuKongIM only routes messages.

What is stubbed (TODO before production)
----------------------------------------
* Image/file inbound: we recognise ``type != 1`` payloads, log a TODO,
  and skip them. Plain text (``type == 1``) is the only fully-working
  inbound path.
* Outbound image/file: only ``Plain`` is encoded into the WuKongIM
  payload. ``Image`` becomes a ``[image]`` placeholder, ``At`` becomes
  ``@<id>``. Real attachments require the WuKongIM file service
  (``/v1/file/upload``) which is not wired yet.
* Webhook signature verification: a ``webhook_secret`` HMAC-SHA256 hook
  is sketched but the exact header name varies by WuKongIM deployment;
  defaults to the ``X-Signature`` header WuKongIM 2.x uses.

How to enable
-------------
Add to AstrBot's platform config (``data/config/astrbot_config.json``)::

    {
      "platform": [
        {
          "id": "wukongim_main",
          "type": "wukongim",
          "enable": true,
          "api_base_url": "http://localhost:5001",
          "webhook_host": "0.0.0.0",
          "webhook_port": 18080,
          "webhook_path": "/astrbot/wukongim/webhook",
          "webhook_secret": "",
          "bot_uid": "astrbot_cs_bot",
          "bot_token": "<system token issued by WuKongIM>",
          "wake_keywords": ["@客服", "@机器人", "找客服", "人工"],
          "platform_id": "qjl",
          "handoff_endpoint": "http://deepmatch/api/im/handoff",
          "handoff_token": "<shared secret with deepmatch>"
        }
      ]
    }

Then in WuKongIM's config, point ``webhook.url`` (or
``webhook.grpc.addr`` if you prefer gRPC; not supported here) at
``http://<host>:<webhook_port><webhook_path>``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any, cast

import aiohttp
from aiohttp import web

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from ...register import register_platform_adapter
from .wukongim_event import WuKongIMMessageEvent

# --- WuKongIM protocol constants (v2.0.6) -----------------------------------
# channel_type values per WuKongIM core:
#   1 = person (1-1 chat)
#   2 = group
#   3 = customer service
#   4 = community
WK_CHANNEL_TYPE_PERSON = 1
WK_CHANNEL_TYPE_GROUP = 2
WK_CHANNEL_TYPE_CUSTOMER_SERVICE = 3

# Inner payload "type" values:
#   1 = text, 2 = image, 3 = gif, 4 = voice, 5 = video, 6 = location, ...
WK_PAYLOAD_TYPE_TEXT = 1
WK_PAYLOAD_TYPE_IMAGE = 2

# Default endpoints
DEFAULT_API_BASE = "http://localhost:5001"
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 18080
DEFAULT_WEBHOOK_PATH = "/astrbot/wukongim/webhook"

# Dedup window for webhook events (seconds)
DEDUP_WINDOW_SECONDS = 1800


@register_platform_adapter(
    "wukongim",
    "悟空IM (WuKongIM) 平台适配器，基于 Webhook + HTTP API",
    support_streaming_message=False,
)
class WuKongIMPlatformAdapter(Platform):
    """AstrBot Platform adapter for WuKongIM."""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        # ---- read config ----
        self.api_base: str = str(
            platform_config.get("api_base_url", DEFAULT_API_BASE),
        ).rstrip("/")
        self.webhook_host: str = platform_config.get(
            "webhook_host",
            DEFAULT_WEBHOOK_HOST,
        )
        self.webhook_port: int = int(
            platform_config.get("webhook_port", DEFAULT_WEBHOOK_PORT),
        )
        self.webhook_path: str = platform_config.get(
            "webhook_path",
            DEFAULT_WEBHOOK_PATH,
        )
        self.webhook_secret: str = platform_config.get("webhook_secret", "") or ""

        self.bot_uid: str = platform_config.get("bot_uid", "astrbot_cs_bot")
        self.bot_token: str = platform_config.get("bot_token", "") or ""
        self.bot_name = "astrbot"

        # ---- v1.1 wake-up + tenant + handoff config ----
        # Wake keywords trigger the bot in CT=1 / CT=2 even without an explicit
        # @<bot_uid> mention. Always lowercase-matched. Empty = only @bot wakes.
        wake_kw_raw = platform_config.get("wake_keywords") or []
        if isinstance(wake_kw_raw, str):
            wake_kw_raw = [s.strip() for s in wake_kw_raw.split(",") if s.strip()]
        self.wake_keywords: list[str] = [str(k) for k in wake_kw_raw]

        # platform_id labels which tenant this adapter instance serves
        # (e.g. "qjl" for 全金链, "syy" for 数易元). It is surfaced to the LLM
        # via PlatformMetadata.id so per-tenant agents / MCP allowlists kick in.
        self.platform_id: str = str(
            platform_config.get("platform_id") or platform_config.get("id") or "",
        )

        # Handoff endpoint on the business backend (e.g. deepmatch). Optional.
        self.handoff_endpoint: str = (
            platform_config.get("handoff_endpoint", "") or ""
        ).rstrip("/")
        self.handoff_token: str = platform_config.get("handoff_token", "") or ""

        # Channels currently in human-handoff state — adapter drops inbound for
        # these so the LLM does not interrupt a human operator. Cleared by the
        # business backend calling ``release_handoff(channel_id)`` (or by TTL).
        self._handoff_channels: dict[str, float] = {}
        self.handoff_ttl_seconds: int = int(
            platform_config.get("handoff_ttl_seconds", 3600),
        )

        # ---- runtime state ----
        # WuKongIM webhook envelopes carry a ``client_msg_no`` (or
        # ``message_id``) that we use as the dedup key. Same shape as
        # lark_adapter._is_duplicate_event.
        self.event_id_timestamps: dict[str, float] = {}

        self._http_session: aiohttp.ClientSession | None = None
        self._aio_runner: web.AppRunner | None = None
        self._aio_site: web.BaseSite | None = None

    # ------------------------------------------------------------------ meta

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="wukongim",
            description="悟空IM (WuKongIM) 平台适配器，基于 Webhook + HTTP API",
            id=cast(str, self.config.get("id")),
            support_streaming_message=False,
        )

    # ----------------------------------------------------- lifecycle: run/stop

    async def run(self) -> None:
        """Start the aiohttp webhook server.

        Returns once the server is listening; the server keeps running in the
        background until ``terminate()`` is called.
        """
        # outbound HTTP client (kept alive for the lifetime of the adapter)
        self._http_session = aiohttp.ClientSession()

        app = web.Application()
        app.router.add_post(self.webhook_path, self.handle_webhook)
        # Health probe — handy for Docker/k8s
        app.router.add_get(self.webhook_path + "/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.webhook_host, self.webhook_port)
        await site.start()

        self._aio_runner = runner
        self._aio_site = site

        logger.info(
            f"[WuKongIM] Webhook server listening on "
            f"http://{self.webhook_host}:{self.webhook_port}{self.webhook_path} "
            f"(api_base={self.api_base}, bot_uid={self.bot_uid})",
        )

    async def terminate(self) -> None:
        if self._aio_runner is not None:
            try:
                await self._aio_runner.cleanup()
            except Exception as e:  # pragma: no cover - shutdown best effort
                logger.warning(f"[WuKongIM] aiohttp runner cleanup failed: {e}")
            self._aio_runner = None
            self._aio_site = None

        if self._http_session is not None:
            try:
                await self._http_session.close()
            except Exception as e:  # pragma: no cover
                logger.warning(f"[WuKongIM] http session close failed: {e}")
            self._http_session = None

        logger.info("悟空IM (WuKongIM) 适配器已关闭")

    # ----------------------------------------------------- webhook plumbing

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "platform": "wukongim"})

    async def handle_webhook(self, request: web.Request) -> web.Response:
        """aiohttp handler for incoming WuKongIM webhook POSTs."""
        try:
            raw_body = await request.read()
        except Exception as e:
            logger.error(f"[WuKongIM] Failed to read webhook body: {e}")
            return web.json_response({"status": "error"}, status=400)

        # optional HMAC-SHA256 signature check
        if self.webhook_secret:
            sig_header = request.headers.get("X-Signature", "")
            expected = hmac.new(
                self.webhook_secret.encode("utf-8"),
                raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                logger.warning("[WuKongIM] Webhook signature mismatch — dropping")
                return web.json_response({"status": "forbidden"}, status=403)

        try:
            envelope = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            logger.error(f"[WuKongIM] Webhook JSON decode failed: {e}")
            return web.json_response({"status": "bad_json"}, status=400)

        if not isinstance(envelope, dict):
            logger.error(f"[WuKongIM] Webhook envelope is not an object: {envelope!r}")
            return web.json_response({"status": "bad_shape"}, status=400)

        event_name = envelope.get("event", "")
        # WuKongIM 2.x emits events such as: msg.notify, msg.offline,
        # msg.delivered, online_status, ... Only msg.notify carries chat
        # content the bot needs to reply to.
        if event_name != "msg.notify":
            logger.debug(f"[WuKongIM] Skipping non-chat event: {event_name}")
            # Acknowledge — WuKongIM expects 200 to stop retry storms.
            return web.json_response({"status": "ignored"})

        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            logger.error("[WuKongIM] msg.notify data is not an object")
            return web.json_response({"status": "bad_data"}, status=400)

        # dedup
        msg_no = str(data.get("client_msg_no") or data.get("message_id") or "")
        if msg_no and self._is_duplicate_event(msg_no):
            logger.debug(f"[WuKongIM] Skip duplicate event client_msg_no={msg_no}")
            return web.json_response({"status": "dup"})

        try:
            abm = await self.convert_msg(data)
        except Exception as e:
            logger.error(f"[WuKongIM] convert_msg failed: {e}", exc_info=True)
            return web.json_response({"status": "convert_failed"}, status=200)

        if abm is not None:
            await self.handle_msg(abm)

        # Always 200 quickly so WuKongIM does not retry. The actual LLM
        # work happens asynchronously via the event queue.
        return web.json_response({"status": "ok"})

    # ---------------------------------------------------- payload → AstrBotMessage

    async def convert_msg(self, payload: dict) -> AstrBotMessage | None:
        """Convert a WuKongIM ``msg.notify.data`` dict into AstrBotMessage.

        Returns ``None`` if the payload is malformed or unsupported.
        """
        from_uid = str(payload.get("from_uid") or "")
        channel_id = str(payload.get("channel_id") or "")
        channel_type = int(payload.get("channel_type") or WK_CHANNEL_TYPE_PERSON)
        client_msg_no = str(payload.get("client_msg_no") or "")
        message_id = str(payload.get("message_id") or client_msg_no or "")
        ts_raw = payload.get("timestamp")

        if not from_uid or not channel_id:
            logger.error(f"[WuKongIM] Missing from_uid/channel_id: {payload!r}")
            return None

        # ignore messages sent by the bot itself (echoes)
        if from_uid == self.bot_uid:
            logger.debug(f"[WuKongIM] Skip self-echo from {from_uid}")
            return None

        # decode inner payload (base64 → JSON)
        inner = self._decode_inner_payload(payload.get("payload"))
        if inner is None:
            logger.warning(f"[WuKongIM] Could not decode inner payload: {payload!r}")
            return None

        components, message_str = self._inner_to_components(inner)
        if not components:
            logger.debug(
                f"[WuKongIM] No supported components in inner payload: {inner!r}",
            )
            return None

        # ---- v1.1 wake-up gate ------------------------------------------------
        # Drop messages that should not invoke the LLM (e.g. group chatter
        # without an @bot mention, or a 1:1 deal channel idle conversation).
        if not self._should_wake(
            channel_type=channel_type,
            content_text=message_str,
            from_uid=from_uid,
            channel_id=channel_id,
        ):
            logger.debug(
                f"[WuKongIM] Not woken — channel_id={channel_id} "
                f"channel_type={channel_type} from_uid={from_uid}",
            )
            return None

        abm = AstrBotMessage()
        abm.message = components
        abm.message_str = message_str
        abm.message_id = message_id or f"wk_{int(time.time() * 1000)}"
        abm.self_id = self.bot_uid or self.bot_name
        abm.raw_message = payload
        try:
            abm.timestamp = int(ts_raw) if ts_raw is not None else int(time.time())
        except (TypeError, ValueError):
            abm.timestamp = int(time.time())

        abm.sender = MessageMember(
            user_id=from_uid,
            nickname=from_uid[:8],
        )

        # type + session_id mapping
        if channel_type == WK_CHANNEL_TYPE_GROUP:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = channel_id
            abm.session_id = channel_id
        elif channel_type == WK_CHANNEL_TYPE_CUSTOMER_SERVICE:
            # Customer service: still treat as 1-1 for the LLM dispatcher,
            # but tag the session_id so downstream logic knows.
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = f"cs:{channel_id}"
        else:
            # WK_CHANNEL_TYPE_PERSON or unknown
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = from_uid

        return abm

    @staticmethod
    def _decode_inner_payload(raw: Any) -> dict[str, Any] | None:
        """WuKongIM webhook payloads are base64(JSON)."""
        if raw is None:
            return None
        if isinstance(raw, dict):
            # Some deployments forward already-decoded payloads.
            return raw
        if not isinstance(raw, str):
            return None
        try:
            decoded = base64.b64decode(raw)
        except Exception as e:
            logger.error(f"[WuKongIM] base64 decode failed: {e}")
            return None
        try:
            obj = json.loads(decoded.decode("utf-8"))
        except Exception as e:
            logger.error(f"[WuKongIM] inner payload JSON decode failed: {e}")
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _inner_to_components(
        inner: dict[str, Any],
    ) -> tuple[list[Comp.BaseMessageComponent], str]:
        """Translate WuKongIM inner payload → AstrBot message components."""
        components: list[Comp.BaseMessageComponent] = []
        wk_type = int(inner.get("type") or WK_PAYLOAD_TYPE_TEXT)

        if wk_type == WK_PAYLOAD_TYPE_TEXT:
            text = str(inner.get("content", ""))
            # WuKongIM "mention" extension lives at inner["mention"]["uids"]
            mention = inner.get("mention") or {}
            mention_uids = (
                mention.get("uids") if isinstance(mention, dict) else None
            ) or []
            for uid in mention_uids:
                if uid:
                    components.append(Comp.At(qq=str(uid), name=str(uid)))
            if text:
                components.append(Comp.Plain(text))
            return components, text

        if wk_type == WK_PAYLOAD_TYPE_IMAGE:
            # TODO: download from WuKongIM file API
            #   inner["url"] / inner["file_id"] / inner["width"] / inner["height"]
            # Real implementation should:
            #   1. GET <api_base>{inner["url"]} (or fetch from WuKongIM file
            #      service)
            #   2. wrap as Comp.Image.fromBytes / fromURL
            #
            # FIX (codex audit FAIL-1): previous draft returned a
            # ``Comp.Plain("[image]")`` placeholder, which then flowed into
            # the LLM context as if the user had literally typed "[image]".
            # Until real media is implemented we drop the message entirely:
            # return EMPTY components so ``convert_msg`` short-circuits BEFORE
            # the wake gate / event_queue.put_nowait. The message is logged
            # for forensics but never reaches the agent.
            logger.info(
                "[WuKongIM] image inbound dropped (not yet supported); "
                f"url={inner.get('url')!r} file_id={inner.get('file_id')!r}",
            )
            return [], ""

        # TODO: gif (3), voice (4), video (5), location (6), card, custom...
        # Same FAIL-1 fix: do NOT synthesise a Plain placeholder; drop instead.
        logger.info(
            f"[WuKongIM] inbound payload type={wk_type} not supported, dropping; "
            f"raw={inner!r}",
        )
        return [], ""

    # ---------------------------------------------------- queue handoff

    async def handle_msg(self, abm: AstrBotMessage) -> None:
        event = WuKongIMMessageEvent(
            message_str=abm.message_str,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            adapter=self,
        )
        self._event_queue.put_nowait(event)

    # ---------------------------------------------------- outbound

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ) -> None:
        """Send a MessageChain to a WuKongIM channel by session id."""
        if session.message_type == MessageType.GROUP_MESSAGE:
            channel_id = session.session_id
            channel_type = WK_CHANNEL_TYPE_GROUP
        else:
            # Customer-service sessions are tagged "cs:<channel_id>"
            sid = session.session_id
            if sid.startswith("cs:"):
                channel_id = sid[3:]
                channel_type = WK_CHANNEL_TYPE_CUSTOMER_SERVICE
            else:
                channel_id = sid
                channel_type = WK_CHANNEL_TYPE_PERSON

        await self.send_chain(
            channel_id=channel_id,
            channel_type=channel_type,
            message_chain=message_chain,
        )
        await super().send_by_session(session, message_chain)

    async def send_chain(
        self,
        *,
        channel_id: str,
        channel_type: int,
        message_chain: MessageChain,
    ) -> bool:
        """Encode + send a MessageChain via WuKongIM ``/message/send``."""
        text = self._chain_to_text(message_chain)
        if not text:
            logger.debug("[WuKongIM] Empty chain after rendering — nothing to send")
            return False

        return await self._send_text(
            channel_id=channel_id,
            channel_type=channel_type,
            text=text,
        )

    @staticmethod
    def _chain_to_text(chain: MessageChain) -> str:
        """Render a MessageChain to a WuKongIM-flavoured plain string.

        Image/file/audio/video are rendered as placeholders; outbound media
        upload is a TODO (see module docstring).
        """
        parts: list[str] = []
        for comp in chain.chain:
            if isinstance(comp, Comp.Plain):
                if comp.text:
                    parts.append(comp.text)
            elif isinstance(comp, Comp.At):
                ident = str(comp.qq or comp.name or "").strip()
                if ident:
                    parts.append(f"@{ident}")
            elif isinstance(comp, Comp.Image):
                # TODO: upload to WuKongIM file service then send type=2 payload
                parts.append("[image]")
            else:
                # File / Record / Video / Reply / etc.
                logger.debug(
                    f"[WuKongIM] TODO: outbound component {type(comp).__name__} "
                    f"not implemented, rendering as placeholder",
                )
                parts.append(f"[{type(comp).__name__.lower()}]")
        return "".join(parts).strip()

    async def _send_text(
        self,
        *,
        channel_id: str,
        channel_type: int,
        text: str,
    ) -> bool:
        """POST a single text message to ``{api_base}/message/send``."""
        if self._http_session is None:
            logger.error("[WuKongIM] HTTP session not initialised; call run() first")
            return False

        inner_payload = json.dumps(
            {"type": WK_PAYLOAD_TYPE_TEXT, "content": text},
            ensure_ascii=False,
        )
        payload_b64 = base64.b64encode(inner_payload.encode("utf-8")).decode("ascii")

        body: dict[str, Any] = {
            "header": {"no_persist": 0, "red_dot": 1, "sync_once": 0},
            "from_uid": self.bot_uid,
            "channel_id": channel_id,
            "channel_type": channel_type,
            "payload": payload_b64,
        }

        url = f"{self.api_base}/message/send"
        headers = {"Content-Type": "application/json"}
        if self.bot_token:
            # WuKongIM admin/system endpoints accept the system token via
            # the ``token`` query string OR an ``Authorization`` header
            # depending on deployment. Sending both is harmless.
            headers["Authorization"] = self.bot_token
            url = f"{url}?token={self.bot_token}"

        try:
            async with self._http_session.post(
                url,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        f"[WuKongIM] /message/send HTTP {resp.status}: {resp_text}",
                    )
                    return False
                logger.debug(f"[WuKongIM] /message/send ok: {resp_text}")
                return True
        except Exception as e:
            logger.error(f"[WuKongIM] /message/send raise: {e}", exc_info=True)
            return False

    # ------------------------------------------------------ dedup helper

    def _clean_expired_events(self) -> None:
        """Drop dedup keys older than 30 minutes."""
        now = time.time()
        expired = [
            k
            for k, ts in self.event_id_timestamps.items()
            if now - ts > DEDUP_WINDOW_SECONDS
        ]
        for k in expired:
            del self.event_id_timestamps[k]

    def _is_duplicate_event(self, client_msg_no: str) -> bool:
        """Return True if we have already processed this client_msg_no recently.

        Modeled on ``LarkPlatformAdapter._is_duplicate_event``: a 30-minute
        sliding window keyed off whatever id WuKongIM gives us.
        """
        self._clean_expired_events()
        if client_msg_no in self.event_id_timestamps:
            return True
        self.event_id_timestamps[client_msg_no] = time.time()
        return False

    # ------------------------------------------------------ v1.1 wake-up gate

    def _should_wake(
        self,
        *,
        channel_type: int,
        content_text: str,
        from_uid: str,
        channel_id: str,
    ) -> bool:
        """Decide whether an inbound message should reach the LLM.

        Wake matrix (architecture doc §5.2):

        | channel_type | rule                                                   |
        |--------------|--------------------------------------------------------|
        | 3 (CS)       | always wake — except messages already in handoff       |
        | 1 (1-1)      | wake only on @<bot_uid> or matching ``wake_keywords``  |
        | 2 (group)    | wake only on @<bot_uid> or matching ``wake_keywords``  |
        | other        | do not wake                                            |

        Messages whose ``channel_id`` is currently in human-handoff state are
        never woken regardless of channel_type (the human operator is in
        charge until the business backend calls ``release_handoff``).
        """
        # never reply to ourselves
        if from_uid == self.bot_uid:
            return False

        if self._is_channel_in_handoff(channel_id):
            logger.debug(
                f"[WuKongIM] Channel {channel_id} is in handoff — bot stays quiet",
            )
            return False

        if channel_type == WK_CHANNEL_TYPE_CUSTOMER_SERVICE:
            return True

        if channel_type in (WK_CHANNEL_TYPE_PERSON, WK_CHANNEL_TYPE_GROUP):
            return self._has_wake_signal(content_text)

        # community / unknown types: do not wake by default
        return False

    def _has_wake_signal(self, content_text: str) -> bool:
        """Return True if the text mentions the bot or matches a wake keyword."""
        if not content_text:
            return False

        # Explicit @ mention. Match both ``@<bot_uid>`` and ``@<bot_name>``
        # (e.g. ``@客服``) since front-ends may render mentions either way.
        if self.bot_uid and f"@{self.bot_uid}" in content_text:
            return True
        if self.bot_name and f"@{self.bot_name}" in content_text:
            return True

        # Configured keywords (case-insensitive contains).
        lower = content_text.lower()
        for kw in self.wake_keywords:
            if not kw:
                continue
            if kw.lower() in lower:
                return True
        return False

    # ------------------------------------------------------ v1.1 handoff API

    def _is_channel_in_handoff(self, channel_id: str) -> bool:
        ts = self._handoff_channels.get(channel_id)
        if ts is None:
            return False
        if (time.time() - ts) > self.handoff_ttl_seconds:
            # TTL expired — auto-resume bot
            self._handoff_channels.pop(channel_id, None)
            return False
        return True

    def mark_handoff(self, channel_id: str) -> None:
        """Mark a channel as taken over by a human operator.

        After this, inbound messages on that channel are dropped by the wake
        gate until either ``release_handoff`` is called OR the TTL expires.
        """
        if channel_id:
            self._handoff_channels[channel_id] = time.time()

    def release_handoff(self, channel_id: str) -> None:
        """Resume bot responses on ``channel_id``."""
        self._handoff_channels.pop(channel_id, None)

    async def request_handoff(
        self,
        *,
        channel_id: str,
        channel_type: int,
        reason: str,
        urgency: str = "medium",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """POST a handoff request to the business backend.

        Designed to be called from an LLM tool ``request_human_support`` so
        the model can hand off when it judges itself out of depth. The
        contract mirrors tgo's ``handoff.py`` tool — the business backend
        is the source of truth for ticket queueing and staff assignment.

        Returns True on HTTP 2xx from the backend.
        """
        if not self.handoff_endpoint:
            logger.warning(
                "[WuKongIM] request_handoff called but handoff_endpoint is unset",
            )
            return False
        if self._http_session is None:
            logger.error(
                "[WuKongIM] HTTP session not initialised; call run() first",
            )
            return False

        body = {
            "platform_id": self.platform_id,
            "channel_id": channel_id,
            "channel_type": channel_type,
            "bot_uid": self.bot_uid,
            "reason": reason,
            "urgency": urgency,
            "metadata": metadata or {},
            "timestamp": int(time.time()),
        }
        headers = {"Content-Type": "application/json"}
        if self.handoff_token:
            headers["Authorization"] = f"Bearer {self.handoff_token}"

        try:
            async with self._http_session.post(
                self.handoff_endpoint,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        f"[WuKongIM] handoff HTTP {resp.status}: {resp_text}",
                    )
                    return False
                # Locally mark the channel so we stop responding until the
                # backend explicitly releases it.
                self.mark_handoff(channel_id)
                logger.info(
                    f"[WuKongIM] Handoff accepted by backend for "
                    f"channel_id={channel_id} reason={reason!r}",
                )
                return True
        except Exception as e:
            logger.error(f"[WuKongIM] request_handoff raise: {e}", exc_info=True)
            return False
