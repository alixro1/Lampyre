"""
Création de visuels animés
==========================
Un "Visual" est un petit canevas (ex. 1280x720) qui contient des formes (carré, losange,
cercle, formes libres...). Chaque forme peut avoir des animations (clignotement, rotation...).

Ce fichier contient :
  - les données  : Anim, Shape, Visual   (sauvegardables en JSON)
  - le dessin    : paint_shape / Visual.paint  (utilisé par l'éditeur ET par le mapping)
  - l'interface  : CanvasView + VisualEditor (la fenêtre "Création de visuel")
"""
import math
import json
import time
from dataclasses import dataclass, field, asdict, fields

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QFormLayout, QGroupBox, QPushButton,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox, QListWidget,
    QColorDialog, QFileDialog, QScrollArea, QLayout,
)

# ----------------------------------------------------------------------------
# CATALOGUE DES ANIMATIONS : clé -> (nom affiché, aide, vitesse par défaut, quantité par défaut)
# ----------------------------------------------------------------------------
ANIM_TYPES = {
    "blink": ("Clignotement",
              "Vitesse = clignotements par seconde.\nQuantité = part du temps où la forme est allumée (0 à 1).",
              2.0, 0.5),
    "fade": ("Fondu (pulsation d'opacité)",
             "Vitesse = cycles par seconde.\nQuantité = profondeur du fondu (1 = va jusqu'à invisible).",
             1.0, 1.0),
    "rotate": ("Rotation",
               "Vitesse = tours par seconde (négatif = sens inverse).\nQuantité : inutilisée.",
               0.25, 0.0),
    "pulse": ("Pulsation (taille)",
              "Vitesse = cycles par seconde.\nQuantité = variation de taille (0.3 = ±30 %).",
              1.0, 0.3),
    "move_x": ("Va-et-vient horizontal",
               "Vitesse = cycles par seconde.\nQuantité = amplitude (fraction de la largeur du canevas).",
               0.5, 0.2),
    "move_y": ("Va-et-vient vertical",
               "Vitesse = cycles par seconde.\nQuantité = amplitude (fraction de la hauteur du canevas).",
               0.5, 0.2),
    "hue": ("Cycle de couleur",
            "Vitesse = cycles complets de l'arc-en-ciel par seconde.\n"
            "Quantité : inutilisée. (Sans effet sur le blanc, noir et gris.)",
            0.2, 0.0),
}

# Formes prêtes à l'emploi : (nom, géométrie, largeur, hauteur)  [largeur/hauteur = fraction de la hauteur du canevas]
SHAPE_PRESETS = [
    ("Carré", "square", 0.30, 0.30),
    ("Rectangle", "square", 0.50, 0.25),
    ("Losange", "diamond", 0.35, 0.35),
    ("Cercle", "circle", 0.30, 0.30),
    ("Triangle", "triangle", 0.30, 0.30),
    ("Hexagone", "hexagon", 0.30, 0.30),
    ("Étoile", "star", 0.35, 0.35),
    ("Croix", "cross", 0.30, 0.30),
    ("Flèche", "arrow", 0.40, 0.25),
]

PALETTE = ["#ff3366", "#33ccff", "#ffd23f", "#7CFF6B", "#b266ff", "#ff8c42", "#ffffff"]


# ----------------------------------------------------------------------------
# DONNÉES
# ----------------------------------------------------------------------------
@dataclass
class Anim:
    kind: str = "blink"
    speed: float = 1.0
    amount: float = 0.5
    phase: float = 0.0     # décalage dans le cycle (0 à 1) -> utile pour décaler plusieurs formes


