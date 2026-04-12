from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()

"""
ARQS Discord Adapter (DM-only v1)

What this bot does:
- Runs as a headless Discord DM bridge for ARQS.
- Auto-registers an ARQS identity on first run and reuses it on later runs.
- Uses one hidden ARQS endpoint per linked contact.
- Supports link management through Discord slash commands.
- Routes normal DM messages to the active contact, or to the replied-to contact.
- Long-polls ARQS continuously and forwards inbound messages into Discord DMs.
- Keeps destructive identity deletion behind a CLI flag only.

Install:
    pip install -U discord.py

Environment:
    DISCORD_BOT_TOKEN=...

Config file example (JSON):
{
  "base_url": "http://127.0.0.1:8000",
  "node_name": "discord-adapter",
  "state_dir": "~/.arqs_discord_adapter",
  "poll_wait_seconds": 20,
  "poll_limit": 100,
  "sync_commands_on_start": false,
  "log_level": "INFO"
}

Run:
    python discord_adapter.py --config ~/.arqs_discord_adapter/config.json
    python discord_adapter.py --config ~/.arqs_discord_adapter/config.json --sync-commands
    python discord_adapter.py --config ~/.arqs_discord_adapter/config.json --delete-identity
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from arqs_api import ARQSClient, ARQSError, ARQSHTTPError, Link, LinkCode, NodeIdentity


LinkMode = Literal["bidirectional", "a_to_b", "b_to_a"]


DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:8000",
    "node_name": "discord-adapter",
    "state_dir": "~/.arqs_discord_adapter",
    "poll_wait_seconds": 20,
    "poll_limit": 100,
    "sync_commands_on_start": False,
    "log_level": "INFO",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def format_expiry_for_display(expires_at: datetime) -> str:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)

    local_dt = expires_at.astimezone()
    remaining_seconds = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    remaining_minutes = (remaining_seconds + 59) // 60

    if remaining_minutes == 0:
        return f"Expired at {local_dt.strftime('%I:%M %p %Z')}"

    minute_label = "minute" if remaining_minutes == 1 else "minutes"
    return f"Expires in {remaining_minutes} {minute_label} at {local_dt.strftime('%I:%M %p %Z')}"
    minute_label = "minute" if remaining_minutes == 1 else "minutes"
    return f"Expires in {remaining_minutes} {minute_label} at {local_dt.strftime('%I:%M %p %Z')}"

def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def json_load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


@dataclass
class AdapterConfig:
    base_url: str
    node_name: str
    state_dir: Path
    poll_wait_seconds: int = 20
    poll_limit: int = 100
    sync_commands_on_start: bool = False
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: Path) -> "AdapterConfig":
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            json_dump(path, DEFAULT_CONFIG)
            raise SystemExit(
                f"Config file did not exist. A template was created at {path}. "
                "Edit it and run again."
            )

        raw = json_load(path, DEFAULT_CONFIG.copy())
        merged = DEFAULT_CONFIG.copy()
        merged.update(raw)

        state_dir = Path(os.path.expanduser(str(merged["state_dir"]))).resolve()
        return cls(
            base_url=str(merged["base_url"]).rstrip("/"),
            node_name=str(merged.get("node_name") or "discord-adapter"),
            state_dir=state_dir,
            poll_wait_seconds=max(0, min(60, int(merged.get("poll_wait_seconds", 20)))),
            poll_limit=max(1, min(1000, int(merged.get("poll_limit", 100)))),
            sync_commands_on_start=bool(merged.get("sync_commands_on_start", False)),
            log_level=str(merged.get("log_level", "INFO")).upper(),
        )


@dataclass
class Binding:
    binding_id: str
    discord_user_id: str
    local_endpoint_id: str
    remote_endpoint_id: str
    link_id: str
    label: str
    created_at: str
    updated_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Binding":
        return cls(
            binding_id=str(data["binding_id"]),
            discord_user_id=str(data["discord_user_id"]),
            local_endpoint_id=str(data["local_endpoint_id"]),
            remote_endpoint_id=str(data["remote_endpoint_id"]),
            link_id=str(data["link_id"]),
            label=str(data["label"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )


@dataclass
class PendingLink:
    pending_id: str
    discord_user_id: str
    local_endpoint_id: str
    code: str
    requested_mode: str
    label: str
    created_at: str
    expires_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingLink":
        return cls(
            pending_id=str(data["pending_id"]),
            discord_user_id=str(data["discord_user_id"]),
            local_endpoint_id=str(data["local_endpoint_id"]),
            code=str(data["code"]),
            requested_mode=str(data["requested_mode"]),
            label=str(data["label"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
        )


@dataclass
class AdapterState:
    bindings: list[Binding] = field(default_factory=list)
    pending_links: list[PendingLink] = field(default_factory=list)
    active_contacts: dict[str, str] = field(default_factory=dict)  # discord_user_id -> binding_id
    seen_deliveries: set[str] = field(default_factory=set)
    reply_index: dict[str, dict[str, str]] = field(default_factory=dict)  # discord_message_id -> {discord_user_id, binding_id}

    def to_dict(self) -> dict[str, Any]:
        return {
            "bindings": [asdict(item) for item in self.bindings],
            "pending_links": [asdict(item) for item in self.pending_links],
            "active_contacts": dict(self.active_contacts),
            "seen_deliveries": sorted(self.seen_deliveries),
            "reply_index": dict(self.reply_index),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdapterState":
        return cls(
            bindings=[Binding.from_dict(item) for item in data.get("bindings", [])],
            pending_links=[PendingLink.from_dict(item) for item in data.get("pending_links", [])],
            active_contacts={str(k): str(v) for k, v in dict(data.get("active_contacts", {})).items()},
            seen_deliveries={str(item) for item in data.get("seen_deliveries", [])},
            reply_index={str(k): {str(kk): str(vv) for kk, vv in dict(v).items()} for k, v in dict(data.get("reply_index", {})).items()},
        )


class RuntimeStore:
    def __init__(self, config: AdapterConfig) -> None:
        self.config = config
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self.identity_path = self.config.state_dir / "identity.json"
        self.state_path = self.config.state_dir / "state.json"
        self.log = logging.getLogger("arqs.discord.store")

    def load_state(self) -> AdapterState:
        return AdapterState.from_dict(json_load(self.state_path, {}))

    def save_state(self, state: AdapterState) -> None:
        json_dump(self.state_path, state.to_dict())

    def wipe_state(self) -> None:
        self.save_state(AdapterState())

    def ensure_identity(self) -> NodeIdentity:
        if self.identity_path.exists():
            identity = NodeIdentity.load(self.identity_path)
            self.log.info("Loaded existing ARQS identity for node %s", identity.node_id)
            return identity

        client = ARQSClient(self.config.base_url)
        identity = client.register(node_name=self.config.node_name, adopt_identity=False)
        identity.save(self.identity_path)
        self.log.info("Registered new ARQS identity for node %s", identity.node_id)
        return identity

    def load_client(self) -> ARQSClient:
        if self.identity_path.exists():
            return ARQSClient.from_identity_file(self.config.base_url, self.identity_path)
        return ARQSClient(self.config.base_url)

    def delete_identity_file(self) -> None:
        try:
            self.identity_path.unlink()
        except FileNotFoundError:
            return


class DMOnlyDeleteLinkView(discord.ui.View):
    def __init__(self, bot: "ARQSDiscordBot", user_id: int, binding_id: str) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = int(user_id)
        self.binding_id = binding_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=False)
            return False
        return True

    def _disable_buttons(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_buttons()
        await interaction.response.edit_message(content="Delete link cancelled.", view=self)

    @discord.ui.button(label="Delete link", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_buttons()
        try:
            message = await self.bot.delete_active_binding_for_user(str(self.user_id), expected_binding_id=self.binding_id)
        except Exception as exc:
            await interaction.response.edit_message(content=f"Delete failed: {exc}", view=self)
            return
        await interaction.response.edit_message(content=message, view=self)


class ARQSDiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        config: AdapterConfig,
        store: RuntimeStore,
        sync_commands_on_start: bool,
        delete_identity_mode: bool,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.messages = True
        intents.dm_messages = True

        super().__init__(command_prefix="!", intents=intents)
        self.config_obj = config
        self.store = store
        self.arqs = self.store.load_client()
        self.state = self.store.load_state()
        self.state_lock = asyncio.Lock()
        self.log = logging.getLogger("arqs.discord.bot")
        self.sync_commands_on_start = bool(sync_commands_on_start)
        self.delete_identity_mode = bool(delete_identity_mode)
        self._background_started = False
        self._delete_started = False
        self._poll_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[None] | None = None
        self._register_app_commands()

    def _register_app_commands(self) -> None:
        @app_commands.command(name="request_link_code", description="Create a link code for a new ARQS contact.")
        async def request_link_code(interaction: discord.Interaction) -> None:
            await self.cmd_request_link_code(interaction)

        @app_commands.command(name="redeem_link_code", description="Redeem a link code for a new ARQS contact.")
        @app_commands.describe(code="The ARQS link code to redeem")
        async def redeem_link_code(interaction: discord.Interaction, code: str) -> None:
            await self.cmd_redeem_link_code(interaction, code)

        @app_commands.command(name="links", description="List your linked ARQS contacts.")
        async def links(interaction: discord.Interaction) -> None:
            await self.cmd_links(interaction)

        @app_commands.command(name="use_contact", description="Choose the active contact for normal messages.")
        @app_commands.describe(contact="Contact label or number from /links")
        async def use_contact(interaction: discord.Interaction, contact: str) -> None:
            await self.cmd_use_contact(interaction, contact)

        @app_commands.command(name="current_contact", description="Show your current active contact.")
        async def current_contact(interaction: discord.Interaction) -> None:
            await self.cmd_current_contact(interaction)

        @app_commands.command(name="rename_contact", description="Rename your active contact.")
        @app_commands.describe(new_name="New name for the active contact")
        async def rename_contact(interaction: discord.Interaction, new_name: str) -> None:
            await self.cmd_rename_contact(interaction, new_name)

        @app_commands.command(name="delete_link", description="Delete your active contact link.")
        async def delete_link(interaction: discord.Interaction) -> None:
            await self.cmd_delete_link(interaction)

        for command in (
            request_link_code,
            redeem_link_code,
            links,
            use_contact,
            current_contact,
            rename_contact,
            delete_link,
        ):
            self.tree.add_command(command)

    async def setup_hook(self) -> None:
        if self.sync_commands_on_start:
            self.log.info("Syncing application commands...")
            await self.tree.sync()
            self.log.info("Application commands synced.")

    async def on_ready(self) -> None:
        assert self.user is not None
        self.log.info("Logged in as %s (%s)", self.user.name, self.user.id)
        if self.delete_identity_mode:
            if not self._delete_started:
                self._delete_started = True
                self.loop.create_task(self._delete_identity_and_shutdown())
            return
        if not self._background_started:
            self._background_started = True
            self._poll_task = self.loop.create_task(self._poll_loop())
            self._reconcile_task = self.loop.create_task(self._reconcile_loop())

    async def close(self) -> None:
        for task in (self._poll_task, self._reconcile_task):
            if task is not None:
                task.cancel()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is not None:
            return
        if self.delete_identity_mode:
            return
        if not message.content.strip():
            return

        try:
            binding = await self._resolve_outbound_binding_for_message(message)
        except ValueError as exc:
            await message.channel.send(str(exc))
            return
        except Exception as exc:
            await message.channel.send(f"Failed to resolve contact: {exc}")
            return

        if binding is None:
            await message.channel.send(
                "You do not have any linked contacts yet. Use /request_link_code or /redeem_link_code first."
            )
            return

        meta = {
            "adapter": "discord_dm",
            "discord_user_id": str(message.author.id),
            "discord_user": str(message.author),
            "discord_message_id": str(message.id),
        }
        if message.reference and message.reference.message_id:
            meta["discord_reply_to_message_id"] = str(message.reference.message_id)

        try:
            result = await asyncio.to_thread(
                self.arqs.send_packet,
                from_endpoint_id=binding.local_endpoint_id,
                to_endpoint_id=binding.remote_endpoint_id,
                body=message.content,
                data=None,
                headers={"content_type": "text/plain"},
                meta=meta,
            )
        except Exception as exc:
            await message.channel.send(f"Send failed: {exc}")
            return

        async with self.state_lock:
            for item in self.state.bindings:
                if item.binding_id == binding.binding_id:
                    item.updated_at = utc_now_iso()
                    break
            self.state.active_contacts[str(message.author.id)] = binding.binding_id
            self.store.save_state(self.state)

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        self.log.info(
            "Forwarded Discord DM %s from user %s to ARQS contact %s (%s)",
            message.id,
            message.author.id,
            binding.label,
            result.result,
        )

    async def _resolve_outbound_binding_for_message(self, message: discord.Message) -> Binding | None:
        user_id = str(message.author.id)
        async with self.state_lock:
            bindings = [item for item in self.state.bindings if item.discord_user_id == user_id]
            if not bindings:
                return None

            if message.reference and message.reference.message_id:
                reply_info = self.state.reply_index.get(str(message.reference.message_id))
                if reply_info and reply_info.get("discord_user_id") == user_id:
                    binding_id = reply_info.get("binding_id")
                    for item in bindings:
                        if item.binding_id == binding_id:
                            return item

            if len(bindings) == 1:
                return bindings[0]

            active_binding_id = self.state.active_contacts.get(user_id)
            if active_binding_id:
                for item in bindings:
                    if item.binding_id == active_binding_id:
                        return item

        raise ValueError(
            "You have multiple linked contacts. Use /use_contact first, or reply directly to one of my forwarded messages."
        )

    async def _poll_loop(self) -> None:
        while not self.is_closed():
            try:
                deliveries = await asyncio.to_thread(
                    self.arqs.poll_inbox,
                    wait=self.config_obj.poll_wait_seconds,
                    limit=self.config_obj.poll_limit,
                    request_timeout=self.config_obj.poll_wait_seconds + 10,
                )
                for delivery in deliveries:
                    await self._handle_delivery(delivery)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.exception("Poll loop error: %s", exc)
                await asyncio.sleep(3)

    async def _reconcile_loop(self) -> None:
        while not self.is_closed():
            try:
                await self._reconcile_links()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.exception("Reconcile loop error: %s", exc)
            await asyncio.sleep(30)

    async def _handle_delivery(self, delivery: Any) -> None:
        delivery_id = str(delivery.delivery_id)
        packet = delivery.packet
        local_endpoint_id = str(packet.to_endpoint_id)

        async with self.state_lock:
            if delivery_id in self.state.seen_deliveries:
                seen = True
            else:
                seen = False
                self.state.seen_deliveries.add(delivery_id)
                self.store.save_state(self.state)

            binding = next((item for item in self.state.bindings if item.local_endpoint_id == local_endpoint_id), None)

        if seen:
            try:
                await asyncio.to_thread(self.arqs.ack_delivery, delivery_id, status="handled")
            except Exception:
                pass
            return

        if binding is None:
            self.log.warning("Received delivery for unknown local endpoint %s; ACKing to avoid endless redelivery.", local_endpoint_id)
            try:
                await asyncio.to_thread(self.arqs.ack_delivery, delivery_id, status="handled")
            except Exception as exc:
                self.log.warning("ACK failed for unknown delivery %s: %s", delivery_id, exc)
            return

        content = str(packet.body or "")
        if not content and packet.data:
            content = json.dumps(packet.data, ensure_ascii=False, indent=2)
        if not content:
            content = "[empty message]"

        forwarded = f"[{binding.label}] {content}"
        forwarded_chunks = self._split_message(forwarded)

        try:
            user = await self.fetch_user(int(binding.discord_user_id))
            sent_messages: list[discord.Message] = []
            for chunk in forwarded_chunks:
                sent = await user.send(chunk)
                sent_messages.append(sent)
        except Exception as exc:
            self.log.exception("Failed to forward delivery %s to Discord user %s: %s", delivery_id, binding.discord_user_id, exc)
            return

        async with self.state_lock:
            for sent in sent_messages:
                self.state.reply_index[str(sent.id)] = {
                    "discord_user_id": binding.discord_user_id,
                    "binding_id": binding.binding_id,
                }
            self.state.active_contacts[binding.discord_user_id] = binding.binding_id
            self.store.save_state(self.state)

        try:
            await asyncio.to_thread(self.arqs.ack_delivery, delivery_id, status="handled")
        except Exception as exc:
            self.log.warning("ACK failed after Discord forward for delivery %s: %s", delivery_id, exc)
            return

        self.log.info("Forwarded delivery %s to Discord user %s via label %s", delivery_id, binding.discord_user_id, binding.label)

    async def _reconcile_links(self) -> None:
        server_links = await asyncio.to_thread(self.arqs.list_links)
        active_server_links = [item for item in server_links if item.status == "active"]

        async with self.state_lock:
            local_endpoint_ids = {
                item.local_endpoint_id for item in self.state.bindings
            } | {
                item.local_endpoint_id for item in self.state.pending_links
            }

            active_by_local_endpoint: dict[str, Link] = {}
            active_link_ids: set[str] = set()
            for link in active_server_links:
                a = str(link.endpoint_a_id)
                b = str(link.endpoint_b_id)
                if a in local_endpoint_ids:
                    active_by_local_endpoint[a] = link
                    active_link_ids.add(str(link.link_id))
                if b in local_endpoint_ids:
                    active_by_local_endpoint[b] = link
                    active_link_ids.add(str(link.link_id))

            newly_activated: list[Binding] = []
            remaining_pending: list[PendingLink] = []
            for pending in self.state.pending_links:
                link = active_by_local_endpoint.get(pending.local_endpoint_id)
                if link is None:
                    expiry = parse_iso(pending.expires_at)
                    if expiry is not None and expiry <= datetime.now(timezone.utc):
                        self.log.info("Dropping expired pending link code %s for user %s", pending.code, pending.discord_user_id)
                    else:
                        remaining_pending.append(pending)
                    continue

                if str(link.endpoint_a_id) == pending.local_endpoint_id:
                    remote_endpoint_id = str(link.endpoint_b_id)
                else:
                    remote_endpoint_id = str(link.endpoint_a_id)

                binding = Binding(
                    binding_id=str(uuid.uuid4()),
                    discord_user_id=pending.discord_user_id,
                    local_endpoint_id=pending.local_endpoint_id,
                    remote_endpoint_id=remote_endpoint_id,
                    link_id=str(link.link_id),
                    label=pending.label,
                    created_at=utc_now_iso(),
                    updated_at=utc_now_iso(),
                )
                self.state.bindings.append(binding)
                newly_activated.append(binding)
                self.state.active_contacts[pending.discord_user_id] = binding.binding_id

            self.state.pending_links = remaining_pending

            severed_bindings: list[Binding] = []
            kept_bindings: list[Binding] = []
            for binding in self.state.bindings:
                if binding.link_id not in active_link_ids:
                    severed_bindings.append(binding)
                else:
                    kept_bindings.append(binding)
            self.state.bindings = kept_bindings

            for binding in severed_bindings:
                user_bindings = [item for item in self.state.bindings if item.discord_user_id == binding.discord_user_id]
                current_active = self.state.active_contacts.get(binding.discord_user_id)
                if current_active == binding.binding_id:
                    if len(user_bindings) == 1:
                        self.state.active_contacts[binding.discord_user_id] = user_bindings[0].binding_id
                    elif len(user_bindings) == 0:
                        self.state.active_contacts.pop(binding.discord_user_id, None)
                    else:
                        self.state.active_contacts.pop(binding.discord_user_id, None)

            self.store.save_state(self.state)

        for binding in newly_activated:
            await self._notify_link_activated(binding)
            await self._maybe_send_second_contact_explainer(binding.discord_user_id)

        for binding in severed_bindings:
            await self._notify_link_severed(binding)

    async def _notify_link_activated(self, binding: Binding) -> None:
        try:
            user = await self.fetch_user(int(binding.discord_user_id))
            await user.send(
                f"A new ARQS link is now active as **{binding.label}**. "
                f"Use /current_contact to see your active contact, /use_contact to switch, and /rename_contact to rename the active contact."
            )
        except Exception as exc:
            self.log.warning("Failed to notify user %s about new link: %s", binding.discord_user_id, exc)

    async def _notify_link_severed(self, binding: Binding) -> None:
        try:
            user = await self.fetch_user(int(binding.discord_user_id))
            await user.send(
                f"Your ARQS link **{binding.label}** was severed. "
                f"{self.user.name if self.user else 'This bot'} will not deliver messages for that contact until you create a new link."
            )
        except Exception as exc:
            self.log.warning("Failed to notify user %s about severed link: %s", binding.discord_user_id, exc)

    async def _maybe_send_second_contact_explainer(self, discord_user_id: str) -> None:
        async with self.state_lock:
            count = sum(1 for item in self.state.bindings if item.discord_user_id == discord_user_id)
        if count != 2:
            return
        try:
            user = await self.fetch_user(int(discord_user_id))
            await user.send(
                "You now have more than one linked contact.\n\n"
                "How sending works:\n"
                "- Reply to one of my forwarded messages to answer that contact directly.\n"
                "- Or use /use_contact to choose the active contact for normal messages.\n\n"
                "Useful commands:\n"
                "- /links\n"
                "- /use_contact\n"
                "- /current_contact"
            )
        except Exception as exc:
            self.log.warning("Failed to send second-contact explainer to user %s: %s", discord_user_id, exc)

    def _split_message(self, content: str, limit: int = 1900) -> list[str]:
        if len(content) <= limit:
            return [content]
        parts: list[str] = []
        remaining = content
        while remaining:
            if len(remaining) <= limit:
                parts.append(remaining)
                break
            chunk = remaining[:limit]
            split_at = chunk.rfind("\n")
            if split_at < 200:
                split_at = chunk.rfind(" ")
            if split_at < 200:
                split_at = limit
            parts.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        return parts

    async def _delete_identity_and_shutdown(self) -> None:
        async with self.state_lock:
            bindings = list(self.state.bindings)

        notified_users: set[str] = set()
        for binding in bindings:
            if binding.discord_user_id in notified_users:
                continue
            notified_users.add(binding.discord_user_id)
            try:
                user = await self.fetch_user(int(binding.discord_user_id))
                await user.send(
                    f"Your ARQS link(s) through {self.user.name if self.user else 'this bot'} were severed. "
                    "You will not receive messages here again until you link again."
                )
            except Exception as exc:
                self.log.warning("Failed to send identity-deletion notice to user %s: %s", binding.discord_user_id, exc)

        try:
            if self.store.identity_path.exists():
                await asyncio.to_thread(self.arqs.delete_identity)
                self.log.info("Deleted ARQS identity from server.")
        except Exception as exc:
            self.log.warning("Server-side identity deletion failed: %s", exc)

        async with self.state_lock:
            self.state = AdapterState()
            self.store.wipe_state()
        self.store.delete_identity_file()
        self.log.info("Local state wiped. Shutting down.")
        await self.close()

    def _dm_only_check(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is None

    async def _send_dm_only_error(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            await interaction.followup.send("This adapter only supports commands in DMs.")
        else:
            await interaction.response.send_message("This adapter only supports commands in DMs.")

    def _label_exists_for_user(self, discord_user_id: str, label: str, *, exclude_binding_id: str | None = None) -> bool:
        target = label.strip().casefold()
        for item in self.state.bindings:
            if item.discord_user_id != discord_user_id:
                continue
            if exclude_binding_id and item.binding_id == exclude_binding_id:
                continue
            if item.label.strip().casefold() == target:
                return True
        for item in self.state.pending_links:
            if item.discord_user_id != discord_user_id:
                continue
            if item.label.strip().casefold() == target:
                return True
        return False

    def _next_default_label(self, discord_user_id: str) -> str:
        existing = {
            item.label.strip().casefold()
            for item in self.state.bindings
            if item.discord_user_id == discord_user_id
        } | {
            item.label.strip().casefold()
            for item in self.state.pending_links
            if item.discord_user_id == discord_user_id
        }
        n = 1
        while True:
            candidate = f"Contact {n}"
            if candidate.casefold() not in existing:
                return candidate
            n += 1

    def _find_binding_by_label_or_index(self, discord_user_id: str, query: str) -> Binding | None:
        bindings = [item for item in self.state.bindings if item.discord_user_id == discord_user_id]
        if not bindings:
            return None
        ordered = sorted(bindings, key=lambda item: item.created_at)
        q = query.strip()
        if q.isdigit():
            idx = int(q)
            if 1 <= idx <= len(ordered):
                return ordered[idx - 1]
        target = q.casefold()
        for item in ordered:
            if item.label.casefold() == target:
                return item
        for item in ordered:
            if item.label.casefold().startswith(target):
                return item
        return None

    def _resolve_remote_endpoint(self, link: Link, local_endpoint_id: str) -> str:
        if str(link.endpoint_a_id) == local_endpoint_id:
            return str(link.endpoint_b_id)
        return str(link.endpoint_a_id)

    async def _create_hidden_endpoint(self, discord_user_id: str) -> str:
        endpoint_name = f"discord:dm:{discord_user_id}:{uuid.uuid4().hex[:8]}"
        endpoint = await asyncio.to_thread(
            self.arqs.create_endpoint,
            endpoint_name=endpoint_name,
            kind="discord_dm",
            meta={"discord_user_id": discord_user_id, "scope": "dm"},
        )
        return str(endpoint.endpoint_id)

    async def _ensure_active_binding_valid(self, discord_user_id: str) -> None:
        async with self.state_lock:
            binding_ids = {item.binding_id for item in self.state.bindings if item.discord_user_id == discord_user_id}
            active = self.state.active_contacts.get(discord_user_id)
            if active in binding_ids:
                return
            user_bindings = [item for item in self.state.bindings if item.discord_user_id == discord_user_id]
            if len(user_bindings) == 1:
                self.state.active_contacts[discord_user_id] = user_bindings[0].binding_id
            else:
                self.state.active_contacts.pop(discord_user_id, None)
            self.store.save_state(self.state)

    async def delete_active_binding_for_user(self, discord_user_id: str, *, expected_binding_id: str | None = None) -> str:
        async with self.state_lock:
            active_id = self.state.active_contacts.get(discord_user_id)
            if active_id is None:
                raise ValueError("No active contact is selected.")
            binding = next((item for item in self.state.bindings if item.binding_id == active_id and item.discord_user_id == discord_user_id), None)
            if binding is None:
                raise ValueError("The active contact is no longer valid.")
            if expected_binding_id is not None and binding.binding_id != expected_binding_id:
                raise ValueError("The active contact changed before deletion was confirmed.")

        revoke_error: str | None = None
        endpoint_delete_error: str | None = None
        try:
            await asyncio.to_thread(self.arqs.revoke_link, binding.link_id)
        except Exception as exc:
            revoke_error = str(exc)
        try:
            await asyncio.to_thread(self.arqs.delete_endpoint, binding.local_endpoint_id)
        except Exception as exc:
            endpoint_delete_error = str(exc)

        async with self.state_lock:
            self.state.bindings = [item for item in self.state.bindings if item.binding_id != binding.binding_id]
            # clear reply routes pointing at this binding
            self.state.reply_index = {
                msg_id: data for msg_id, data in self.state.reply_index.items()
                if data.get("binding_id") != binding.binding_id
            }
            remaining = [item for item in self.state.bindings if item.discord_user_id == discord_user_id]
            if len(remaining) == 1:
                self.state.active_contacts[discord_user_id] = remaining[0].binding_id
            elif len(remaining) == 0:
                self.state.active_contacts.pop(discord_user_id, None)
            else:
                self.state.active_contacts.pop(discord_user_id, None)
            self.store.save_state(self.state)

        if revoke_error or endpoint_delete_error:
            bits: list[str] = [
                f"Deleted local contact **{binding.label}**."
            ]
            if revoke_error:
                bits.append(f"Server-side link revoke failed: {revoke_error}")
            if endpoint_delete_error:
                bits.append(f"Hidden endpoint cleanup failed: {endpoint_delete_error}")
            return " ".join(bits)

        return (
            f"Deleted link **{binding.label}**. This cannot be undone. "
            f"{self.user.name if self.user else 'This bot'} will not deliver messages for that contact again until you create a new link."
        )

    async def cmd_request_link_code(self, interaction: discord.Interaction) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        await interaction.response.defer(thinking=True)
        discord_user_id = str(interaction.user.id)
        mode = "bidirectional"
        try:
            local_endpoint_id = await self._create_hidden_endpoint(discord_user_id)
        except Exception as exc:
            await interaction.followup.send(f"Failed to create hidden endpoint: {exc}")
            return

        try:
            link_code = await asyncio.to_thread(self.arqs.request_link_code, local_endpoint_id, requested_mode=mode)
        except Exception as exc:
            try:
                await asyncio.to_thread(self.arqs.delete_endpoint, local_endpoint_id)
            except Exception:
                pass
            await interaction.followup.send(f"Failed to request link code: {exc}")
            return

        async with self.state_lock:
            label = self._next_default_label(discord_user_id)
            pending = PendingLink(
                pending_id=str(uuid.uuid4()),
                discord_user_id=discord_user_id,
                local_endpoint_id=local_endpoint_id,
                code=str(link_code.code),
                requested_mode=str(link_code.requested_mode),
                label=label,
                created_at=utc_now_iso(),
                expires_at=link_code.expires_at.isoformat(),
            )
            self.state.pending_links.append(pending)
            self.store.save_state(self.state)

        await interaction.followup.send(
            f"Link code created for a new hidden contact slot (**{label}**).\n\n"
            f"Code: `{link_code.code}`\n"
            f"Mode: `{link_code.requested_mode}`\n"
            f"{format_expiry_for_display(link_code.expires_at)}\n\n"
            "Share that code with the other ARQS user. When the link becomes active, I will DM you."
        )

    async def cmd_redeem_link_code(self, interaction: discord.Interaction, code: str) -> None:  
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        await interaction.response.defer(thinking=True)
        discord_user_id = str(interaction.user.id)
        code = code.strip().upper()

        try:
            local_endpoint_id = await self._create_hidden_endpoint(discord_user_id)
        except Exception as exc:
            await interaction.followup.send(f"Failed to create hidden endpoint: {exc}")
            return

        try:
            link = await asyncio.to_thread(self.arqs.redeem_link_code, code, local_endpoint_id)
        except Exception as exc:
            try:
                await asyncio.to_thread(self.arqs.delete_endpoint, local_endpoint_id)
            except Exception:
                pass
            await interaction.followup.send(f"Failed to redeem link code: {exc}")
            return

        remote_endpoint_id = self._resolve_remote_endpoint(link, local_endpoint_id)

        async with self.state_lock:
            label = self._next_default_label(discord_user_id)
            binding = Binding(
                binding_id=str(uuid.uuid4()),
                discord_user_id=discord_user_id,
                local_endpoint_id=local_endpoint_id,
                remote_endpoint_id=remote_endpoint_id,
                link_id=str(link.link_id),
                label=label,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
            )
            self.state.bindings.append(binding)
            user_bindings = [item for item in self.state.bindings if item.discord_user_id == discord_user_id]
            if discord_user_id not in self.state.active_contacts:
                self.state.active_contacts[discord_user_id] = binding.binding_id
            self.store.save_state(self.state)
            new_count = len(user_bindings)

        await interaction.followup.send(
            f"Link redeemed successfully as **{binding.label}**. "
            f"Use /rename_contact to rename the active contact if you want."
        )

        if new_count == 2:
            await self._maybe_send_second_contact_explainer(discord_user_id)

    async def cmd_links(self, interaction: discord.Interaction) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        discord_user_id = str(interaction.user.id)
        async with self.state_lock:
            bindings = [item for item in self.state.bindings if item.discord_user_id == discord_user_id]
            active_binding_id = self.state.active_contacts.get(discord_user_id)
        if not bindings:
            await interaction.response.send_message("You do not have any linked contacts yet.")
            return

        ordered = sorted(bindings, key=lambda item: item.created_at)
        lines = ["Linked contacts:"]
        for idx, item in enumerate(ordered, start=1):
            marker = "*" if item.binding_id == active_binding_id else " "
            lines.append(f"{marker} {idx}. {item.label}")
        lines.append("\n`*` = active contact")
        await interaction.response.send_message("\n".join(lines))

    async def cmd_use_contact(self, interaction: discord.Interaction, contact: str) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        discord_user_id = str(interaction.user.id)
        async with self.state_lock:
            binding = self._find_binding_by_label_or_index(discord_user_id, contact)
            if binding is None:
                await interaction.response.send_message("Contact not found. Use /links to see valid choices.")
                return
            self.state.active_contacts[discord_user_id] = binding.binding_id
            self.store.save_state(self.state)
        await interaction.response.send_message(f"Active contact set to **{binding.label}**.")

    async def cmd_current_contact(self, interaction: discord.Interaction) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        discord_user_id = str(interaction.user.id)
        async with self.state_lock:
            binding_id = self.state.active_contacts.get(discord_user_id)
            binding = next((item for item in self.state.bindings if item.binding_id == binding_id), None)
        if binding is None:
            await interaction.response.send_message("No active contact is selected.")
            return
        await interaction.response.send_message(f"Current active contact: **{binding.label}**")

    async def cmd_rename_contact(self, interaction: discord.Interaction, new_name: str) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        discord_user_id = str(interaction.user.id)
        new_name = new_name.strip()
        if not new_name:
            await interaction.response.send_message("New name cannot be empty.")
            return

        async with self.state_lock:
            active_binding_id = self.state.active_contacts.get(discord_user_id)
            if not active_binding_id:
                await interaction.response.send_message("No active contact is selected.")
                return
            binding = next((item for item in self.state.bindings if item.binding_id == active_binding_id), None)
            if binding is None:
                await interaction.response.send_message("The active contact is no longer valid.")
                return
            if self._label_exists_for_user(discord_user_id, new_name, exclude_binding_id=binding.binding_id):
                await interaction.response.send_message("You already have a contact with that name.")
                return
            old = binding.label
            binding.label = new_name
            binding.updated_at = utc_now_iso()
            self.store.save_state(self.state)
        await interaction.response.send_message(f"Renamed **{old}** to **{new_name}**.")

    async def cmd_delete_link(self, interaction: discord.Interaction) -> None:
        if not self._dm_only_check(interaction):
            await self._send_dm_only_error(interaction)
            return

        discord_user_id = str(interaction.user.id)
        async with self.state_lock:
            active_binding_id = self.state.active_contacts.get(discord_user_id)
            if active_binding_id is None:
                await interaction.response.send_message("No active contact is selected.")
                return
            binding = next((item for item in self.state.bindings if item.binding_id == active_binding_id), None)
            if binding is None:
                await interaction.response.send_message("The active contact is no longer valid.")
                return

        warning = (
            f"Warning: this will delete the link **{binding.label}**. This cannot be undone. "
            f"To use this contact again, a new link will have to be made. "
            f"{self.user.name if self.user else 'This bot'} will not deliver messages for you again for that contact until you create a new link."
        )
        view = DMOnlyDeleteLinkView(self, interaction.user.id, binding.binding_id)
        await interaction.response.send_message(warning, view=view)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARQS Discord Adapter (DM-only v1)")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".arqs_discord_adapter" / "config.json",
        help="Path to the adapter JSON config file.",
    )
    parser.add_argument(
        "--sync-commands",
        action="store_true",
        help="Sync Discord application commands on startup.",
    )
    parser.add_argument(
        "--delete-identity",
        action="store_true",
        help="Delete the ARQS identity, notify linked Discord users, wipe local state, then exit.",
    )
    return parser


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    config = AdapterConfig.load(args.config)
    configure_logging(config.log_level)
    log = logging.getLogger("arqs.discord.main")

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required in the environment.")

    store = RuntimeStore(config)
    if args.delete_identity:
        if not store.identity_path.exists():
            raise SystemExit("No saved identity exists, so there is nothing to delete.")
    else:
        store.ensure_identity()

    bot = ARQSDiscordBot(
        config=config,
        store=store,
        sync_commands_on_start=args.sync_commands or config.sync_commands_on_start,
        delete_identity_mode=args.delete_identity,
    )

    try:
        bot.run(token, log_handler=None)
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    main()
