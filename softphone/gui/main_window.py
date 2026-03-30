"""Main application window — assembles all GUI panels."""

import tkinter as tk
import threading
import time
import logging
from datetime import datetime

from gui.theme import get_theme
from gui.dialpad import Dialpad
from gui.blf_panel import BlfPanel
from gui.call_history import CallHistoryPanel
from gui.settings_dialog import SettingsDialog
from protocols.sip_handler import SipHandler
from protocols.iax_handler import IaxHandler
from models.call_record import CallRecord
from utils.database import add_call_record
from config import load_config, save_config

logger = logging.getLogger(__name__)


class MainWindow:
    """Main softphone window."""

    def __init__(self):
        self.config = load_config()
        self.theme_name = self.config.get("gui", {}).get("theme", "dark")
        self.colors = get_theme(self.theme_name)

        # Protocol handlers
        self.sip = SipHandler()
        self.iax = IaxHandler()
        self.active_protocol = "SIP"  # or "IAX"

        # Call state
        self._current_call = None
        self._call_timer_running = False
        self._call_start_time = None

        self._build_window()
        self._setup_protocols()
        self._auto_register()

    def _build_window(self):
        c = self.colors

        self.root = tk.Tk()
        self.root.title("PySoftphone")
        self.root.geometry("820x640")
        self.root.configure(bg=c["bg"])
        self.root.minsize(700, 500)

        if self.config.get("gui", {}).get("always_on_top"):
            self.root.attributes("-topmost", True)

        # ---- Top bar ----
        top_bar = tk.Frame(self.root, bg=c["status_bar"], height=36)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)

        tk.Label(top_bar, text="PySoftphone", font=("Segoe UI", 11, "bold"),
                 bg=c["status_bar"], fg=c["accent"]).pack(side=tk.LEFT, padx=10)

        # Protocol selector
        proto_frame = tk.Frame(top_bar, bg=c["status_bar"])
        proto_frame.pack(side=tk.LEFT, padx=20)

        self._proto_buttons = {}
        for proto in ["SIP", "IAX"]:
            btn = tk.Label(proto_frame, text=proto, font=("Segoe UI", 9, "bold"),
                           padx=10, pady=2, cursor="hand2",
                           bg=c["accent"] if proto == self.active_protocol else c["status_bar"],
                           fg="#ffffff" if proto == self.active_protocol else c["fg_dim"])
            btn.pack(side=tk.LEFT, padx=2)
            btn.bind("<Button-1>", lambda e, p=proto: self._switch_protocol(p))
            self._proto_buttons[proto] = btn

        # Registration status
        self.reg_status_var = tk.StringVar(value="Not registered")
        self.reg_status = tk.Label(top_bar, textvariable=self.reg_status_var,
                                   font=("Segoe UI", 9),
                                   bg=c["status_bar"], fg=c["fg_dim"])
        self.reg_status.pack(side=tk.LEFT, padx=10)

        # Settings button
        settings_btn = tk.Label(top_bar, text="\u2699", font=("Segoe UI", 16),
                                bg=c["status_bar"], fg=c["fg"], cursor="hand2")
        settings_btn.pack(side=tk.RIGHT, padx=10)
        settings_btn.bind("<Button-1>", lambda e: self._open_settings())

        # Register / Unregister button
        self.reg_btn_var = tk.StringVar(value="Register")
        self.reg_btn = tk.Label(top_bar, textvariable=self.reg_btn_var,
                                font=("Segoe UI", 9, "bold"),
                                bg=c["button_bg"], fg=c["button_fg"],
                                cursor="hand2", padx=10, pady=2)
        self.reg_btn.pack(side=tk.RIGHT, padx=5)
        self.reg_btn.bind("<Button-1>", lambda e: self._toggle_registration())

        # ---- Main content (3-panel layout) ----
        content = tk.Frame(self.root, bg=c["bg"])
        content.pack(fill=tk.BOTH, expand=True)

        # Left: BLF panel
        blf_container = tk.Frame(content, bg=c["bg_secondary"], width=200)
        blf_container.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 0), pady=4)
        blf_container.pack_propagate(False)

        self.blf_panel = BlfPanel(blf_container, theme_name=self.theme_name,
                                  on_click=self._blf_clicked)
        self.blf_panel.pack(fill=tk.BOTH, expand=True)

        # Load saved BLF entries
        blf_entries = self.config.get("blf", {}).get("entries", [])
        self.blf_panel.load_entries(blf_entries)
        self.blf_panel.bind("<<BlfAdded>>", lambda e: self._save_blf())

        # Center: Dialpad
        center = tk.Frame(content, bg=c["bg"])
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.dialpad = Dialpad(center, theme_name=self.theme_name,
                               on_call=self._make_call,
                               on_hangup=self._hangup,
                               on_dtmf=self._send_dtmf,
                               on_answer=self._answer_call)
        self.dialpad.pack(fill=tk.BOTH, expand=True)

        # Mid-call events
        self.dialpad.bind("<<MidCall-hold>>", lambda e: self._hold())
        self.dialpad.bind("<<MidCall-transfer>>", lambda e: self._transfer())
        self.dialpad.bind("<<MidCall-mute>>", lambda e: self._mute())

        # Right: Call history
        history_container = tk.Frame(content, bg=c["bg_secondary"], width=250)
        history_container.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4), pady=4)
        history_container.pack_propagate(False)

        self.history_panel = CallHistoryPanel(
            history_container, theme_name=self.theme_name,
            on_redial=self._redial)
        self.history_panel.pack(fill=tk.BOTH, expand=True)

        # ---- Status bar ----
        status_bar = tk.Frame(self.root, bg=c["status_bar"], height=24)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
        status_bar.pack_propagate(False)

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(status_bar, textvariable=self.status_var,
                 font=("Segoe UI", 8), bg=c["status_bar"],
                 fg=c["fg_dim"]).pack(side=tk.LEFT, padx=8)

        # Keyboard bindings
        self.root.bind("<Escape>", lambda e: self._hangup())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- Protocol setup ----

    def _setup_protocols(self):
        """Initialize protocol handlers and set callbacks."""
        sip_config = self.config.get("sip", {})
        iax_config = self.config.get("iax", {})

        self.sip.set_callbacks(
            on_incoming_call=self._on_incoming_call,
            on_call_state_change=self._on_call_state_change,
            on_registration_state=self._on_registration_state,
            on_blf_state_change=self._on_blf_state_change,
        )
        self.iax.set_callbacks(
            on_incoming_call=self._on_incoming_call,
            on_call_state_change=self._on_call_state_change,
            on_registration_state=self._on_registration_state,
            on_blf_state_change=self._on_blf_state_change,
        )

        if sip_config.get("enabled", True):
            self.sip.initialize(sip_config)
        if iax_config.get("enabled", False):
            self.iax.initialize(iax_config)

    def _auto_register(self):
        """Auto-register if credentials are configured."""
        for proto_name, handler, cfg_key in [("SIP", self.sip, "sip"), ("IAX", self.iax, "iax")]:
            cfg = self.config.get(cfg_key, {})
            if cfg.get("enabled") and cfg.get("server") and cfg.get("username"):
                threading.Thread(
                    target=handler.register,
                    args=(cfg["server"], cfg["username"],
                          cfg.get("password", ""), cfg.get("port", 5060)),
                    daemon=True
                ).start()

    def _active_handler(self):
        return self.sip if self.active_protocol == "SIP" else self.iax

    # ---- Protocol switching ----

    def _switch_protocol(self, proto):
        if proto == self.active_protocol:
            return
        c = self.colors
        self._proto_buttons[self.active_protocol].configure(
            bg=c["status_bar"], fg=c["fg_dim"])
        self.active_protocol = proto
        self._proto_buttons[proto].configure(bg=c["accent"], fg="#ffffff")
        handler = self._active_handler()
        if handler.registered:
            self.reg_status_var.set(f"{proto}: Registered")
        else:
            self.reg_status_var.set(f"{proto}: Not registered")
        self.status_var.set(f"Switched to {proto}")

    # ---- Registration ----

    def _toggle_registration(self):
        handler = self._active_handler()
        cfg_key = self.active_protocol.lower()
        cfg = self.config.get(cfg_key, {})

        if handler.registered:
            handler.unregister()
        else:
            if not cfg.get("server") or not cfg.get("username"):
                self.status_var.set("Configure server/credentials in Settings first")
                self._open_settings()
                return
            threading.Thread(
                target=handler.register,
                args=(cfg["server"], cfg["username"],
                      cfg.get("password", ""), cfg.get("port", 5060)),
                daemon=True
            ).start()

    # ---- Call operations ----

    def _make_call(self, number):
        handler = self._active_handler()
        if handler.in_call:
            self.status_var.set("Already in a call")
            return

        self._current_call = CallRecord(
            direction="outbound",
            protocol=self.active_protocol,
            remote_number=number,
        )
        if handler.make_call(number):
            self.status_var.set(f"Calling {number} via {self.active_protocol}...")
            self.dialpad.caller_info_var.set(f"Calling: {number}")
        else:
            self._current_call.status = "failed"
            self._current_call.end()
            self._save_call_record()
            self.status_var.set("Call failed")

    def _answer_call(self):
        handler = self._active_handler()
        handler.answer_call()
        if self._current_call:
            self._current_call.answer()
            self._start_call_timer()

    def _hangup(self):
        handler = self._active_handler()
        handler.hangup_call()
        self._stop_call_timer()
        if self._current_call:
            self._current_call.end()
            self._save_call_record()
            self._current_call = None
        self.dialpad.hide_incoming()
        self.dialpad.caller_info_var.set("")
        self.dialpad.timer_var.set("")
        self.status_var.set("Ready")

    def _send_dtmf(self, digit):
        handler = self._active_handler()
        if handler.in_call:
            handler.send_dtmf(digit)

    def _hold(self):
        handler = self._active_handler()
        handler.hold_call()
        self.status_var.set("Call on hold")

    def _transfer(self):
        number = self.dialpad.number_var.get().strip()
        if number:
            handler = self._active_handler()
            handler.transfer_call(number)
            self.status_var.set(f"Transferring to {number}")

    def _mute(self):
        self.status_var.set("Mute toggled")

    # ---- Callbacks from protocol handlers (called from background threads) ----

    def _on_incoming_call(self, protocol, remote_number, remote_name):
        """Handle incoming call notification."""
        def _update():
            self._current_call = CallRecord(
                direction="inbound",
                protocol=protocol,
                remote_number=remote_number,
                remote_name=remote_name,
            )
            display = f"{remote_name} <{remote_number}>" if remote_name else remote_number
            self.dialpad.show_incoming(display)
            self.status_var.set(f"Incoming {protocol} call: {display}")
        self.root.after(0, _update)

    def _on_call_state_change(self, protocol, state, reason):
        """Handle call state changes."""
        def _update():
            if state == "CONFIRMED":
                if self._current_call:
                    self._current_call.answer()
                self._start_call_timer()
                self.status_var.set(f"In call ({protocol})")
                self.dialpad.caller_info_var.set("Connected")
            elif state == "DISCONNECTED":
                self._stop_call_timer()
                if self._current_call:
                    self._current_call.end()
                    self._save_call_record()
                    self._current_call = None
                self.dialpad.hide_incoming()
                self.dialpad.caller_info_var.set("")
                self.dialpad.timer_var.set("")
                self.status_var.set(f"Call ended: {reason}")
            elif state == "RINGING":
                self.status_var.set("Ringing...")
            elif state == "HOLD":
                self.status_var.set("On hold")
            elif state == "CALLING":
                self.status_var.set("Calling...")
            elif state in ("REJECTED", "BUSY"):
                if self._current_call:
                    self._current_call.status = "rejected"
                    self._current_call.end()
                    self._save_call_record()
                    self._current_call = None
                self.status_var.set(f"Call {state.lower()}: {reason}")
        self.root.after(0, _update)

    def _on_registration_state(self, protocol, registered, code):
        """Handle registration state changes."""
        def _update():
            c = self.colors
            if registered:
                self.reg_status_var.set(f"{protocol}: Registered")
                self.reg_status.configure(fg=c["green"])
                self.reg_btn_var.set("Unregister")
                self.status_var.set(f"{protocol} registered successfully")

                # Subscribe BLF entries
                handler = self.sip if protocol == "SIP" else self.iax
                for entry in self.blf_panel.get_entries():
                    handler.subscribe_blf(entry["extension"])
            else:
                self.reg_status_var.set(f"{protocol}: Not registered")
                self.reg_status.configure(fg=c["red"])
                self.reg_btn_var.set("Register")
                if code == 401:
                    self.status_var.set(f"{protocol}: Authentication failed")
                else:
                    self.status_var.set(f"{protocol}: Unregistered")
        self.root.after(0, _update)

    def _on_blf_state_change(self, extension, state):
        """Handle BLF presence changes."""
        self.root.after(0, lambda: self.blf_panel.update_state(extension, state))

    # ---- Call timer ----

    def _start_call_timer(self):
        self._call_start_time = time.time()
        self._call_timer_running = True
        self._update_timer()

    def _stop_call_timer(self):
        self._call_timer_running = False

    def _update_timer(self):
        if not self._call_timer_running:
            return
        elapsed = int(time.time() - self._call_start_time)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            self.dialpad.timer_var.set(f"{hours:d}:{mins:02d}:{secs:02d}")
        else:
            self.dialpad.timer_var.set(f"{mins:d}:{secs:02d}")
        self.root.after(1000, self._update_timer)

    # ---- Call record persistence ----

    def _save_call_record(self):
        if not self._current_call:
            return
        cr = self._current_call
        add_call_record(
            direction=cr.direction,
            protocol=cr.protocol,
            remote_number=cr.remote_number,
            remote_name=cr.remote_name,
            status=cr.status,
            started_at=cr.started_at,
            answered_at=cr.answered_at,
            ended_at=cr.ended_at,
            duration_seconds=cr.duration_seconds,
        )
        self.history_panel.refresh()

    # ---- BLF ----

    def _blf_clicked(self, extension):
        """Dial the clicked BLF extension."""
        self.dialpad.set_number(extension)
        self._make_call(extension)

    def _save_blf(self):
        self.config.setdefault("blf", {})["entries"] = self.blf_panel.get_entries()
        save_config(self.config)

    # ---- Redial ----

    def _redial(self, number):
        self.dialpad.set_number(number)
        self._make_call(number)

    # ---- Settings ----

    def _open_settings(self):
        def on_save(new_config):
            self.config = new_config
            save_config(self.config)
            self.status_var.set("Settings saved — restart to apply theme changes")
            # Re-register if credentials changed
            self._auto_register()

        SettingsDialog(self.root, self.config, self.theme_name, on_save=on_save)

    # ---- Window lifecycle ----

    def _on_close(self):
        """Clean shutdown."""
        self._save_blf()
        self.sip.shutdown()
        self.iax.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