@dataclass
class Shape:
    kind: str = "square"           # square, diamond, circle, triangle, hexagon, star, cross, arrow, custom
    name: str = "Forme"
    x: float = 0.5                 # centre, en fraction de la largeur du canevas
    y: float = 0.5                 # centre, en fraction de la hauteur du canevas
    w: float = 0.3                 # largeur  (fraction de la HAUTEUR du canevas -> un carré reste carré)
    h: float = 0.3                 # hauteur  (fraction de la hauteur du canevas)
    rotation: float = 0.0          # degrés
    fill: str = "#ff3366"
    outline: str = "#ffffff"
    outline_w: float = 0.0         # épaisseur du contour (pixels du canevas)
    filled: bool = True
    opacity: float = 1.0
    visible: bool = True
    points: list = field(default_factory=list)   # pour les formes libres (coordonnées -0.5..0.5)
    anims: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        anims = [Anim(**a) for a in d.pop("anims", [])]
        known = {f.name for f in fields(cls)}
        return cls(anims=anims, **{k: v for k, v in d.items() if k in known})


@dataclass
class Visual:
    name: str = "Nouveau visuel"
    width: int = 1280
    height: int = 720
    background: str = "#000000"
    transparent: bool = False
    shapes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        shapes = [Shape.from_dict(s) for s in d.pop("shapes", [])]
        known = {f.name for f in fields(cls)}
        return cls(shapes=shapes, **{k: v for k, v in d.items() if k in known})

    def paint(self, p, W, H, t):
        """Dessine le visuel à l'instant t (secondes) sur un QPainter, taille W x H."""
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.transparent:
            p.fillRect(0, 0, W, H, QColor(self.background))
        for sh in self.shapes:
            if sh.visible:
                paint_shape(p, sh, W, H, t)


# ----------------------------------------------------------------------------
# GÉOMÉTRIE ET ANIMATION
# ----------------------------------------------------------------------------
def unit_points(kind, custom=None):
    """Points d'une forme 'unité' centrée en (0,0), comprise dans [-0.5 ; 0.5]."""
    if kind == "square":
        return [(-.5, -.5), (.5, -.5), (.5, .5), (-.5, .5)]
    if kind == "diamond":
        return [(0, -.5), (.5, 0), (0, .5), (-.5, 0)]
    if kind == "triangle":
        return [(0, -.5), (.5, .5), (-.5, .5)]
    if kind == "hexagon":
        return [(.5 * math.cos(math.radians(60 * i)), .5 * math.sin(math.radians(60 * i))) for i in range(6)]
    if kind == "star":
        pts = []
        for i in range(10):
            r = .5 if i % 2 == 0 else .2
            a = math.radians(-90 + 36 * i)
            pts.append((r * math.cos(a), r * math.sin(a)))
        return pts
    if kind == "cross":
        t = .17
        return [(-t, -.5), (t, -.5), (t, -t), (.5, -t), (.5, t), (t, t),
                (t, .5), (-t, .5), (-t, t), (-.5, t), (-.5, -t), (-t, -t)]
    if kind == "arrow":
        return [(-.5, -.15), (.1, -.15), (.1, -.5), (.5, 0), (.1, .5), (.1, .15), (-.5, .15)]
    if kind == "circle":   # approximation (sert pour la sélection ; le dessin utilise un vrai cercle)
        return [(.5 * math.cos(math.radians(a)), .5 * math.sin(math.radians(a))) for a in range(0, 360, 6)]
    return custom or []    # "custom"


def shape_state(sh, t):
    """Calcule la position / taille / rotation / opacité / teinte de la forme à l'instant t."""
    st = {"x": sh.x, "y": sh.y, "scale": 1.0, "rot": sh.rotation, "op": sh.opacity, "hue": 0.0}
    for a in sh.anims:
        ph = t * a.speed + a.phase           # position dans le cycle (en nombre de cycles)
        s = math.sin(2 * math.pi * ph)       # oscillation entre -1 et 1
        if a.kind == "blink":
            if (ph % 1.0) >= a.amount:
                st["op"] = 0.0
        elif a.kind == "fade":
            st["op"] *= (1 - a.amount) + a.amount * (0.5 + 0.5 * s)
        elif a.kind == "rotate":
            st["rot"] += 360 * ph
        elif a.kind == "pulse":
            st["scale"] *= max(0.0, 1 + a.amount * s)
        elif a.kind == "move_x":
            st["x"] += a.amount * s
        elif a.kind == "move_y":
            st["y"] += a.amount * s
        elif a.kind == "hue":
            st["hue"] += ph
    return st


