from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from arqs_api import ARQSClient, ARQSError, ARQSHTTPError, Endpoint, Link, LinkCode

APP_NAME = "ARQS Messages GUI"
APP_DIR = Path.home() / ".arqs_messages_gui"
IDENTITY_PATH = APP_DIR / "identity.json"
CONFIG_PATH = APP_DIR / "config.json"
LINKS_PATH = APP_DIR / "links.json"
MESSAGES_PATH = APP_DIR / "messages.jsonl"
SEEN_DELIVERIES_PATH = APP_DIR / "seen_deliveries.json"
PENDING_CODES_PATH = APP_DIR / "pending_link_codes.json"
LOCAL_LINK_CODE_TTL_SECONDS = 15 * 60

DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:8000",
    "node_name": "",
    "active_polling": False,
    "poll_wait_seconds": 20,
    "poll_limit": 100,
    "local_endpoint_aliases": {},
    "window_geometry": "1180x760",
    "last_selected_conversation": None,
}


@dataclass
class Conversation:
    key: str
    local_endpoint_id: str
    remote_endpoint_id: str
    title: str
    subtitle: str
    last_timestamp: str


class JsonStore:
    @staticmethod
    def load_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            return default

    @staticmethod
    def save_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        APP_DIR.mkdir(parents=True, exist_ok=True)

        self.config: dict[str, Any] = JsonStore.load_json(CONFIG_PATH, DEFAULT_CONFIG.copy())
        merged = DEFAULT_CONFIG.copy()
        merged.update(self.config)
        self.config = merged
        self.root.geometry(str(self.config.get("window_geometry", DEFAULT_CONFIG["window_geometry"])))

        self.links: list[dict[str, Any]] = JsonStore.load_json(LINKS_PATH, [])
        self.pending_codes: list[dict[str, Any]] = JsonStore.load_json(PENDING_CODES_PATH, [])
        self._prune_pending_codes()
        self.seen_deliveries: set[str] = set(JsonStore.load_json(SEEN_DELIVERIES_PATH, []))
        self.message_index: set[str] = set()
        self.messages: list[dict[str, Any]] = []
        self._load_messages()

        self.client: ARQSClient | None = None
        self.endpoints: list[Endpoint] = []
        self.endpoint_map: dict[str, Endpoint] = {}
        self.conversations: list[Conversation] = []
        self.selected_conversation_key: str | None = self.config.get("last_selected_conversation")

        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.poll_stop = threading.Event()
        self.poll_thread: threading.Thread | None = None
        self.busy_count = 0

        self._build_ui()
        self._refresh_client_from_disk()
        self._refresh_conversations()
        self._restore_last_selection()
        self._set_polling_ui(bool(self.config.get("active_polling", False)))
        if self.client is not None:
            self.refresh_everything(background=True)
        if bool(self.config.get("active_polling", False)):
            self._start_poll_thread()
        self.root.after(100, self._process_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # --------------------------
    # UI construction
    # --------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, sticky="nsew")
        for col in range(10):
            top.columnconfigure(col, weight=0)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Server URL").grid(row=0, column=0, sticky="w")
        self.base_url_var = tk.StringVar(value=str(self.config.get("base_url", DEFAULT_CONFIG["base_url"])))
        ttk.Entry(top, textvariable=self.base_url_var).grid(row=0, column=1, sticky="ew", padx=(6, 10))

        ttk.Button(top, text="Load Identity", command=self.load_identity).grid(row=0, column=2, padx=2)
        ttk.Button(top, text="Register Node", command=self.register_node).grid(row=0, column=3, padx=2)
        ttk.Button(top, text="Create Endpoint", command=self.create_endpoint).grid(row=0, column=4, padx=2)
        ttk.Button(top, text="Request Link Code", command=self.request_link_code).grid(row=0, column=5, padx=2)
        ttk.Button(top, text="Redeem Link Code", command=self.redeem_link_code).grid(row=0, column=6, padx=2)
        ttk.Button(top, text="Refresh", command=lambda: self.refresh_everything(background=True)).grid(row=0, column=7, padx=2)

        ttk.Label(top, text="Node name").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.node_name_var = tk.StringVar(value=str(self.config.get("node_name", "")))
        ttk.Entry(top, textvariable=self.node_name_var).grid(row=1, column=1, sticky="ew", padx=(6, 10), pady=(8, 0))

        self.active_poll_var = tk.BooleanVar(value=bool(self.config.get("active_polling", False)))
        ttk.Checkbutton(
            top,
            text="Automatically fetch inbox",
            variable=self.active_poll_var,
            command=self.toggle_active_polling,
        ).grid(row=1, column=2, padx=2, pady=(8, 0), sticky="w")
        ttk.Button(top, text="Refresh inbox", command=lambda: self.poll_inbox(background=True, wait=0)).grid(
            row=1, column=3, padx=2, pady=(8, 0)
        )

        ttk.Label(top, text="Poll wait").grid(row=1, column=4, sticky="e", pady=(8, 0))
        self.poll_wait_var = tk.StringVar(value=str(self.config.get("poll_wait_seconds", 20)))
        ttk.Entry(top, textvariable=self.poll_wait_var, width=6).grid(row=1, column=5, sticky="w", pady=(8, 0))
        ttk.Label(top, text="sec").grid(row=1, column=6, sticky="w", pady=(8, 0))

        ttk.Label(top, text="Status").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(top, textvariable=self.status_var).grid(row=2, column=1, columnspan=7, sticky="w", padx=(6, 10), pady=(8, 0))

        body = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(body, padding=8)
        right = ttk.Frame(body, padding=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        body.add(left, weight=1)
        body.add(right, weight=3)

        ttk.Label(left, text="Conversations").grid(row=0, column=0, sticky="w")
        self.conversation_list = tk.Listbox(left, exportselection=False)
        self.conversation_list.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.conversation_list.bind("<<ListboxSelect>>", self.on_conversation_selected)

        convo_scroll = ttk.Scrollbar(left, orient="vertical", command=self.conversation_list.yview)
        convo_scroll.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.conversation_list.configure(yscrollcommand=convo_scroll.set)

        controls = ttk.Frame(left)
        controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)
        controls.columnconfigure(2, weight=1)

        ttk.Button(
            controls,
            text="Rename Contact",
            command=self.rename_contact,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ttk.Button(
            controls,
            text="Delete Link",
            command=self.delete_link,
        ).grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Button(
            controls,
            text="Copy Link Code",
            command=self.copy_selected_pending_code,
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        self.conversation_header_var = tk.StringVar(value="No conversation selected")
        ttk.Label(right, textvariable=self.conversation_header_var, font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, sticky="w")
        self.message_history = ScrolledText(right, wrap=tk.WORD, state="disabled")
        self.message_history.grid(row=1, column=0, sticky="nsew", pady=(6, 8))

        compose = ttk.Frame(right)
        compose.grid(row=2, column=0, sticky="ew")
        compose.columnconfigure(0, weight=1)
        compose.rowconfigure(0, weight=1)

        self.message_entry = tk.Text(compose, height=4, wrap=tk.WORD)
        self.message_entry.grid(row=0, column=0, sticky="ew")
        self.message_entry.bind("<Control-Return>", lambda _event: self.send_message())
        ttk.Button(compose, text="Send", command=self.send_message).grid(row=0, column=1, sticky="ns", padx=(8, 0))

    # --------------------------
    # Persistence helpers
    # --------------------------
    def _load_messages(self) -> None:
        self.messages.clear()
        self.message_index.clear()
        if not MESSAGES_PATH.exists():
            return
        with MESSAGES_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dedupe = item.get("packet_id") or item.get("delivery_id")
                if dedupe:
                    self.message_index.add(str(dedupe))
                self.messages.append(item)

    def _append_message(self, item: dict[str, Any]) -> None:
        dedupe = item.get("packet_id") or item.get("delivery_id")
        if dedupe and str(dedupe) in self.message_index:
            return
        if dedupe:
            self.message_index.add(str(dedupe))
        self.messages.append(item)
        MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MESSAGES_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _save_config(self) -> None:
        self.config["base_url"] = self.base_url_var.get().strip()
        self.config["node_name"] = self.node_name_var.get().strip()
        self.config["active_polling"] = bool(self.active_poll_var.get())
        self.config["last_selected_conversation"] = self.selected_conversation_key
        self.config["window_geometry"] = self.root.geometry()
        JsonStore.save_json(CONFIG_PATH, self.config)

    def _save_links(self) -> None:
        JsonStore.save_json(LINKS_PATH, self.links)

    def _save_pending_codes(self) -> None:
        JsonStore.save_json(PENDING_CODES_PATH, self.pending_codes)

    def _save_seen_deliveries(self) -> None:
        JsonStore.save_json(SEEN_DELIVERIES_PATH, sorted(self.seen_deliveries))

    def _pending_code_is_expired(self, item: dict[str, Any]) -> bool:
        expires_at = item.get("local_expires_at")
        if not expires_at:
            return True
        try:
            dt = datetime.fromisoformat(str(expires_at))
        except ValueError:
            return True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def _prune_pending_codes(self) -> None:
        original = len(self.pending_codes)
        self.pending_codes = [
            item for item in self.pending_codes
            if not self._pending_code_is_expired(item)
        ]
        if len(self.pending_codes) != original:
            self._save_pending_codes()

    def _future_iso(self, seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def _refresh_client_from_disk(self) -> None:
        base_url = self.base_url_var.get().strip()
        if not base_url:
            self.client = None
            return
        if IDENTITY_PATH.exists():
            try:
                self.client = ARQSClient.from_identity_file(base_url, IDENTITY_PATH)
                self.set_status(f"Loaded identity for node {self.client.identity.node_id}.")
                return
            except Exception as exc:
                self.client = ARQSClient(base_url)
                self.set_status(f"Identity file exists but failed to load: {exc}")
                return
        self.client = ARQSClient(base_url)
        self.set_status("No saved identity loaded yet.")

    # --------------------------
    # Conversation and display
    # --------------------------
    def _refresh_conversations(self) -> None:
        conversation_map: dict[str, Conversation] = {}
        for link in self.links:
            key = self._conversation_key(str(link["local_endpoint_id"]), str(link["remote_endpoint_id"]))
            title = self._conversation_title(link)
            subtitle = self._conversation_subtitle(link)
            timestamp = str(link.get("updated_at") or link.get("created_at") or "")
            conversation_map[key] = Conversation(
                key=key,
                local_endpoint_id=str(link["local_endpoint_id"]),
                remote_endpoint_id=str(link["remote_endpoint_id"]),
                title=title,
                subtitle=subtitle,
                last_timestamp=timestamp,
            )

        for msg in self.messages:
            key = self._conversation_key(str(msg["local_endpoint_id"]), str(msg["remote_endpoint_id"]))
            existing = conversation_map.get(key)
            title = existing.title if existing else self._fallback_conversation_title(msg)
            subtitle = existing.subtitle if existing else self._fallback_conversation_subtitle(msg)
            timestamp = str(msg.get("created_at") or msg.get("received_at") or "")
            if existing is None or timestamp > existing.last_timestamp:
                conversation_map[key] = Conversation(
                    key=key,
                    local_endpoint_id=str(msg["local_endpoint_id"]),
                    remote_endpoint_id=str(msg["remote_endpoint_id"]),
                    title=title,
                    subtitle=subtitle,
                    last_timestamp=timestamp,
                )

        self.conversations = sorted(
            conversation_map.values(),
            key=lambda item: item.last_timestamp,
            reverse=True,
        )
        self._rebuild_conversation_listbox()
        self._render_selected_conversation()

    def _rebuild_conversation_listbox(self) -> None:
        self.conversation_list.delete(0, tk.END)
        for convo in self.conversations:
            display = convo.title
            if convo.subtitle:
                display = f"{display} — {convo.subtitle}"
            self.conversation_list.insert(tk.END, display)
        self._restore_last_selection()

    def _restore_last_selection(self) -> None:
        if not self.conversations:
            return
        if self.selected_conversation_key is None:
            self.selected_conversation_key = self.conversations[0].key
        for idx, convo in enumerate(self.conversations):
            if convo.key == self.selected_conversation_key:
                self.conversation_list.selection_clear(0, tk.END)
                self.conversation_list.selection_set(idx)
                self.conversation_list.see(idx)
                break
        self._render_selected_conversation()

    def on_conversation_selected(self, _event: Any = None) -> None:
        selection = self.conversation_list.curselection()
        if not selection:
            return
        convo = self.conversations[selection[0]]
        self.selected_conversation_key = convo.key
        self._save_config()
        self._render_selected_conversation()

    def _render_selected_conversation(self) -> None:
        convo = self.get_selected_conversation()
        if convo is None:
            self.conversation_header_var.set("No conversation selected")
            self._set_history_text("")
            return

        header = convo.title
        if convo.subtitle:
            header = f"{header} — {convo.subtitle}"
        self.conversation_header_var.set(header)

        relevant = [
            item
            for item in self.messages
            if self._conversation_key(str(item["local_endpoint_id"]), str(item["remote_endpoint_id"])) == convo.key
        ]
        relevant.sort(key=self._message_sort_key)

        contact_name = convo.title

        lines: list[str] = []
        for item in relevant:
            direction = item.get("direction", "unknown")
            who = "You" if direction == "outgoing" else contact_name
            timestamp = self._format_dt(item.get("created_at") or item.get("received_at"))
            body = str(item.get("body") or "")
            if not body and item.get("data"):
                body = json.dumps(item.get("data"), ensure_ascii=False, indent=2)
            lines.append(f"[{timestamp}] {who}:\n{body}\n")

        self._set_history_text("\n".join(lines).strip())

    def _set_history_text(self, text: str) -> None:
        self.message_history.configure(state="normal")
        self.message_history.delete("1.0", tk.END)
        self.message_history.insert("1.0", text)
        self.message_history.configure(state="disabled")
        self.message_history.see(tk.END)

    def get_selected_conversation(self) -> Conversation | None:
        if not self.selected_conversation_key:
            return None
        for convo in self.conversations:
            if convo.key == self.selected_conversation_key:
                return convo
        return None

    # --------------------------
    # Business logic
    # --------------------------
    def load_identity(self) -> None:
        self._refresh_client_from_disk()
        self.refresh_everything(background=True)

    def register_node(self) -> None:
        base_url = self.base_url_var.get().strip()
        if not base_url:
            messagebox.showerror(APP_NAME, "Server URL is required.")
            return
        node_name = self.node_name_var.get().strip() or None
        self.client = ARQSClient(base_url)

        def job() -> tuple[str, str]:
            assert self.client is not None
            identity = self.client.register(node_name=node_name)
            identity.save(IDENTITY_PATH)
            return str(identity.node_id), str(identity.default_endpoint_id)

        def done(result: tuple[str, str]) -> None:
            node_id, endpoint_id = result
            self._refresh_client_from_disk()
            self.set_status(f"Registered node {node_id}. Default endpoint {endpoint_id} saved to disk.")
            self.refresh_everything(background=True)

        self.run_bg(job, on_success=done, label="Registering node")

    def create_endpoint(self) -> None:
        client = self.require_client()
        if client is None:
            return
        dialog = EndpointDialog(self.root, title="Create Endpoint")
        if not dialog.result:
            return
        endpoint_name = dialog.result["endpoint_name"]
        kind = dialog.result["kind"] or None
        alias = dialog.result["alias"] or endpoint_name

        def job() -> Endpoint:
            return client.create_endpoint(endpoint_name=endpoint_name, kind=kind, meta=None)

        def done(endpoint: Endpoint) -> None:
            self.config.setdefault("local_endpoint_aliases", {})[str(endpoint.endpoint_id)] = alias
            self._save_config()
            self.set_status(f"Created endpoint {endpoint.endpoint_name or endpoint.endpoint_id}.")
            self.refresh_everything(background=True)

        self.run_bg(job, on_success=done, label="Creating endpoint")

    def request_link_code(self) -> None:
        client = self.require_client()
        if client is None:
            return
        if not self.endpoints:
            messagebox.showerror(APP_NAME, "You need at least one endpoint before requesting a link code.")
            return
        dialog = RequestLinkDialog(self.root, self.endpoints, self.config.get("local_endpoint_aliases", {}))
        if not dialog.result:
            return

        source_endpoint_id = dialog.result["source_endpoint_id"]
        requested_mode = dialog.result["requested_mode"]

        def job() -> LinkCode:
            return client.request_link_code(source_endpoint_id, requested_mode=requested_mode)
      
        def done(link_code: LinkCode) -> None:
            self.pending_codes = [
                {
                    "code": link_code.code,
                    "link_code_id": str(link_code.link_code_id),
                    "source_endpoint_id": str(link_code.source_endpoint_id),
                    "requested_mode": link_code.requested_mode,
                    "created_at": link_code.created_at.isoformat(),
                    "expires_at": link_code.expires_at.isoformat(),
                    "local_expires_at": self._future_iso(LOCAL_LINK_CODE_TTL_SECONDS),
                    "status": link_code.status,
                }
            ]
            self._save_pending_codes()
            self.root.clipboard_clear()
            self.root.clipboard_append(link_code.code)
            self.set_status(f"Link code {link_code.code} created and copied to clipboard.")
            messagebox.showinfo(APP_NAME, f"Link code:\n\n{link_code.code}\n\nCopied to clipboard.")
            self.refresh_everything(background=True)

        self.run_bg(job, on_success=done, label="Requesting link code")

    def redeem_link_code(self) -> None:

        client = self.require_client()
        if client is None:
            return
        self._prune_pending_codes()
        dialog = RedeemLinkDialog(self.root, self.endpoints, self.config.get("local_endpoint_aliases", {}))
        if not dialog.result:
            return
        code = dialog.result["code"]
        destination_endpoint_id = dialog.result["destination_endpoint_id"]
        create_endpoint_name = dialog.result["create_endpoint_name"]
        remote_label = dialog.result["remote_label"]
        endpoint_alias = dialog.result["endpoint_alias"]

        def job() -> tuple[Link, Endpoint | None]:
            created_endpoint: Endpoint | None = None
            actual_destination = destination_endpoint_id
            if create_endpoint_name:
                created_endpoint = client.create_endpoint(endpoint_name=create_endpoint_name, kind="message", meta=None)
                actual_destination = str(created_endpoint.endpoint_id)
            link = client.redeem_link_code(code, actual_destination)
            return link, created_endpoint

        def done(result: tuple[Link, Endpoint | None]) -> None:
            link, created_endpoint = result
            if created_endpoint is not None:
                alias_value = endpoint_alias or created_endpoint.endpoint_name or str(created_endpoint.endpoint_id)
                self.config.setdefault("local_endpoint_aliases", {})[str(created_endpoint.endpoint_id)] = alias_value
                self._save_config()
            self._upsert_link_record(link, explicit_remote_label=remote_label)
            self.pending_codes = [item for item in self.pending_codes if item.get("code") != code.strip().upper()]
            self._save_pending_codes()
            self.set_status(f"Redeemed link code and saved link {link.link_id}.")
            self.refresh_everything(background=True)

        self.run_bg(job, on_success=done, label="Redeeming link code")

    def rename_contact(self) -> None:
        convo = self.get_selected_conversation()
        if convo is None:
            messagebox.showerror(APP_NAME, "Select a conversation first.")
            return
        record = self._get_link_record(convo.local_endpoint_id, convo.remote_endpoint_id)
        if record is None:
            messagebox.showerror(APP_NAME, "This conversation has no saved link record yet.")
            return
        current = str(record.get("remote_label") or "")
        new_value = simpledialog.askstring(APP_NAME, "Remote label", initialvalue=current, parent=self.root)
        if not new_value:
            return
        record["remote_label"] = new_value.strip()
        record["updated_at"] = self._now_iso()
        self._save_links()
        self._refresh_conversations()
        self.set_status("Contact label updated.")

    def delete_link(self) -> None:
        client = self.require_client()
        if client is None:
            return

        convo = self.get_selected_conversation()
        if convo is None:
            messagebox.showerror(APP_NAME, "Select a conversation first.")
            return

        record = self._get_link_record(convo.local_endpoint_id, convo.remote_endpoint_id)
        if record is None:
            messagebox.showerror(APP_NAME, "This conversation has no saved link record yet.")
            return

        confirmed = ask_continue_cancel(
            self.root,
            "Delete Link",
            "Warning: this will delete the link and all message history. "
            "This cannot be undone.\n\nAre you sure?",
        )
        if not confirmed:
            self.set_status("Delete link cancelled.")
            return

        link_id = str(record.get("link_id") or "")

        def job() -> dict[str, Any]:
            revoke_error: str | None = None
            if link_id and not link_id.startswith("local-"):
                try:
                    client.revoke_link(link_id)
                except Exception as exc:
                    revoke_error = str(exc)

            return {
                "local_endpoint_id": convo.local_endpoint_id,
                "remote_endpoint_id": convo.remote_endpoint_id,
                "link_id": link_id,
                "revoke_error": revoke_error,
            }

        def done(result: dict[str, Any]) -> None:
            self._delete_link_local(result["local_endpoint_id"], result["remote_endpoint_id"])
            if result["revoke_error"]:
                self.set_status(
                    f"Deleted local link/history, but server revoke failed: {result['revoke_error']}"
                )
                messagebox.showwarning(
                    APP_NAME,
                    "Local link and history were deleted, but server-side revoke failed:\n\n"
                    f"{result['revoke_error']}",
                )
            else:
                self.set_status("Link and message history deleted.")

        self.run_bg(job, on_success=done, label="Deleting link")

    def copy_selected_pending_code(self) -> None:
        self._prune_pending_codes()
        if not self.pending_codes:
            messagebox.showerror(APP_NAME, "No unexpired link code is saved locally.")
            return

        item = self.pending_codes[0]
        code = str(item["code"])
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.set_status(f"Copied current link code {code}.")

    def send_message(self) -> None:
        client = self.require_client()
        if client is None:
            return
        convo = self.get_selected_conversation()
        if convo is None:
            messagebox.showerror(APP_NAME, "Select a conversation first.")
            return
        body = self.message_entry.get("1.0", tk.END).strip()
        if not body:
            return

        def job() -> dict[str, Any]:
            result = client.send_packet(
                from_endpoint_id=convo.local_endpoint_id,
                to_endpoint_id=convo.remote_endpoint_id,
                body=body,
                data=None,
                headers={"content_type": "text/plain"},
                meta={"client": APP_NAME},
            )
            return {
                "packet_id": str(result.packet_id),
                "delivery_id": str(result.delivery_id) if result.delivery_id else None,
                "expires_at": result.expires_at.isoformat() if result.expires_at else None,
                "result": result.result,
            }

        def done(result: dict[str, Any]) -> None:
            self.message_entry.delete("1.0", tk.END)
            now = self._now_iso()
            self._append_message(
                {
                    "packet_id": result["packet_id"],
                    "delivery_id": result["delivery_id"],
                    "direction": "outgoing",
                    "local_endpoint_id": convo.local_endpoint_id,
                    "remote_endpoint_id": convo.remote_endpoint_id,
                    "body": body,
                    "data": {},
                    "created_at": now,
                    "received_at": None,
                    "delivery_state": result["result"],
                }
            )
            record = self._get_link_record(convo.local_endpoint_id, convo.remote_endpoint_id)
            if record is not None:
                record["updated_at"] = now
                self._save_links()
            self._refresh_conversations()
            self.set_status(f"Message sent ({result['result']}).")

        self.run_bg(job, on_success=done, label="Sending message")

    def refresh_everything(self, *, background: bool = True) -> None:
        if self.client is None:
            self._refresh_client_from_disk()
        client = self.require_client(silent=True)
        if client is None:
            return

        def job() -> dict[str, Any]:
            endpoints = client.list_endpoints()
            links = client.list_links()
            return {"endpoints": endpoints, "links": links}

        def done(result: dict[str, Any]) -> None:
            self.endpoints = result["endpoints"]
            self.endpoint_map = {str(item.endpoint_id): item for item in self.endpoints}
            for link in result["links"]:
                self._upsert_link_record(link)
            self._refresh_conversations()
            self.set_status(f"Loaded {len(self.endpoints)} endpoints and {len(self.links)} saved links.")

        if background:
            self.run_bg(job, on_success=done, label="Refreshing endpoints and links")
        else:
            done(job())

    def poll_inbox(self, *, background: bool = True, wait: int | None = None) -> None:
        client = self.require_client()
        if client is None:
            return
        wait_seconds = self._get_poll_wait_seconds(default=20) if wait is None else max(0, int(wait))

        def job() -> list[dict[str, Any]]:
            deliveries = client.poll_inbox(wait=wait_seconds, limit=100, request_timeout=wait_seconds + 10)
            items: list[dict[str, Any]] = []
            for delivery in deliveries:
                packet = delivery.packet
                item = {
                    "delivery_id": str(delivery.delivery_id),
                    "packet_id": str(packet.packet_id),
                    "from_endpoint_id": str(packet.from_endpoint_id),
                    "to_endpoint_id": str(packet.to_endpoint_id),
                    "body": packet.body or "",
                    "data": packet.data,
                    "created_at": packet.created_at.isoformat(),
                    "received_at": self._now_iso(),
                }
                items.append(item)
            return items

        def done(items: list[dict[str, Any]]) -> None:
            count = 0
            for item in items:
                delivery_id = str(item["delivery_id"])
                if delivery_id in self.seen_deliveries:
                    continue
                self.seen_deliveries.add(delivery_id)
                self._append_message(
                    {
                        "delivery_id": delivery_id,
                        "packet_id": str(item["packet_id"]),
                        "direction": "incoming",
                        "local_endpoint_id": str(item["to_endpoint_id"]),
                        "remote_endpoint_id": str(item["from_endpoint_id"]),
                        "body": item["body"],
                        "data": item["data"],
                        "created_at": item["created_at"],
                        "received_at": item["received_at"],
                    }
                )
                count += 1
                self._ack_delivery_async(delivery_id)
                self._ensure_message_link_stub(str(item["to_endpoint_id"]), str(item["from_endpoint_id"]))
            if count:
                self._save_seen_deliveries()
                self._refresh_conversations()
                self.set_status(f"Received {count} message(s).")
            else:
                self.set_status("Poll complete. No new messages.")
            self.refresh_everything(background=True)

        if background:
            self.run_bg(job, on_success=done, label="Polling inbox")
        else:
            done(job())

    def toggle_active_polling(self) -> None:
        enabled = bool(self.active_poll_var.get())
        self._set_polling_ui(enabled)
        self._save_config()
        if enabled:
            self._start_poll_thread()
        else:
            self._stop_poll_thread()

    def _set_polling_ui(self, enabled: bool) -> None:
        self.active_poll_var.set(enabled)

    def _start_poll_thread(self) -> None:
        self._stop_poll_thread()
        self.poll_stop.clear()
        self.poll_thread = threading.Thread(target=self._poll_loop, name="arqs-gui-poll", daemon=True)
        self.poll_thread.start()
        self.set_status("Automatically fetching inbox enabled.")

    def _stop_poll_thread(self) -> None:
        self.poll_stop.set()
        self.poll_thread = None

    def _poll_loop(self) -> None:
        while not self.poll_stop.is_set():
            if self.busy_count > 0:
                self.poll_stop.wait(1.0)
                continue
            client = self.client
            if client is None:
                self.poll_stop.wait(2.0)
                continue
            wait_seconds = self._get_poll_wait_seconds(default=20)
            try:
                deliveries = client.poll_inbox(wait=wait_seconds, limit=100, request_timeout=wait_seconds + 10)
                self.ui_queue.put(("poll_result", deliveries))
            except Exception as exc:
                self.ui_queue.put(("poll_error", exc))
                self.poll_stop.wait(3.0)

    def _ack_delivery_async(self, delivery_id: str) -> None:
        client = self.client
        if client is None:
            return

        def worker() -> None:
            try:
                client.ack_delivery(delivery_id, status="handled")
            except Exception:
                return

        threading.Thread(target=worker, name=f"ack-{delivery_id}", daemon=True).start()

    def _ensure_message_link_stub(self, local_endpoint_id: str, remote_endpoint_id: str) -> None:
        if self._get_link_record(local_endpoint_id, remote_endpoint_id) is not None:
            return
        self.links.append(
            {
                "link_id": f"local-{local_endpoint_id}-{remote_endpoint_id}",
                "mode": "unknown",
                "local_endpoint_id": local_endpoint_id,
                "remote_endpoint_id": remote_endpoint_id,
                "remote_label": f"Endpoint {remote_endpoint_id[:8]}",
                "created_at": self._now_iso(),
                "updated_at": self._now_iso(),
                "status": "unknown",
            }
        )
        self._save_links()

    def _upsert_link_record(self, link: Link, explicit_remote_label: str | None = None) -> None:
        endpoint_ids = {str(item.endpoint_id) for item in self.endpoints}
        a = str(link.endpoint_a_id)
        b = str(link.endpoint_b_id)

        if a in endpoint_ids and b not in endpoint_ids:
            local_endpoint_id, remote_endpoint_id = a, b
        elif b in endpoint_ids and a not in endpoint_ids:
            local_endpoint_id, remote_endpoint_id = b, a
        elif a in endpoint_ids and b in endpoint_ids:
            local_endpoint_id, remote_endpoint_id = a, b
        else:
            return

        record = self._get_link_record(local_endpoint_id, remote_endpoint_id)
        remote_label = explicit_remote_label or (record.get("remote_label") if record else None) or f"Endpoint {remote_endpoint_id[:8]}"
        payload = {
            "link_id": str(link.link_id),
            "mode": link.mode,
            "local_endpoint_id": local_endpoint_id,
            "remote_endpoint_id": remote_endpoint_id,
            "remote_label": remote_label,
            "created_at": link.created_at.isoformat(),
            "updated_at": self._now_iso(),
            "status": link.status,
        }
        if record is None:
            self.links.append(payload)
        else:
            record.update(payload)
        self._save_links()

    def _get_link_record(self, local_endpoint_id: str, remote_endpoint_id: str) -> dict[str, Any] | None:
        for item in self.links:
            if str(item.get("local_endpoint_id")) == local_endpoint_id and str(item.get("remote_endpoint_id")) == remote_endpoint_id:
                return item
        return None

    def _delete_link_local(self, local_endpoint_id: str, remote_endpoint_id: str) -> None:
        self.links = [
            item
            for item in self.links
            if not (
                str(item.get("local_endpoint_id")) == local_endpoint_id
                and str(item.get("remote_endpoint_id")) == remote_endpoint_id
            )
        ]

        self.messages = [
            item
            for item in self.messages
            if not (
                str(item.get("local_endpoint_id")) == local_endpoint_id
                and str(item.get("remote_endpoint_id")) == remote_endpoint_id
            )
        ]

        self.message_index = {
            str(item.get("packet_id") or item.get("delivery_id"))
            for item in self.messages
            if item.get("packet_id") or item.get("delivery_id")
        }

        self._save_links()
        MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MESSAGES_PATH.open("w", encoding="utf-8") as handle:
            for item in self.messages:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

        if self.selected_conversation_key == self._conversation_key(local_endpoint_id, remote_endpoint_id):
            self.selected_conversation_key = None

        self._save_config()
        self._refresh_conversations()

    def _clear_local_identity_state(self) -> None:
        self.links = []
        self.pending_codes = []
        self.seen_deliveries = set()
        self.messages = []
        self.message_index = set()
        self.endpoints = []
        self.endpoint_map = {}
        self.conversations = []
        self.selected_conversation_key = None
        self.config["local_endpoint_aliases"] = {}

        self._save_links()
        self._save_pending_codes()
        self._save_seen_deliveries()
        MESSAGES_PATH.parent.mkdir(parents=True, exist_ok=True)
        MESSAGES_PATH.write_text("", encoding="utf-8")

        self._save_config()
        self._refresh_conversations()

    def require_client(self, *, silent: bool = False) -> ARQSClient | None:
        if self.client is None:
            self._refresh_client_from_disk()
        if self.client is None:
            if not silent:
                messagebox.showerror(APP_NAME, "No client is loaded.")
            return None
        self.client.base_url = self.base_url_var.get().strip().rstrip("/")
        return self.client

    # --------------------------
    # Background execution
    # --------------------------
    def run_bg(self, func: Any, *, on_success: Any, label: str) -> None:
        def runner() -> None:
            self.ui_queue.put(("busy", label))
            try:
                result = func()
            except Exception as exc:
                self.ui_queue.put(("error", exc))
            else:
                self.ui_queue.put(("success", on_success, result))
            finally:
                self.ui_queue.put(("idle", label))

        threading.Thread(target=runner, name=label.replace(" ", "-"), daemon=True).start()

    def _process_ui_queue(self) -> None:
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "busy":
                self.busy_count += 1
                self.set_status(f"{item[1]}...")
            elif kind == "idle":
                self.busy_count = max(0, self.busy_count - 1)
            elif kind == "error":
                self._handle_error(item[1])
            elif kind == "success":
                callback, result = item[1], item[2]
                callback(result)
            elif kind == "poll_result":
                deliveries = item[1]
                self._handle_poll_deliveries(deliveries)
            elif kind == "poll_error":
                self._handle_error(item[1], popup=False)
        self.root.after(100, self._process_ui_queue)

    def _handle_poll_deliveries(self, deliveries: list[Any]) -> None:
        count = 0
        for delivery in deliveries:
            delivery_id = str(delivery.delivery_id)
            if delivery_id in self.seen_deliveries:
                self._ack_delivery_async(delivery_id)
                continue
            self.seen_deliveries.add(delivery_id)
            packet = delivery.packet
            self._append_message(
                {
                    "delivery_id": delivery_id,
                    "packet_id": str(packet.packet_id),
                    "direction": "incoming",
                    "local_endpoint_id": str(packet.to_endpoint_id),
                    "remote_endpoint_id": str(packet.from_endpoint_id),
                    "body": packet.body or "",
                    "data": packet.data,
                    "created_at": packet.created_at.isoformat(),
                    "received_at": self._now_iso(),
                }
            )
            self._ack_delivery_async(delivery_id)
            self._ensure_message_link_stub(str(packet.to_endpoint_id), str(packet.from_endpoint_id))
            count += 1
        if count:
            self._save_seen_deliveries()
            self._refresh_conversations()
            self.set_status(f"Received {count} message(s).")
            self.refresh_everything(background=True)

    def _handle_error(self, exc: Exception, *, popup: bool = True) -> None:
        if isinstance(exc, ARQSHTTPError):
            message = f"HTTP {exc.status_code}: {exc.detail}"
        else:
            message = str(exc)
        self.set_status(message)
        if popup:
            messagebox.showerror(APP_NAME, message)

    # --------------------------
    # Formatting
    # --------------------------

    def _message_sort_key(self, item: dict[str, Any]) -> tuple[datetime, datetime, str]:
        created_dt = self._parse_message_dt(item.get("created_at"))
        received_dt = self._parse_message_dt(item.get("received_at"))
        tie_breaker = str(item.get("packet_id") or item.get("delivery_id") or "")
        return (created_dt, received_dt, tie_breaker)

    def _parse_message_dt(self, value: Any) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _conversation_key(self, local_endpoint_id: str, remote_endpoint_id: str) -> str:
        return f"{local_endpoint_id}|{remote_endpoint_id}"

    def _conversation_title(self, link_record: dict[str, Any]) -> str:
        remote_label = str(link_record.get("remote_label") or f"Endpoint {str(link_record['remote_endpoint_id'])[:8]}")
        return remote_label

    def _conversation_subtitle(self, link_record: dict[str, Any]) -> str:
        local_id = str(link_record["local_endpoint_id"])
        endpoint = self.endpoint_map.get(local_id)
        alias = self.config.get("local_endpoint_aliases", {}).get(local_id)
        local_name = alias or (endpoint.endpoint_name if endpoint else None) or f"Local {local_id[:8]}"
        return f"via {local_name}"

    def _fallback_conversation_title(self, msg: dict[str, Any]) -> str:
        record = self._get_link_record(str(msg["local_endpoint_id"]), str(msg["remote_endpoint_id"]))
        if record is not None:
            return self._conversation_title(record)
        return f"Endpoint {str(msg['remote_endpoint_id'])[:8]}"

    def _fallback_conversation_subtitle(self, msg: dict[str, Any]) -> str:
        local_id = str(msg["local_endpoint_id"])
        endpoint = self.endpoint_map.get(local_id)
        if endpoint and endpoint.endpoint_name:
            return f"via {endpoint.endpoint_name}"
        alias = self.config.get("local_endpoint_aliases", {}).get(local_id)
        if alias:
            return f"via {alias}"
        return f"via {local_id[:8]}"

    def _get_poll_wait_seconds(self, *, default: int) -> int:
        raw = self.poll_wait_var.get().strip()
        try:
            value = int(raw)
        except ValueError:
            value = default
        value = max(0, min(60, value))
        self.poll_wait_var.set(str(value))
        self.config["poll_wait_seconds"] = value
        return value

    def _format_dt(self, value: Any) -> str:
        if not value:
            return "unknown time"
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self._save_config()

    def on_close(self) -> None:
        self._stop_poll_thread()
        self._save_config()
        self.root.destroy()

class ContinueCancelDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, message: str) -> None:
        super().__init__(parent)
        self.result = False
        self.title(title)
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        container = ttk.Frame(self, padding=12)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)

        ttk.Label(container, text=message, wraplength=460, justify="left").grid(
            row=0, column=0, sticky="w"
        )

        buttons = ttk.Frame(container)
        buttons.grid(row=1, column=0, sticky="e", pady=(12, 0))

        self.cancel_button = ttk.Button(buttons, text="Cancel", command=self._cancel)
        self.cancel_button.grid(row=0, column=0, padx=(0, 8))

        self.continue_button = ttk.Button(buttons, text="Continue", command=self._continue)
        self.continue_button.grid(row=0, column=1)

        self.bind("<Return>", lambda _event: self._cancel())
        self.bind("<Escape>", lambda _event: self._cancel())

        self.update_idletasks()
        parent_widget = parent.winfo_toplevel()
        x = parent_widget.winfo_rootx() + 60
        y = parent_widget.winfo_rooty() + 60
        self.geometry(f"+{x}+{y}")

        self.cancel_button.focus_set()
        self.wait_window(self)

    def _continue(self) -> None:
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()


def ask_continue_cancel(parent: tk.Misc, title: str, message: str) -> bool:
    dialog = ContinueCancelDialog(parent, title, message)
    return dialog.result

class EndpointDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, title: str) -> None:
        self.result: dict[str, str] | None = None
        self.endpoint_name_var = tk.StringVar()
        self.kind_var = tk.StringVar(value="message")
        self.alias_var = tk.StringVar()
        super().__init__(parent, title)

    def body(self, master: tk.Misc) -> Any:
        ttk.Label(master, text="Endpoint name").grid(row=0, column=0, sticky="w")
        ttk.Entry(master, textvariable=self.endpoint_name_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(master, text="Kind").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(master, textvariable=self.kind_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(master, text="Local alias").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(master, textvariable=self.alias_var).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        master.columnconfigure(1, weight=1)
        return None

    def validate(self) -> bool:
        name = self.endpoint_name_var.get().strip()
        if not name:
            messagebox.showerror(APP_NAME, "Endpoint name is required.", parent=self)
            return False
        return True

    def apply(self) -> None:
        self.result = {
            "endpoint_name": self.endpoint_name_var.get().strip(),
            "kind": self.kind_var.get().strip(),
            "alias": self.alias_var.get().strip(),
        }


class RequestLinkDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, endpoints: list[Endpoint], aliases: dict[str, str]) -> None:
        self.result: dict[str, str] | None = None
        self.endpoints = endpoints
        self.aliases = aliases
        self.endpoint_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="bidirectional")
        super().__init__(parent, "Request Link Code")

    def body(self, master: tk.Misc) -> Any:
        ttk.Label(master, text="Source endpoint").grid(row=0, column=0, sticky="w")
        combo = ttk.Combobox(master, textvariable=self.endpoint_var, state="readonly", width=60)
        combo["values"] = [self._endpoint_display(item) for item in self.endpoints]
        if self.endpoints:
            combo.current(0)
        combo.grid(row=0, column=1, sticky="ew")

        ttk.Label(master, text="Mode").grid(row=1, column=0, sticky="w", pady=(8, 0))
        mode_combo = ttk.Combobox(master, textvariable=self.mode_var, state="readonly")
        mode_combo["values"] = ("bidirectional", "a_to_b", "b_to_a")
        mode_combo.current(0)
        mode_combo.grid(row=1, column=1, sticky="w", pady=(8, 0))
        master.columnconfigure(1, weight=1)
        return combo

    def _endpoint_display(self, endpoint: Endpoint) -> str:
        endpoint_id = str(endpoint.endpoint_id)
        alias = self.aliases.get(endpoint_id)
        name = alias or endpoint.endpoint_name or f"Endpoint {endpoint_id[:8]}"
        return f"{name} [{endpoint_id}]"

    def validate(self) -> bool:
        return bool(self.endpoint_var.get().strip())

    def apply(self) -> None:
        selected = self.endpoint_var.get().strip()
        endpoint_id = selected.rsplit("[", 1)[1].rstrip("]")
        self.result = {
            "source_endpoint_id": endpoint_id,
            "requested_mode": self.mode_var.get().strip(),
        }


class RedeemLinkDialog(simpledialog.Dialog):
    def __init__(self, parent: tk.Misc, endpoints: list[Endpoint], aliases: dict[str, str]) -> None:
        self.result: dict[str, str] | None = None
        self.endpoints = endpoints
        self.aliases = aliases
        self.code_var = tk.StringVar()
        self.use_existing_var = tk.BooleanVar(value=bool(endpoints))
        self.endpoint_var = tk.StringVar()
        self.create_name_var = tk.StringVar()
        self.remote_label_var = tk.StringVar()
        self.endpoint_alias_var = tk.StringVar()
        super().__init__(parent, "Redeem Link Code")

    def body(self, master: tk.Misc) -> Any:
        ttk.Label(master, text="Link code").grid(row=0, column=0, sticky="w")
        ttk.Entry(master, textvariable=self.code_var).grid(row=0, column=1, sticky="ew")

        ttk.Checkbutton(
            master,
            text="Use existing endpoint",
            variable=self.use_existing_var,
            command=self._toggle_mode,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(master, text="Existing endpoint").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.endpoint_combo = ttk.Combobox(master, textvariable=self.endpoint_var, state="readonly", width=60)
        self.endpoint_combo["values"] = [self._endpoint_display(item) for item in self.endpoints]
        if self.endpoints:
            self.endpoint_combo.current(0)
        self.endpoint_combo.grid(row=2, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(master, text="Or create endpoint name").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.create_entry = ttk.Entry(master, textvariable=self.create_name_var)
        self.create_entry.grid(row=3, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(master, text="Local endpoint alias").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(master, textvariable=self.endpoint_alias_var).grid(row=4, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(master, text="Remote label").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(master, textvariable=self.remote_label_var).grid(row=5, column=1, sticky="ew", pady=(8, 0))

        master.columnconfigure(1, weight=1)
        self._toggle_mode()
        return None

    def _endpoint_display(self, endpoint: Endpoint) -> str:
        endpoint_id = str(endpoint.endpoint_id)
        alias = self.aliases.get(endpoint_id)
        name = alias or endpoint.endpoint_name or f"Endpoint {endpoint_id[:8]}"
        return f"{name} [{endpoint_id}]"

    def _toggle_mode(self) -> None:
        if self.use_existing_var.get() and self.endpoints:
            self.endpoint_combo.configure(state="readonly")
            self.create_entry.configure(state="disabled")
        else:
            self.endpoint_combo.configure(state="disabled")
            self.create_entry.configure(state="normal")

    def validate(self) -> bool:
        if not self.code_var.get().strip():
            messagebox.showerror(APP_NAME, "Link code is required.", parent=self)
            return False
        if self.use_existing_var.get() and self.endpoints:
            if not self.endpoint_var.get().strip():
                messagebox.showerror(APP_NAME, "Select an endpoint.", parent=self)
                return False
        else:
            if not self.create_name_var.get().strip():
                messagebox.showerror(APP_NAME, "Provide a name for the new endpoint.", parent=self)
                return False
        return True

    def apply(self) -> None:
        destination_endpoint_id = ""
        if self.use_existing_var.get() and self.endpoints and self.endpoint_var.get().strip():
            destination_endpoint_id = self.endpoint_var.get().strip().rsplit("[", 1)[1].rstrip("]")
        self.result = {
            "code": self.code_var.get().strip().upper(),
            "destination_endpoint_id": destination_endpoint_id,
            "create_endpoint_name": self.create_name_var.get().strip(),
            "remote_label": self.remote_label_var.get().strip(),
            "endpoint_alias": self.endpoint_alias_var.get().strip(),
        }


def main() -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
