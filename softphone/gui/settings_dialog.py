"""Settings dialog for configuring SIP, IAX, and audio settings."""

import tkinter as tk
from tkinter import ttk
from gui.theme import get_theme


class SettingsDialog(tk.Toplevel):
    """Modal dialog for softphone configuration."""

    def __init__(self, parent, config, theme_name="dark", on_save=None):
        super().__init__(parent)
        self.colors = get_theme(theme_name)
        self.config = config
        self._on_save = on_save
        self.result = None

        self.title("Settings")
        self.geometry("480x640")
        self.configure(bg=self.colors["bg"])
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self._vars = {}
        self._build()
        self._load_values()

    def _build(self):
        c = self.colors

        # Tab-like notebook using frames
        tab_bar = tk.Frame(self, bg=c["bg_secondary"])
        tab_bar.pack(fill=tk.X)

        self._tabs = {}
        self._tab_frames = {}
        self._active_tab = None

        container = tk.Frame(self, bg=c["bg"])
        container.pack(fill=tk.BOTH, expand=True)

        for tab_name in ["SIP", "IAX", "Audio", "General"]:
            # Tab button
            btn = tk.Label(tab_bar, text=tab_name, font=("Segoe UI", 10),
                           bg=c["bg_secondary"], fg=c["fg"], cursor="hand2",
                           padx=16, pady=6)
            btn.pack(side=tk.LEFT)
            btn.bind("<Button-1>", lambda e, n=tab_name: self._switch_tab(n))
            self._tabs[tab_name] = btn

            # Tab content frame
            frame = tk.Frame(container, bg=c["bg"])
            self._tab_frames[tab_name] = frame

        self._build_sip_tab()
        self._build_iax_tab()
        self._build_audio_tab()
        self._build_general_tab()

        # Buttons
        btn_frame = tk.Frame(self, bg=c["bg"])
        btn_frame.pack(fill=tk.X, padx=15, pady=10)

        tk.Button(btn_frame, text="Cancel", command=self.destroy,
                  bg=c["button_bg"], fg=c["button_fg"], font=("Segoe UI", 10),
                  relief=tk.FLAT, padx=20, pady=4).pack(side=tk.RIGHT, padx=(5, 0))

        tk.Button(btn_frame, text="Save", command=self._save,
                  bg=c["accent"], fg="#ffffff", font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=20, pady=4).pack(side=tk.RIGHT)

        self._switch_tab("SIP")

    def _switch_tab(self, name):
        c = self.colors
        if self._active_tab:
            self._tab_frames[self._active_tab].pack_forget()
            self._tabs[self._active_tab].configure(bg=c["bg_secondary"], fg=c["fg"])
        self._tab_frames[name].pack(fill=tk.BOTH, expand=True, padx=15, pady=10)
        self._tabs[name].configure(bg=c["accent"], fg="#ffffff")
        self._active_tab = name

    def _field(self, parent, label, key, show=None):
        """Create a labeled input field and store its StringVar."""
        c = self.colors
        tk.Label(parent, text=label, bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(8, 2))
        var = tk.StringVar()
        self._vars[key] = var
        entry_kwargs = dict(textvariable=var, bg=c["bg_input"], fg=c["fg"],
                            font=("Segoe UI", 11), relief=tk.FLAT, bd=4,
                            insertbackground=c["fg"])
        if show:
            entry_kwargs["show"] = show
        tk.Entry(parent, **entry_kwargs).pack(fill=tk.X)
        return var

    def _server_field(self, parent, label, key):
        """Create a server field with auto-domain hint."""
        c = self.colors
        tk.Label(parent, text=label, bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(8, 2))
        row = tk.Frame(parent, bg=c["bg"])
        row.pack(fill=tk.X)
        var = tk.StringVar()
        self._vars[key] = var
        entry = tk.Entry(row, textvariable=var, bg=c["bg_input"], fg=c["fg"],
                         font=("Segoe UI", 11), relief=tk.FLAT, bd=4,
                         insertbackground=c["fg"])
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(row, text=".myline.tel", bg=c["bg"], fg=c["fg_dim"],
                 font=("Segoe UI", 10)).pack(side=tk.LEFT, padx=(2, 0))
        return var

    def _checkbox(self, parent, label, key):
        c = self.colors
        var = tk.BooleanVar()
        self._vars[key] = var
        cb = tk.Checkbutton(parent, text=label, variable=var,
                            bg=c["bg"], fg=c["fg"], selectcolor=c["bg_input"],
                            activebackground=c["bg"], activeforeground=c["fg"],
                            font=("Segoe UI", 10))
        cb.pack(anchor=tk.W, pady=(8, 0))
        return var

    def _build_sip_tab(self):
        f = self._tab_frames["SIP"]
        self._checkbox(f, "Enable SIP", "sip.enabled")
        self._server_field(f, "Server:", "sip.server")
        self._field(f, "Port:", "sip.port")
        self._field(f, "Local Port:", "sip.local_port")
        self._checkbox(f, "Enable rport (RFC 3581 NAT traversal)", "sip.rport")
        self._field(f, "Username:", "sip.username")
        self._field(f, "Password:", "sip.password", show="*")
        self._field(f, "Display Name:", "sip.display_name")

        c = self.colors
        tk.Label(f, text="Transport:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(8, 2))
        var = tk.StringVar(value="UDP")
        self._vars["sip.transport"] = var
        transport_frame = tk.Frame(f, bg=c["bg"])
        transport_frame.pack(anchor=tk.W)
        for t in ["UDP", "TCP", "TLS"]:
            tk.Radiobutton(transport_frame, text=t, variable=var, value=t,
                           bg=c["bg"], fg=c["fg"], selectcolor=c["bg_input"],
                           activebackground=c["bg"], font=("Segoe UI", 10)
                           ).pack(side=tk.LEFT, padx=(0, 10))

    def _build_iax_tab(self):
        f = self._tab_frames["IAX"]
        self._checkbox(f, "Enable IAX", "iax.enabled")
        self._server_field(f, "Server:", "iax.server")
        self._field(f, "Port:", "iax.port")
        self._field(f, "Local Port:", "iax.local_port")
        self._field(f, "Username:", "iax.username")
        self._field(f, "Password:", "iax.password", show="*")
        self._field(f, "Display Name:", "iax.display_name")

    def _build_audio_tab(self):
        f = self._tab_frames["Audio"]
        c = self.colors

        tk.Label(f, text="Audio devices are auto-detected.\n"
                 "Leave blank to use system defaults.",
                 bg=c["bg"], fg=c["fg_dim"], font=("Segoe UI", 9),
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 5))

        self._field(f, "Input Device:", "audio.input_device")
        self._field(f, "Output Device:", "audio.output_device")
        self._field(f, "Ring Device:", "audio.ring_device")

    def _build_general_tab(self):
        f = self._tab_frames["General"]
        self._checkbox(f, "Always on top", "gui.always_on_top")
        self._checkbox(f, "Start minimized", "gui.start_minimized")

        c = self.colors
        tk.Label(f, text="Theme:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(8, 2))
        var = tk.StringVar(value="dark")
        self._vars["gui.theme"] = var
        theme_frame = tk.Frame(f, bg=c["bg"])
        theme_frame.pack(anchor=tk.W)
        for t in ["dark", "light"]:
            tk.Radiobutton(theme_frame, text=t.capitalize(), variable=var, value=t,
                           bg=c["bg"], fg=c["fg"], selectcolor=c["bg_input"],
                           activebackground=c["bg"], font=("Segoe UI", 10)
                           ).pack(side=tk.LEFT, padx=(0, 10))

    def _load_values(self):
        """Populate fields from config dict."""
        for key, var in self._vars.items():
            parts = key.split(".")
            value = self.config
            for p in parts:
                value = value.get(p, "")
                if not isinstance(value, dict) and p != parts[-1]:
                    value = ""
                    break
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            else:
                # Strip .myline.tel suffix for server fields display
                s = str(value)
                if parts[-1] == "server" and s.endswith(".myline.tel"):
                    s = s[:-len(".myline.tel")]
                var.set(s)

    def _save(self):
        """Write field values back to config and close."""
        for key, var in self._vars.items():
            parts = key.split(".")
            target = self.config
            for p in parts[:-1]:
                target = target.setdefault(p, {})
            value = var.get()
            # Auto-append .myline.tel for server fields without a dot
            if parts[-1] == "server" and isinstance(value, str) and value.strip():
                value = value.strip()
                if "." not in value:
                    value = value + ".myline.tel"
            # Convert port fields to int
            elif parts[-1] in ("port", "local_port"):
                try:
                    value = int(value)
                except ValueError:
                    if parts[-1] == "local_port":
                        value = 0
                    else:
                        value = 5060 if parts[0] == "sip" else 4569
            elif isinstance(var, tk.BooleanVar):
                value = var.get()
            target[parts[-1]] = value

        self.result = self.config
        if self._on_save:
            self._on_save(self.config)
        self.destroy()