def shape_points(sh, st, W, H):
    """Points de la forme (en pixels du canevas), après taille/rotation/position."""
    sw, shh = sh.w * H * st["scale"], sh.h * H * st["scale"]
    ca, sa = math.cos(math.radians(st["rot"])), math.sin(math.radians(st["rot"]))
    out = []
    for px, py in unit_points(sh.kind, sh.points):
        x, y = px * sw, py * shh
        out.append((st["x"] * W + x * ca - y * sa, st["y"] * H + x * sa + y * ca))
    return out


def colored(hexstr, hue_shift):
    c = QColor(hexstr)
    if hue_shift:
        h = c.hsvHueF()
        if h >= 0:  # -1 = couleur sans teinte (blanc/gris/noir)
            c.setHsvF((h + hue_shift) % 1.0, c.hsvSaturationF(), c.valueF(), c.alphaF())
    return c


def paint_shape(p, sh, W, H, t):
    st = shape_state(sh, t)
    if st["op"] <= 0:
        return
    sw, shh = sh.w * H * st["scale"], sh.h * H * st["scale"]
    p.save()
    p.setOpacity(min(1.0, st["op"]))
    p.translate(st["x"] * W, st["y"] * H)
    p.rotate(st["rot"])
    if sh.filled:
        p.setBrush(colored(sh.fill, st["hue"]))
    else:
        p.setBrush(Qt.BrushStyle.NoBrush)
    if sh.outline_w > 0:
        pen = QPen(colored(sh.outline, st["hue"]), sh.outline_w)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
    else:
        p.setPen(Qt.PenStyle.NoPen)
    if sh.kind == "circle":
        p.drawEllipse(QPointF(0, 0), sw / 2, shh / 2)
    else:
        p.drawPolygon(QPolygonF([QPointF(px * sw, py * shh) for px, py in unit_points(sh.kind, sh.points)]))
    p.restore()


