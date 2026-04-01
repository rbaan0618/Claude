"""Main application window — assembles all GUI panels."""

import tkinter as tk
import threading
import time
import logging
import winsound
from datetime import datetime

from gui.theme import get_theme
from gui.dialpad import Dialpad
from gui.blf_panel import BlfPanel
from gui.contacts_panel import ContactsPanel
from gui.call_history import CallHistoryPanel
from gui.settings_dialog import SettingsDialog
from protocols.sip_handler import SipHandler
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

        # Protocol handler
        self.sip = SipHandler()

        # Call state
        self._current_call = None
        self._call_timer_running = False
        self._call_start_time = None
        self._ringing = False
        self._ring_type = "ringback"  # "ringback" for outbound, "incoming" for inbound

        # Attended transfer state
        self._transfer_in_progress = False
        self._transfer_target = None
        self._transfer_dialog = None

        self._build_window()
        self._setup_protocols()
        self._auto_register()

    def _build_window(self):
        c = self.colors

        self.root = tk.Tk()
        self.root.title("My Line Telecom Softphone")
        self.root.geometry("820x640")
        self.root.configure(bg=c["bg"])
        self.root.minsize(700, 500)

        if self.config.get("gui", {}).get("always_on_top"):
            self.root.attributes("-topmost", True)

        # ---- Top bar ----
        top_bar = tk.Frame(self.root, bg=c["status_bar"], height=36)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)

        tk.Label(top_bar, text="My Line Telecom", font=("Segoe UI", 11, "bold"),
                 bg=c["status_bar"], fg=c["accent"]).pack(side=tk.LEFT, padx=10)

        tk.Label(top_bar, text="SIP", font=("Segoe UI", 9, "bold"),
                 padx=10, pady=2, bg=c["accent"], fg="#ffffff").pack(side=tk.LEFT, padx=20)

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

        # Left: Tabbed panel (Contacts / Favorites / BLF)
        left_container = tk.Frame(content, bg=c["bg_secondary"], width=220)
        left_container.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 0), pady=4)
        left_container.pack_propagate(False)

        # Tab bar
        left_tab_bar = tk.Frame(left_container, bg=c["bg_secondary"])
        left_tab_bar.pack(fill=tk.X)

        self._left_tabs = {}
        self._left_tab_frames = {}
        self._left_active_tab = None

        left_content = tk.Frame(left_container, bg=c["bg_secondary"])
        left_content.pack(fill=tk.BOTH, expand=True)

        for tab_name in ["Contacts", "BLF"]:
            btn = tk.Label(left_tab_bar, text=tab_name, font=("Segoe UI", 9),
                           bg=c["bg_secondary"], fg=c["fg"], cursor="hand2",
                           padx=12, pady=4)
            btn.pack(side=tk.LEFT)
            btn.bind("<Button-1>", lambda e, n=tab_name: self._switch_left_tab(n))
            self._left_tabs[tab_name] = btn

            frame = tk.Frame(left_content, bg=c["bg_secondary"])
            self._left_tab_frames[tab_name] = frame

        # Contacts panel
        self.contacts_panel = ContactsPanel(
            self._left_tab_frames["Contacts"], theme_name=self.theme_name,
            on_dial=self._redial, on_change=self._save_contacts)
        self.contacts_panel.pack(fill=tk.BOTH, expand=True)

        # Load saved contacts
        contacts_list = self.config.get("contacts", {}).get("entries", [])
        self.contacts_panel.load_contacts(contacts_list)

        # BLF panel
        self.blf_panel = BlfPanel(self._left_tab_frames["BLF"],
                                  theme_name=self.theme_name,
                                  on_click=self._blf_clicked)
        self.blf_panel.pack(fill=tk.BOTH, expand=True)

        # Load saved BLF entries
        blf_entries = self.config.get("blf", {}).get("entries", [])
        self.blf_panel.load_entries(blf_entries)
        self.blf_panel.bind("<<BlfAdded>>", lambda e: self._save_blf())

        self._switch_left_tab("Contacts")

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
        """Initialize SIP protocol handler and set callbacks."""
        sip_config = self.config.get("sip", {})
        audio_config = self.config.get("audio", {})

        self.sip.set_callbacks(
            on_incoming_call=self._on_incoming_call,
            on_call_state_change=self._on_call_state_change,
            on_registration_state=self._on_registration_state,
            on_blf_state_change=self._on_blf_state_change,
        )

        if sip_config.get("enabled", True):
            self.sip.initialize(sip_config, audio_config)

    def _auto_register(self):
        """Auto-register if SIP credentials are configured."""
        cfg = self.config.get("sip", {})
        if cfg.get("enabled") and cfg.get("server") and cfg.get("username"):
            threading.Thread(
                target=self.sip.register,
                args=(cfg["server"], cfg["username"],
                      cfg.get("password", ""), cfg.get("port", 5060)),
                daemon=True
            ).start()

    def _active_handler(self):
        return self.sip

    # ---- Registration ----

    def _toggle_registration(self):
        handler = self._active_handler()
        cfg = self.config.get("sip", {})

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
            protocol="SIP",
            remote_number=number,
        )
        if handler.make_call(number):
            self.status_var.set(f"Calling {number}...")
            self.dialpad.caller_info_var.set(f"Calling: {number}")
        else:
            self._current_call.status = "failed"
            self._current_call.end()
            self._save_call_record()
            self.status_var.set("Call failed")

    def _answer_call(self):
        self._stop_ringtone()
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
        if self._transfer_in_progress:
            # Cancel transfer and resume original call
            self._cancel_transfer()
            self.dialpad.set_hold_active(False)
            return
        if handler.on_hold:
            handler.unhold_call()
            self.dialpad.set_hold_active(False)
            self.status_var.set("Call resumed")
        else:
            handler.hold_call()
            self.dialpad.set_hold_active(True)
            self.status_var.set("Call on hold")

    def _transfer(self):
        """Attended transfer: hold current call, dial target, then complete or cancel."""
        handler = self._active_handler()
        if not handler.in_call:
            self.status_var.set("Not in a call")
            return

        # If we're already in the consultation call, complete the transfer
        if self._transfer_in_progress:
            self._complete_transfer()
            return

        c = self.colors
        dialog = tk.Toplevel(self.root)
        dialog.title("Transfer Call")
        dialog.geometry("320x180")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.root)
        dialog.grab_set()
        self._transfer_dialog = dialog

        tk.Label(dialog, text="Transfer to:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 11)).pack(anchor=tk.W, padx=15, pady=(15, 2))

        target_var = tk.StringVar(value=self.dialpad.number_var.get().strip())
        entry = tk.Entry(dialog, textvariable=target_var, bg=c["bg_input"],
                         fg=c["fg"], font=("Segoe UI", 14), relief=tk.FLAT, bd=4,
                         insertbackground=c["fg"])
        entry.pack(fill=tk.X, padx=15)
        entry.focus_set()
        entry.select_range(0, tk.END)

        tk.Label(dialog, text="The current call will be placed on hold.\n"
                 "Dial the target, then complete or cancel the transfer.",
                 bg=c["bg"], fg=c["fg_dim"], font=("Segoe UI", 8),
                 justify=tk.LEFT).pack(anchor=tk.W, padx=15, pady=(5, 0))

        def _start_consultation():
            number = target_var.get().strip()
            if not number:
                return
            self._transfer_target = number
            # Hold the current call and start consultation
            handler.hold_call()
            self._transfer_in_progress = True
            handler.consultation_call(number)
            self.status_var.set(f"Consulting {number}... (original call on hold)")
            self.dialpad.caller_info_var.set(f"Consulting: {number}")
            dialog.destroy()

        entry.bind("<Return>", lambda e: _start_consultation())

        btn_frame = tk.Frame(dialog, bg=c["bg"])
        btn_frame.pack(fill=tk.X, padx=15, pady=10)

        tk.Button(btn_frame, text="Call", command=_start_consultation,
                  bg=c["accent"], fg="#ffffff", font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=20, pady=4).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="Cancel", command=dialog.destroy,
                  bg=c["button_bg"], fg=c["button_fg"], font=("Segoe UI", 10),
                  relief=tk.FLAT, padx=20, pady=4).pack(side=tk.LEFT, padx=(8, 0))

    def _complete_transfer(self):
        """Complete the attended transfer — REFER the held call to the consultation target."""
        handler = self._active_handler()
        handler.complete_attended_transfer()
        self._transfer_in_progress = False
        self._transfer_target = None
        self.status_var.set("Transfer completed")
        self.dialpad.caller_info_var.set("")

    def _cancel_transfer(self):
        """Cancel transfer — hang up consultation call, resume original call."""
        handler = self._active_handler()
        handler.cancel_consultation()
        self._transfer_in_progress = False
        self._transfer_target = None
        self.status_var.set("Transfer cancelled — call resumed")
        self.dialpad.caller_info_var.set("Connected")

    def _mute(self):
        handler = self._active_handler()
        if not handler.in_call or not handler._rtp_session:
            return
        muted = not handler._rtp_session._muted
        handler._rtp_session.set_muted(muted)
        self.dialpad.set_mute_active(muted)
        self.status_var.set("Microphone muted" if muted else "Microphone unmuted")

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
            self._start_ringtone("incoming")
        self.root.after(0, _update)

    def _on_call_state_change(self, protocol, state, reason):
        """Handle call state changes."""
        def _update():
            if state == "CONFIRMED":
                self._stop_ringtone()
                if self._transfer_in_progress:
                    # Consultation call answered — show transfer controls
                    self.status_var.set("Consultation connected — Transfer or Hold to cancel")
                    self.dialpad.caller_info_var.set(
                        f"Consulting: {self._transfer_target}\n(Transfer=complete, Hold=cancel)")
                    return
                if self._current_call:
                    self._current_call.answer()
                self._start_call_timer()
                self.status_var.set(f"In call ({protocol})")
                self.dialpad.caller_info_var.set("Connected")
            elif state == "DISCONNECTED":
                self._stop_ringtone()
                if self._transfer_in_progress:
                    # Consultation call ended (target didn't answer or hung up)
                    self._cancel_transfer()
                    return
                self._stop_call_timer()
                if self._current_call:
                    self._current_call.end()
                    self._save_call_record()
                    self._current_call = None
                self.dialpad.hide_incoming()
                self.dialpad.caller_info_var.set("")
                self.dialpad.timer_var.set("")
                self.dialpad.set_mute_active(False)
                self.dialpad.set_hold_active(False)
                self.status_var.set(f"Call ended: {reason}")
            elif state == "RINGING":
                self._start_ringtone("ringback")
                self.status_var.set("Ringing...")
            elif state == "HOLD":
                self.status_var.set("On hold")
            elif state == "CALLING":
                self.status_var.set("Calling...")
            elif state in ("REJECTED", "BUSY"):
                self._stop_ringtone()
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
                for entry in self.blf_panel.get_entries():
                    self.sip.subscribe_blf(entry["extension"])
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

    # ---- Ringtone ----

    def _start_ringtone(self, ring_type="ringback"):
        """Play a ringtone loop in a background thread."""
        if self._ringing:
            return
        self._ringing = True
        self._ring_type = ring_type
        threading.Thread(target=self._ringtone_loop, daemon=True).start()

    def _stop_ringtone(self):
        self._ringing = False

    def _check_ringing(self, chunks=1):
        """Sleep in small chunks, return False if ringing stopped."""
        for _ in range(chunks):
            if not self._ringing:
                return False
            time.sleep(0.1)
        return True

    def _ringtone_loop(self):
        """Play US standard ring cadence."""
        while self._ringing:
            try:
                if self._ring_type == "ringback":
                    # US ringback tone: 440Hz for 1s, 480Hz for 1s, 4s silence
                    winsound.Beep(440, 1000)
                    if not self._ringing:
                        return
                    winsound.Beep(480, 1000)
                    if not self._ringing:
                        return
                    # 4 second silence
                    if not self._check_ringing(40):
                        return
                else:
                    # US incoming ring: two bursts of 1s, 4s silence
                    winsound.Beep(1200, 1000)
                    if not self._ringing:
                        return
                    # Short gap
                    if not self._check_ringing(3):
                        return
                    winsound.Beep(1200, 1000)
                    if not self._ringing:
                        return
                    # 4 second silence
                    if not self._check_ringing(40):
                        return
            except Exception:
                break

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

    # ---- Left panel tabs ----

    def _switch_left_tab(self, name):
        c = self.colors
        if self._left_active_tab:
            self._left_tab_frames[self._left_active_tab].pack_forget()
            self._left_tabs[self._left_active_tab].configure(
                bg=c["bg_secondary"], fg=c["fg"])
        self._left_tab_frames[name].pack(fill=tk.BOTH, expand=True)
        self._left_tabs[name].configure(bg=c["accent"], fg="#ffffff")
        self._left_active_tab = name

    # ---- Contacts ----

    def _save_contacts(self):
        self.config.setdefault("contacts", {})["entries"] = \
            self.contacts_panel.get_contacts()
        save_config(self.config)

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
        self._save_contacts()
        self._save_blf()
        self.sip.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
