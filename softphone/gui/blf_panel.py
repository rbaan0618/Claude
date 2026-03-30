"""BLF (Busy Lamp Field) panel widget."""

import tkinter as tk
from gui.theme import get_theme
from models.blf_entry import BlfEntry


class BlfPanel(tk.Frame):
    """Panel showing BLF status indicators for monitored extensions."""

    def __init__(self, parent, theme_name="dark", on_click=None, **kwargs):
        self.colors = get_theme(theme_name)
        super().__init__(parent, bg=self.colors["bg_secondary"], **kwargs)
        self._on_click = on_click
        self._entries = {}   # extension -> BlfEntry
        self._widgets = {}   # extension -> widget dict
        self._build()

    def _build(self):
        c = self.colors

        # Header
        header = tk.Frame(self, bg=c["bg_secondary"])
        header.pack(fill=tk.X, padx=8, pady=(8, 4))

        tk.Label(header, text="BLF Monitor", font=("Segoe UI", 11, "bold"),
                 bg=c["bg_secondary"], fg=c["fg"]).pack(side=tk.LEFT)

        self.add_btn = tk.Label(header, text="+", font=("Segoe UI", 14, "bold"),
                                bg=c["bg_secondary"], fg=c["accent"], cursor="hand2")
        self.add_btn.pack(side=tk.RIGHT)
        self.add_btn.bind("<Button-1>", lambda e: self._add_dialog())

        # Scrollable container
        self.canvas = tk.Canvas(self, bg=c["bg_secondary"], highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner_frame = tk.Frame(self.canvas, bg=c["bg_secondary"])

        self.inner_frame.bind("<Configure>",
                              lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def add_entry(self, extension: str, label: str = "", state: str = "unknown"):
        """Add a BLF entry to monitor."""
        if extension in self._entries:
            return
        entry = BlfEntry(extension=extension, label=label, state=state)
        self._entries[extension] = entry
        self._create_widget(entry)

    def remove_entry(self, extension: str):
        """Remove a BLF entry."""
        if extension in self._widgets:
            self._widgets[extension]["frame"].destroy()
            del self._widgets[extension]
        self._entries.pop(extension, None)

    def update_state(self, extension: str, state: str):
        """Update the state of a BLF entry."""
        if extension in self._entries:
            self._entries[extension].state = state
            if extension in self._widgets:
                self._widgets[extension]["indicator"].configure(bg=self._entries[extension].color)
                self._widgets[extension]["state_label"].configure(text=state.capitalize())

    def _create_widget(self, entry: BlfEntry):
        c = self.colors

        frame = tk.Frame(self.inner_frame, bg=c["bg"], cursor="hand2",
                         padx=6, pady=4, relief=tk.FLAT, bd=1)
        frame.pack(fill=tk.X, padx=4, pady=2)

        # Status indicator (colored circle)
        indicator = tk.Canvas(frame, width=14, height=14, bg=c["bg"],
                              highlightthickness=0)
        indicator.pack(side=tk.LEFT, padx=(0, 6))
        indicator.create_oval(2, 2, 12, 12, fill=entry.color, outline="")
        # Store reference so we can update the fill color
        indicator._oval_id = 1  # first item created

        # Extension label
        name_label = tk.Label(frame, text=entry.display_name,
                              font=("Segoe UI", 10, "bold"),
                              bg=c["bg"], fg=c["fg"])
        name_label.pack(side=tk.LEFT)

        # Extension number (if label differs)
        if entry.label and entry.label != entry.extension:
            ext_label = tk.Label(frame, text=f"({entry.extension})",
                                 font=("Segoe UI", 9),
                                 bg=c["bg"], fg=c["fg_dim"])
            ext_label.pack(side=tk.LEFT, padx=(4, 0))

        # State text
        state_label = tk.Label(frame, text=entry.state.capitalize(),
                               font=("Segoe UI", 9),
                               bg=c["bg"], fg=c["fg_dim"])
        state_label.pack(side=tk.RIGHT)

        # Remove button
        remove_btn = tk.Label(frame, text="\u2715", font=("Segoe UI", 9),
                              bg=c["bg"], fg=c["fg_dim"], cursor="hand2")
        remove_btn.pack(side=tk.RIGHT, padx=(0, 4))
        remove_btn.bind("<Button-1>", lambda e, ext=entry.extension: self.remove_entry(ext))

        # Click to dial
        for w in (frame, name_label):
            w.bind("<Button-1>", lambda e, ext=entry.extension: self._click(ext))

        self._widgets[entry.extension] = {
            "frame": frame,
            "indicator": indicator,
            "state_label": state_label,
        }

    def _update_indicator_color(self, indicator, color):
        """Redraw the indicator circle with a new color."""
        indicator.delete("all")
        indicator.create_oval(2, 2, 12, 12, fill=color, outline="")

    def update_state(self, extension: str, state: str):
        """Update the state of a BLF entry."""
        if extension in self._entries:
            self._entries[extension].state = state
            if extension in self._widgets:
                color = self._entries[extension].color
                self._update_indicator_color(self._widgets[extension]["indicator"], color)
                self._widgets[extension]["state_label"].configure(text=state.capitalize())

    def _click(self, extension):
        if self._on_click:
            self._on_click(extension)

    def _add_dialog(self):
        """Show a simple dialog to add a BLF entry."""
        c = self.colors
        dialog = tk.Toplevel(self)
        dialog.title("Add BLF Entry")
        dialog.geometry("300x160")
        dialog.configure(bg=c["bg"])
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        tk.Label(dialog, text="Extension:", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, padx=15, pady=(15, 2))
        ext_var = tk.StringVar()
        ext_entry = tk.Entry(dialog, textvariable=ext_var, bg=c["bg_input"],
                             fg=c["fg"], font=("Segoe UI", 11), relief=tk.FLAT, bd=4)
        ext_entry.pack(fill=tk.X, padx=15)
        ext_entry.focus_set()

        tk.Label(dialog, text="Label (optional):", bg=c["bg"], fg=c["fg"],
                 font=("Segoe UI", 10)).pack(anchor=tk.W, padx=15, pady=(8, 2))
        lbl_var = tk.StringVar()
        tk.Entry(dialog, textvariable=lbl_var, bg=c["bg_input"],
                 fg=c["fg"], font=("Segoe UI", 11), relief=tk.FLAT, bd=4
                 ).pack(fill=tk.X, padx=15)

        def _add():
            ext = ext_var.get().strip()
            if ext:
                self.add_entry(ext, lbl_var.get().strip())
                self.event_generate("<<BlfAdded>>")
                dialog.destroy()

        tk.Button(dialog, text="Add", command=_add, bg=c["accent"],
                  fg="#ffffff", font=("Segoe UI", 10, "bold"),
                  relief=tk.FLAT, padx=20, pady=4).pack(pady=10)

    def get_entries(self):
        """Return list of dicts for saving to config."""
        return [{"extension": e.extension, "label": e.label}
                for e in self._entries.values()]

    def load_entries(self, entries_list):
        """Load entries from config list of dicts."""
        for item in entries_list:
            self.add_entry(item.get("extension", ""),
                           item.get("label", ""))