# ----------------------------------------------------------------------------
# ZONE D'APERÇU (clic pour sélectionner, glisser pour déplacer, mode dessin de forme libre)
# ----------------------------------------------------------------------------
class CanvasView(QWidget):
    shape_selected = Signal(int)
    shape_edited = Signal()
    shape_drawn = Signal(list)
    drawing_changed = Signal(bool)

    def __init__(self, visual):
        super().__init__()
        self.visual = visual
        self.selected = -1
        self.t0 = time.perf_counter()
        self.drawing = False
        self.draw_points = []
        self.mouse_pos = None
        self._drag_last = None
        self.setMinimumSize(520, 320)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def now(self):
        return time.perf_counter() - self.t0

    def canvas_rect(self):
        m = 12
        aw, ah = self.width() - 2 * m, self.height() - 2 * m
        vw, vh = self.visual.width, self.visual.height
        s = min(aw / vw, ah / vh)
        w, h = vw * s, vh * s
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def to_norm(self, pos):
        r = self.canvas_rect()
        return (pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height()

    # --- dessin ---
    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        r = self.canvas_rect()
        vw, vh = self.visual.width, self.visual.height
        t = self.now()

        p.save()
        p.setClipRect(r)
        p.fillRect(r, QColor("#3a3a3a"))        # visible si le fond est transparent
        p.translate(r.x(), r.y())
        p.scale(r.width() / vw, r.height() / vh)
        self.visual.paint(p, vw, vh, t)
        p.restore()

        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#777777"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

        # contour de la forme sélectionnée
        if 0 <= self.selected < len(self.visual.shapes):
            sh = self.visual.shapes[self.selected]
            pts = shape_points(sh, shape_state(sh, t), vw, vh)
            sx, sy = r.width() / vw, r.height() / vh
            poly = QPolygonF([QPointF(r.x() + x * sx, r.y() + y * sy) for x, y in pts])
            pen = QPen(QColor(0, 255, 120), 1.5)
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPolygon(poly)

        # mode "forme libre" en cours
        if self.drawing:
            pts = [QPointF(r.x() + x * r.width(), r.y() + y * r.height()) for x, y in self.draw_points]
            p.setPen(QPen(QColor(255, 220, 0), 2))
            for i in range(len(pts) - 1):
                p.drawLine(pts[i], pts[i + 1])
            if self.mouse_pos is not None and pts:
                pen = QPen(QColor(255, 220, 0, 130), 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(pts[-1], self.mouse_pos)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 220, 0))
            for q in pts:
                p.drawEllipse(q, 4, 4)

    # --- mode dessin ---
    def start_drawing(self):
        self.drawing = True
        self.draw_points = []
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocus()
        self.drawing_changed.emit(True)

    def finish_drawing(self, cancel=False):
        pts = self.draw_points
        self.drawing = False
        self.draw_points = []
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.drawing_changed.emit(False)
        if not cancel and len(pts) >= 3:
            self.shape_drawn.emit(pts)
        self.update()

    # --- souris ---
    def mousePressEvent(self, e):
        nx, ny = self.to_norm(e.position())
        if self.drawing:
            if e.button() == Qt.MouseButton.LeftButton:
                self.draw_points.append((nx, ny))
            elif e.button() == Qt.MouseButton.RightButton:
                self.finish_drawing()
            self.update()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        vw, vh = self.visual.width, self.visual.height
        pt = QPointF(nx * vw, ny * vh)
        t = self.now()
        hit = -1
        for i in reversed(range(len(self.visual.shapes))):
            sh = self.visual.shapes[i]
            if not sh.visible:
                continue
            poly = QPolygonF([QPointF(x, y) for x, y in shape_points(sh, shape_state(sh, t), vw, vh)])
            if poly.containsPoint(pt, Qt.FillRule.OddEvenFill):
                hit = i
                break
        self.selected = hit
        self.shape_selected.emit(hit)
        self._drag_last = (nx, ny) if hit >= 0 else None
        self.update()

    def mouseMoveEvent(self, e):
        self.mouse_pos = e.position()
        if self._drag_last and 0 <= self.selected < len(self.visual.shapes):
            nx, ny = self.to_norm(e.position())
            sh = self.visual.shapes[self.selected]
            sh.x += nx - self._drag_last[0]
            sh.y += ny - self._drag_last[1]
            self._drag_last = (nx, ny)
            self.shape_edited.emit()

    def mouseReleaseEvent(self, e):
        self._drag_last = None

    def keyPressEvent(self, e):
        if self.drawing:
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.finish_drawing()
                return
            if e.key() == Qt.Key.Key_Escape:
                self.finish_drawing(cancel=True)
                return
        super().keyPressEvent(e)


# ----------------------------------------------------------------------------
# FENÊTRE "CRÉATION DE VISUEL"
# ----------------------------------------------------------------------------
def dspin(lo, hi, step, decimals=2):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    return s


def style_color_button(btn, hexstr):
    btn.setText(hexstr)
    btn.setStyleSheet(f"background-color: {hexstr}; color: {'#000' if QColor(hexstr).lightness() > 128 else '#fff'};")


