"""
Colour themes for Port Listener.

Two palettes, dark and light. Neither uses the flat system grey — the dark
theme is a deep blue-slate, the light theme is a warm paper white.
"""

from __future__ import annotations

import os
import tempfile

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap

THEMES = ("dark", "light")
DEFAULT_THEME = "dark"

# The colours offered in the app's colour picker, in display order.
EDITABLE = (
    ("bg",       "Window"),
    ("surface",  "Panels"),
    ("surface2", "Buttons"),
    ("border",   "Borders"),
    ("text",     "Text"),
    ("text_dim", "Dim text"),
    ("accent",   "Accent"),
    ("term_bg",  "Terminal back"),
    ("term_fg",  "Terminal text"),
    ("tag_bg",   "Matched back"),
    ("tag_fg",   "Matched text"),
    ("ok",       "Recording"),
)

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg":         "#0d1117",   # window
        "surface":    "#151b23",   # cards / group boxes
        "surface2":   "#1d252f",   # buttons, spin buttons
        "hover":      "#25303c",
        "input":      "#0b0f14",
        "border":     "#2a3441",
        "border2":    "#3a4655",
        "text":       "#dbe4ee",
        "text_dim":   "#8896a5",
        "accent":     "#4a90e2",
        "accent_hi":  "#67a6ee",
        "on_accent":  "#ffffff",
        "term_bg":    "#080b0f",
        "term_fg":    "#d6e2ec",
        "tag_bg":     "#081310",
        "tag_fg":     "#b6e3c4",
        "tag_border": "#25453a",
        "ok":         "#4cbf6a",
        "warn":       "#d99a3e",
        "scroll":     "#2f3a47",
        "scroll_hi":  "#42505f",
    },
    "light": {
        "bg":         "#f7f4ef",   # warm paper, not grey
        "surface":    "#fffdfa",
        "surface2":   "#f0ebe3",
        "hover":      "#e6dfd4",
        "input":      "#ffffff",
        "border":     "#ddd4c7",
        "border2":    "#c8bcab",
        "text":       "#1f2933",
        "text_dim":   "#6d6459",
        "accent":     "#2563c9",
        "accent_hi":  "#1b4fa8",
        "on_accent":  "#ffffff",
        "term_bg":    "#fffdf8",
        "term_fg":    "#23282e",
        "tag_bg":     "#f2faf3",
        "tag_fg":     "#17431f",
        "tag_border": "#c6e0cb",
        "ok":         "#1a7f37",
        "warn":       "#a35d09",
        "scroll":     "#d3c9ba",
        "scroll_hi":  "#b9ac99",
    },
}


# --------------------------------------------------------------------------- #
# Check mark image for QCheckBox (stylesheets cannot draw one themselves)
# --------------------------------------------------------------------------- #

def _icon_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "port_listener_icons")
    os.makedirs(path, exist_ok=True)
    return path


