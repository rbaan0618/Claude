"""Dark and light theme definitions for the softphone GUI."""

DARK = {
    "bg": "#1e1e2e",
    "bg_secondary": "#282840",
    "bg_input": "#313152",
    "fg": "#cdd6f4",
    "fg_dim": "#6c7086",
    "accent": "#89b4fa",
    "accent_hover": "#74c7ec",
    "green": "#a6e3a1",
    "red": "#f38ba8",
    "orange": "#fab387",
    "yellow": "#f9e2af",
    "border": "#45475a",
    "button_bg": "#45475a",
    "button_fg": "#cdd6f4",
    "button_active": "#585b70",
    "dialpad_bg": "#313152",
    "dialpad_fg": "#cdd6f4",
    "dialpad_active": "#45475a",
    "status_bar": "#181825",
}

LIGHT = {
    "bg": "#eff1f5",
    "bg_secondary": "#e6e9ef",
    "bg_input": "#ccd0da",
    "fg": "#4c4f69",
    "fg_dim": "#8c8fa1",
    "accent": "#1e66f5",
    "accent_hover": "#2a7de1",
    "green": "#40a02b",
    "red": "#d20f39",
    "orange": "#fe640b",
    "yellow": "#df8e1d",
    "border": "#bcc0cc",
    "button_bg": "#ccd0da",
    "button_fg": "#4c4f69",
    "button_active": "#bcc0cc",
    "dialpad_bg": "#dce0e8",
    "dialpad_fg": "#4c4f69",
    "dialpad_active": "#ccd0da",
    "status_bar": "#dce0e8",
}


def get_theme(name="dark"):
    return DARK if name == "dark" else LIGHT