class VisualEditor(QWidget):
    send_to_mapping = Signal(object)    # émet le Visual à ajouter au mapping

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Création de visuel")
        self.resize(1250, 780)
        self.visual = Visual()
        self._loading = False
        self._color_idx = 0

        self.canvas = CanvasView(self.visual)
        self.canvas.shape_selected.connect(self._on_canvas_selected)
        self.canvas.shape_edited.connect(self._sync_position_widgets)
        self.canvas.shape_drawn.connect(self._on_shape_drawn)
        self.canvas.drawing_changed.connect(self._on_drawing_changed)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.canvas.update)

        root = QHBoxLayout(self)
        root.addWidget(self.canvas, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(390)
        scroll.setWidget(self._build_panel())
        root.addWidget(scroll)

        self.set_visual(Visual())

    # ------------------------------------------------------------ construction de l'interface
    def _build_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)

        # --- réglages du visuel ---
        self.ed_vname = QLineEdit()
        self.sp_vw = QSpinBox()
        self.sp_vh = QSpinBox()
        for s in (self.sp_vw, self.sp_vh):
            s.setRange(16, 7680)
            s.setKeyboardTracking(False)
        self.btn_bg = QPushButton()
        self.chk_transp = QCheckBox("Fond transparent")
        self.ed_vname.textEdited.connect(self._apply_visual_settings)
        self.sp_vw.valueChanged.connect(self._apply_visual_settings)
        self.sp_vh.valueChanged.connect(self._apply_visual_settings)
        self.chk_transp.toggled.connect(self._apply_visual_settings)
        self.btn_bg.clicked.connect(self._pick_bg)
        g1 = QGroupBox("Visuel")
        f1 = QFormLayout(g1)
        f1.addRow("Nom", self.ed_vname)
        f1.addRow("Largeur (px)", self.sp_vw)
        f1.addRow("Hauteur (px)", self.sp_vh)
        f1.addRow("Fond", self.btn_bg)
        f1.addRow(self.chk_transp)
        lay.addWidget(g1)

        # --- ajout de formes ---
        g2 = QGroupBox("Ajouter une forme")
        grid = QGridLayout(g2)
        for i, (label, kind, w, h) in enumerate(SHAPE_PRESETS):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, l=label, k=kind, w=w, h=h: self._add_shape(l, k, w, h))
            grid.addWidget(b, i // 3, i % 3)
        b_free = QPushButton("✏ Forme libre (dessiner des points)…")
        b_free.clicked.connect(self.canvas.start_drawing)
        grid.addWidget(b_free, (len(SHAPE_PRESETS) + 2) // 3, 0, 1, 3)
        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        grid.addWidget(self.lbl_status, (len(SHAPE_PRESETS) + 2) // 3 + 1, 0, 1, 3)
        lay.addWidget(g2)

        # --- liste des formes ---
        g3 = QGroupBox("Formes (la dernière est au-dessus)")
        v3 = QVBoxLayout(g3)
        self.list_shapes = QListWidget()
        self.list_shapes.setMinimumHeight(110)
        self.list_shapes.currentRowChanged.connect(self._on_shape_row_changed)
        v3.addWidget(self.list_shapes)
        row = QHBoxLayout()
        for text, fn in (("Dupliquer", self._dup_shape), ("Supprimer", self._del_shape),
                         ("▲", lambda: self._move_shape(1)), ("▼", lambda: self._move_shape(-1))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        v3.addLayout(row)
        lay.addWidget(g3)

        # --- propriétés ---
        self.grp_props = QGroupBox("Propriétés de la forme")
        f = QFormLayout(self.grp_props)
        self.ed_name = QLineEdit()
        self.sp_x = dspin(-1, 2, 0.01, 3)
        self.sp_y = dspin(-1, 2, 0.01, 3)
        self.sp_w = dspin(0.005, 5, 0.01, 3)
        self.sp_h = dspin(0.005, 5, 0.01, 3)
        self.sp_rot = dspin(-360, 360, 5, 1)
        self.sp_op = dspin(0, 1, 0.05)
        self.chk_filled = QCheckBox("Remplie")
        self.btn_fill = QPushButton()
        self.btn_outline = QPushButton()
        self.sp_outw = dspin(0, 100, 1, 1)
        self.chk_vis = QCheckBox("Visible")
        self.ed_name.textEdited.connect(self._apply_props)
        for s in (self.sp_x, self.sp_y, self.sp_w, self.sp_h, self.sp_rot, self.sp_op, self.sp_outw):
            s.valueChanged.connect(self._apply_props)
        self.chk_filled.toggled.connect(self._apply_props)
        self.chk_vis.toggled.connect(self._apply_props)
        self.btn_fill.clicked.connect(lambda: self._pick_shape_color("fill", self.btn_fill))
        self.btn_outline.clicked.connect(lambda: self._pick_shape_color("outline", self.btn_outline))
        f.addRow("Nom", self.ed_name)
        f.addRow("Position X", self.sp_x)
        f.addRow("Position Y", self.sp_y)
        f.addRow("Largeur", self.sp_w)
        f.addRow("Hauteur", self.sp_h)
        f.addRow("Rotation (°)", self.sp_rot)
        f.addRow("Opacité", self.sp_op)
        f.addRow(self.chk_filled)
        f.addRow("Couleur", self.btn_fill)
        f.addRow("Contour", self.btn_outline)
        f.addRow("Épaisseur contour", self.sp_outw)
        f.addRow(self.chk_vis)
        lay.addWidget(self.grp_props)

        # --- animations ---
        self.grp_anims = QGroupBox("Animations de la forme")
        v4 = QVBoxLayout(self.grp_anims)
        self.list_anims = QListWidget()
        self.list_anims.setMaximumHeight(90)
        self.list_anims.currentRowChanged.connect(lambda _: self._load_anim())
        v4.addWidget(self.list_anims)
        self.combo_anim = QComboBox()
        for key, (label, *_rest) in ANIM_TYPES.items():
            self.combo_anim.addItem(label, key)
        b_add_anim = QPushButton("Ajouter")
        b_add_anim.clicked.connect(self._add_anim)
        r2 = QHBoxLayout()
        r2.addWidget(self.combo_anim, 1)
        r2.addWidget(b_add_anim)
        v4.addLayout(r2)
        self.grp_anim_params = QWidget()
        f2 = QFormLayout(self.grp_anim_params)
        f2.setContentsMargins(0, 0, 0, 0)
        self.sp_speed = dspin(-20, 20, 0.1, 2)
        self.sp_amount = dspin(0, 5, 0.05, 2)
        self.sp_phase = dspin(0, 1, 0.05, 2)
        for s in (self.sp_speed, self.sp_amount, self.sp_phase):
            s.valueChanged.connect(self._apply_anim)
        f2.addRow("Vitesse", self.sp_speed)
        f2.addRow("Quantité", self.sp_amount)
        f2.addRow("Décalage (0–1)", self.sp_phase)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color: #999;")
        f2.addRow(self.lbl_hint)
        b_del_anim = QPushButton("Supprimer l'animation")
        b_del_anim.clicked.connect(self._del_anim)
        f2.addRow(b_del_anim)
        v4.addWidget(self.grp_anim_params)
        lay.addWidget(self.grp_anims)

        # --- actions ---
        b_send = QPushButton("➕ Ajouter au mapping")
        b_send.setStyleSheet("font-weight: bold; padding: 8px;")
        b_send.clicked.connect(self._send)
        lay.addWidget(b_send)
        row3 = QHBoxLayout()
        for text, fn in (("Nouveau", self._new), ("Ouvrir…", self._open), ("Sauvegarder…", self._save)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row3.addWidget(b)
        lay.addLayout(row3)
        lay.addStretch(1)
        return panel

    # ------------------------------------------------------------ cycle de vie
    def showEvent(self, e):
        self.timer.start(33)
        super().showEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def set_visual(self, visual):
        self.visual = visual
        self.canvas.visual = visual
        self.canvas.selected = -1
        self._loading = True
        self.ed_vname.setText(visual.name)
        self.sp_vw.setValue(visual.width)
        self.sp_vh.setValue(visual.height)
        self.chk_transp.setChecked(visual.transparent)
        style_color_button(self.btn_bg, visual.background)
        self._loading = False
        self._refresh_shapes(select=0 if visual.shapes else -1)

    # ------------------------------------------------------------ réglages du visuel
    def _apply_visual_settings(self, *_):
        if self._loading:
            return
        v = self.visual
        v.name = self.ed_vname.text()
        v.width = self.sp_vw.value()
        v.height = self.sp_vh.value()
        v.transparent = self.chk_transp.isChecked()

    def _pick_bg(self):
        c = QColorDialog.getColor(QColor(self.visual.background), self, "Couleur de fond")
        if c.isValid():
            self.visual.background = c.name()
            style_color_button(self.btn_bg, c.name())

    # ------------------------------------------------------------ liste des formes
    def _label(self, sh):
        return sh.name + ("" if sh.visible else "  (masquée)")

    def _current_shape(self):
        i = self.list_shapes.currentRow()
        return self.visual.shapes[i] if 0 <= i < len(self.visual.shapes) else None

    def _refresh_shapes(self, select=-1):
        self.list_shapes.blockSignals(True)
        self.list_shapes.clear()
        for sh in self.visual.shapes:
            self.list_shapes.addItem(self._label(sh))
        self.list_shapes.setCurrentRow(select)
        self.list_shapes.blockSignals(False)
        self._on_shape_row_changed(self.list_shapes.currentRow())

    def _on_shape_row_changed(self, row):
        self.canvas.selected = row
        self._load_props()

    def _on_canvas_selected(self, idx):
        self.list_shapes.setCurrentRow(idx)

    def _add_shape(self, label, kind, w, h):
        color = PALETTE[self._color_idx % len(PALETTE)]
        self._color_idx += 1
        n = sum(1 for s in self.visual.shapes if s.name.startswith(label)) + 1
        sh = Shape(kind=kind, name=f"{label} {n}", w=w, h=h, fill=color, outline="#ffffff")
        self.visual.shapes.append(sh)
        self._refresh_shapes(select=len(self.visual.shapes) - 1)

    def _on_drawing_changed(self, drawing):
        self.lbl_status.setText(
            "Clique sur l'aperçu pour placer les points.\nClic droit ou Entrée = terminer · Échap = annuler"
            if drawing else "")

    def _on_shape_drawn(self, pts):
        vw, vh = self.visual.width, self.visual.height
        xs, ys = [p[0] * vw for p in pts], [p[1] * vh for p in pts]
        bw, bh = max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0)
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
        unit = [[(x - cx) / bw, (y - cy) / bh] for x, y in zip(xs, ys)]
        color = PALETTE[self._color_idx % len(PALETTE)]
        self._color_idx += 1
        n = sum(1 for s in self.visual.shapes if s.kind == "custom") + 1
        sh = Shape(kind="custom", name=f"Forme libre {n}", x=cx / vw, y=cy / vh,
                   w=bw / vh, h=bh / vh, points=unit, fill=color)
        self.visual.shapes.append(sh)
        self._refresh_shapes(select=len(self.visual.shapes) - 1)

    def _dup_shape(self):
        sh = self._current_shape()
        if not sh:
            return
        new = Shape.from_dict(asdict(sh))
        new.name += " (copie)"
        new.x += 0.05
        new.y += 0.05
        self.visual.shapes.append(new)
        self._refresh_shapes(select=len(self.visual.shapes) - 1)

    def _del_shape(self):
        i = self.list_shapes.currentRow()
        if 0 <= i < len(self.visual.shapes):
            del self.visual.shapes[i]
            self._refresh_shapes(select=min(i, len(self.visual.shapes) - 1))

    def _move_shape(self, delta):
        """delta = +1 : monte (vers le dessus), -1 : descend."""
        i = self.list_shapes.currentRow()
        j = i + delta
        if 0 <= i < len(self.visual.shapes) and 0 <= j < len(self.visual.shapes):
            sh = self.visual.shapes
            sh[i], sh[j] = sh[j], sh[i]
            self._refresh_shapes(select=j)

    # ------------------------------------------------------------ propriétés de la forme
    def _load_props(self):
        sh = self._current_shape()
        self.grp_props.setEnabled(sh is not None)
        self.grp_anims.setEnabled(sh is not None)
        self._loading = True
        if sh:
            self.ed_name.setText(sh.name)
            self.sp_x.setValue(sh.x)
            self.sp_y.setValue(sh.y)
            self.sp_w.setValue(sh.w)
            self.sp_h.setValue(sh.h)
            self.sp_rot.setValue(sh.rotation)
            self.sp_op.setValue(sh.opacity)
            self.chk_filled.setChecked(sh.filled)
            self.chk_vis.setChecked(sh.visible)
            self.sp_outw.setValue(sh.outline_w)
            style_color_button(self.btn_fill, sh.fill)
            style_color_button(self.btn_outline, sh.outline)
        self._loading = False
        self._refresh_anims()

    def _apply_props(self, *_):
        sh = self._current_shape()
        if sh is None or self._loading:
            return
        sh.name = self.ed_name.text()
        sh.x, sh.y = self.sp_x.value(), self.sp_y.value()
        sh.w, sh.h = self.sp_w.value(), self.sp_h.value()
        sh.rotation = self.sp_rot.value()
        sh.opacity = self.sp_op.value()
        sh.filled = self.chk_filled.isChecked()
        sh.visible = self.chk_vis.isChecked()
        sh.outline_w = self.sp_outw.value()
        item = self.list_shapes.currentItem()
        if item:
            item.setText(self._label(sh))

    def _sync_position_widgets(self):
        """Appelé quand on déplace la forme à la souris : met à jour X/Y."""
        sh = self._current_shape()
        if sh is None:
            return
        self._loading = True
        self.sp_x.setValue(sh.x)
        self.sp_y.setValue(sh.y)
        self._loading = False

    def _pick_shape_color(self, attr, btn):
        sh = self._current_shape()
        if sh is None:
            return
        c = QColorDialog.getColor(QColor(getattr(sh, attr)), self, "Choisir une couleur")
        if c.isValid():
            setattr(sh, attr, c.name())
            style_color_button(btn, c.name())

    # ------------------------------------------------------------ animations
    def _current_anim(self):
        sh = self._current_shape()
        i = self.list_anims.currentRow()
        return sh.anims[i] if sh and 0 <= i < len(sh.anims) else None

    def _refresh_anims(self, select=None):
        sh = self._current_shape()
        self.list_anims.blockSignals(True)
        self.list_anims.clear()
        if sh:
            for a in sh.anims:
                self.list_anims.addItem(ANIM_TYPES[a.kind][0])
            if select is not None and 0 <= select < len(sh.anims):
                self.list_anims.setCurrentRow(select)
            elif sh.anims:
                self.list_anims.setCurrentRow(0)
        self.list_anims.blockSignals(False)
        self._load_anim()

    def _load_anim(self):
        a = self._current_anim()
        self.grp_anim_params.setEnabled(a is not None)
        self._loading = True
        if a:
            self.sp_speed.setValue(a.speed)
            self.sp_amount.setValue(a.amount)
            self.sp_phase.setValue(a.phase)
            self.lbl_hint.setText(ANIM_TYPES[a.kind][1])
        else:
            self.lbl_hint.setText("")
        self._loading = False

    def _apply_anim(self, *_):
        a = self._current_anim()
        if a is None or self._loading:
            return
        a.speed = self.sp_speed.value()
        a.amount = self.sp_amount.value()
        a.phase = self.sp_phase.value()

    def _add_anim(self):
        sh = self._current_shape()
        if sh is None:
            return
        kind = self.combo_anim.currentData()
        _, _, speed, amount = ANIM_TYPES[kind]
        sh.anims.append(Anim(kind, speed, amount, 0.0))
        self._refresh_anims(select=len(sh.anims) - 1)

    def _del_anim(self):
        sh = self._current_shape()
        i = self.list_anims.currentRow()
        if sh and 0 <= i < len(sh.anims):
            del sh.anims[i]
            self._refresh_anims(select=min(i, len(sh.anims) - 1))

    # ------------------------------------------------------------ actions du bas
    def _send(self):
        self.send_to_mapping.emit(self.visual)
        self.lbl_status.setText("Ajouté au mapping ✔  Les modifications faites ici s'y affichent en direct.")

    def _new(self):
        self.set_visual(Visual())

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Sauvegarder le visuel",
                                              f"{self.visual.name}.visual.json", "Visuel (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.visual.to_dict(), f, indent=2)

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir un visuel", "", "Visuel (*.json)")
        if path:
            with open(path, encoding="utf-8") as f:
                self.set_visual(Visual.from_dict(json.load(f)))