def _write_check(path: str, size: int, colour: str) -> None:
    scale = size / 15.0
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour))
    pen.setWidthF(2.0 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPolyline(*[
        QPointF(x * scale, y * scale)
        for x, y in ((3.5, 7.8), (6.3, 10.6), (11.5, 4.6))
    ])
    painter.end()
    pm.save(path, "PNG")


def check_image(colour: str) -> str:
    """Return a stylesheet-safe path to a check mark PNG of the given colour."""
    tag = colour.lstrip("#")
    base = os.path.join(_icon_dir(), f"check_{tag}.png")
    if not os.path.exists(base):
        _write_check(base, 15, colour)
        _write_check(base.replace(".png", "@2x.png"), 30, colour)
    return base.replace(os.sep, "/")


def _write_arrow(path: str, size: int, colour: str, up: bool) -> None:
    """A small chevron. Qt's Fusion style ignores CSS border triangles here."""
    scale = size / 9.0
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour))
    pen.setWidthF(1.4 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    points = ((1.6, 5.9), (4.5, 3.0), (7.4, 5.9)) if up else ((1.6, 3.1), (4.5, 6.0), (7.4, 3.1))
    painter.drawPolyline(*[QPointF(x * scale, y * scale) for x, y in points])
    painter.end()
    pm.save(path, "PNG")


def arrow_image(colour: str, up: bool = False) -> str:
    """Return a stylesheet-safe path to a chevron PNG of the given colour."""
    tag = colour.lstrip("#")
    base = os.path.join(_icon_dir(), f"arrow_{'up' if up else 'down'}_{tag}.png")
    if not os.path.exists(base):
        _write_arrow(base, 9, colour, up)
        _write_arrow(base.replace(".png", "@2x.png"), 18, colour, up)
    return base.replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# Stylesheet
# --------------------------------------------------------------------------- #

def palette(theme: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """The palette for a theme, with any user colour overrides merged in."""
    colours = dict(PALETTES.get(theme, PALETTES[DEFAULT_THEME]))
    for key, value in (overrides or {}).items():
        if key in colours and isinstance(value, str) and QColor(value).isValid():
            colours[key] = value
    return colours


def stylesheet(theme: str, overrides: dict[str, str] | None = None) -> str:
    c = palette(theme, overrides)
    check = check_image(c["on_accent"])
    down = arrow_image(c["text_dim"])
    down_off = arrow_image(c["border2"])
    up = arrow_image(c["text_dim"], up=True)

    return f"""
/* ---------- base ---------- */
QWidget {{
    background: {c['bg']};
    color: {c['text']};
    font-size: 13px;
}}
QMainWindow, QDialog, QMessageBox, QFileDialog {{ background: {c['bg']}; }}
QLabel {{ background: transparent; color: {c['text']}; }}
QLabel:disabled {{ color: {c['text_dim']}; }}

QLabel#apptitle {{ font-size: 15px; font-weight: 700; letter-spacing: 0.3px; }}
QLabel#subtitle {{ color: {c['text_dim']}; }}

/* ---------- cards ---------- */
QGroupBox {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    margin-top: 15px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    background: transparent;
    color: {c['accent']};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {c['surface2']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 7px;
    padding: 6px 14px;
    font-weight: 500;
}}
QPushButton:hover  {{ background: {c['hover']}; border-color: {c['border2']}; }}
QPushButton:pressed {{ background: {c['accent']}; color: {c['on_accent']}; border-color: {c['accent']}; }}
QPushButton:disabled {{ color: {c['text_dim']}; background: {c['surface']}; border-color: {c['border']}; }}
QPushButton:checked {{
    background: {c['ok']};
    color: {c['on_accent']};
    border-color: {c['ok']};
    font-weight: 600;
}}
QPushButton#primary {{
    background: {c['accent']};
    color: {c['on_accent']};
    border-color: {c['accent']};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {c['accent_hi']}; border-color: {c['accent_hi']}; }}
QPushButton#primary:disabled {{ background: {c['surface2']}; color: {c['text_dim']}; border-color: {c['border']}; }}

/* ---------- text inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {c['input']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {c['accent']};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {c['surface2']};
    color: {c['text_dim']};
}}

/* ---------- combo box ---------- */
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: url({down});
    width: 9px; height: 9px;
    margin-right: 8px;
}}
QComboBox::down-arrow:disabled {{ image: url({down_off}); }}
QComboBox QAbstractItemView {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border2']};
    border-radius: 7px;
    padding: 4px;
    outline: none;
    selection-background-color: {c['accent']};
    selection-color: {c['on_accent']};
}}

/* ---------- spin box ---------- */
QSpinBox::up-button, QSpinBox::down-button {{
    background: {c['surface2']};
    border: none;
    width: 18px;
}}
QSpinBox::up-button {{ border-top-right-radius: 6px; }}
QSpinBox::down-button {{ border-bottom-right-radius: 6px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c['hover']}; }}
QSpinBox::up-arrow {{ image: url({up}); width: 9px; height: 9px; }}
QSpinBox::down-arrow {{ image: url({down}); width: 9px; height: 9px; }}
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled {{ image: url({down_off}); }}

/* ---------- check boxes ---------- */
QCheckBox {{ background: transparent; spacing: 7px; }}
QCheckBox:disabled {{ color: {c['text_dim']}; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {c['border2']};
    border-radius: 4px;
    background: {c['input']};
}}
QCheckBox::indicator:hover {{ border-color: {c['accent']}; }}
QCheckBox::indicator:checked {{
    background: {c['accent']};
    border-color: {c['accent']};
    image: url({check});
}}

/* ---------- monitors ---------- */
QPlainTextEdit#terminal {{
    background: {c['term_bg']};
    color: {c['term_fg']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 8px;
}}
QPlainTextEdit#tagview {{
    background: {c['tag_bg']};
    color: {c['tag_fg']};
    border: 1px solid {c['tag_border']};
    border-radius: 8px;
    padding: 8px;
}}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['scroll']}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:horizontal {{ background: {c['scroll']}; border-radius: 4px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {c['scroll_hi']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- misc ---------- */
QScrollArea {{ background: transparent; border: none; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QStatusBar {{
    background: {c['surface']};
    color: {c['text_dim']};
    border-top: 1px solid {c['border']};
}}
QStatusBar::item {{ border: none; }}
QToolTip {{
    background: {c['surface2']};
    color: {c['text']};
    border: 1px solid {c['border2']};
    border-radius: 6px;
    padding: 5px 7px;
}}
"""


def apply(app, theme: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Apply a theme to the whole application and return its palette."""
    c = palette(theme, overrides)

    # Fusion replaces the native platform style, which is what carries the
    # flat system grey; without this the stylesheet is only half honoured.
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(c["input"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(c["surface2"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(c["surface2"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(c["accent"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(c["on_accent"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface2"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(c["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(c["text_dim"]))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(c["text_dim"]))
    app.setPalette(pal)

    app.setStyleSheet(stylesheet(theme, overrides))
    return c
