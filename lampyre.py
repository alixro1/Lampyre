"""
Mapping + VJ  (V3)
==================
Dépendances :  pip install PySide6 opencv-python numpy
Fichiers    :  mapper.py (ce fichier) + visuals.py (visuels animés) + vj.py (mode VJ), dans le même dossier.

Deux onglets, UNE seule fenêtre de sortie (le projecteur) :
  - onglet Mapping : surfaces (image / GIF / vidéo / visuel) déformées sur 4 coins
  - onglet VJ      : scènes vidéo synchronisées, envoyées plein écran

Raccourcis (fonctionnent dans la fenêtre de contrôle ET dans la fenêtre de sortie) :
  E = mode édition          L = verrouiller / déverrouiller la surface sélectionnée
  1 à 9 = envoyer la scène n° 1 à 9 (onglet VJ)
Dans la sortie (mapping) : flèches = déplacer le coin/la surface de 1 px (Maj = 10 px), Échap = fermer.
"""
import sys
import json
import time
import math
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, Signal
from PySide6.QtGui import QImage, QMovie, QPainter, QPen, QColor, QPolygonF, QTransform, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton,
    QComboBox, QLabel, QSlider, QCheckBox, QFileDialog, QMessageBox, QGroupBox, QSpinBox,
    QColorDialog, QScrollArea, QLayout, QTabWidget,
)

from visuals import Visual, VisualEditor
from vj import VjEngine, VjPanel, draw_vj_frame

VIDEO_EXT = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


# ----------------------------------------------------------------------------
# MÉDIAS : chacun fournit une image courante (self.image, une QImage)
#          et une méthode update(now) appelée ~30 fois par seconde
# ----------------------------------------------------------------------------
class Media:
    image = None

    def update(self, now):
        pass


class StillMedia(Media):
    """Image fixe (png, jpg...)."""
    def __init__(self, path):
        img = QImage(path)
        if img.isNull():
            raise ValueError(f"Impossible d'ouvrir l'image : {path}")
        self.image = img


class GifMedia(Media):
    """GIF animé, joué en boucle (QMovie gère le timing et la transparence)."""
    def __init__(self, path):
        self.movie = QMovie(path)
        if not self.movie.isValid():
            raise ValueError(f"Impossible d'ouvrir le GIF : {path}")
        self.movie.setCacheMode(QMovie.CacheMode.CacheAll)
        self.movie.start()
        self.movie.jumpToFrame(0)
        self.image = self.movie.currentImage()

    def update(self, now):
        # certains GIF ne bouclent pas : on relance quand c'est terminé
        if self.movie.state() == QMovie.MovieState.NotRunning:
            self.movie.start()
        img = self.movie.currentImage()
        if not img.isNull():
            self.image = img


class VideoMedia(Media):
    """Vidéo lue avec OpenCV, en boucle."""
    def __init__(self, path):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise ValueError(f"Impossible d'ouvrir la vidéo : {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.period = 1 / fps if fps and fps > 1 else 1 / 25
        self.last = 0.0
        self._read_frame()

    def _read_frame(self):
        ok, frame = self.cap.read()
        if not ok:  # fin de la vidéo -> on boucle
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        if ok:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, _ = frame.shape
            self.image = QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    def update(self, now):
        if now - self.last >= self.period:
            self._read_frame()
            self.last = now if now - self.last > 2 * self.period else self.last + self.period


class VisualMedia(Media):
    """Visuel généré par l'éditeur (formes + animations). Dessiné ~30 fois/s dans une QImage."""
    FPS = 30

    def __init__(self, visual):
        self.visual = visual
        self.t0 = time.perf_counter()
        self.last = 0.0
        self.render(0.0)

    def render(self, t):
        v = self.visual
        if self.image is None or self.image.width() != v.width or self.image.height() != v.height:
            self.image = QImage(v.width, v.height, QImage.Format.Format_ARGB32_Premultiplied)
        self.image.fill(Qt.GlobalColor.transparent)
        p = QPainter(self.image)
        v.paint(p, v.width, v.height, t)
        p.end()

    def update(self, now):
        if now - self.last >= 1 / self.FPS:
            self.render(now - self.t0)
            self.last = now


def make_media(path):
    low = path.lower()
    if low.endswith(VIDEO_EXT):
        return VideoMedia(path)
    if low.endswith(".gif"):
        return GifMedia(path)
    return StillMedia(path)


# ----------------------------------------------------------------------------
# SURFACE : un média + 4 coins (en coordonnées normalisées 0..1 de la sortie)
# Ordre des coins : haut-gauche, haut-droit, bas-droit, bas-gauche
# ----------------------------------------------------------------------------
@dataclass
class Surface:
    name: str
    path: str
    points: list = field(default_factory=lambda: [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]])
    opacity: float = 1.0
    visible: bool = True
    locked: bool = False     # verrouillée : plus de déplacement possible
    media: Media = None      # pas sauvegardé tel quel dans le projet

    def to_dict(self):
        d = {"name": self.name, "path": self.path, "points": self.points,
             "opacity": self.opacity, "visible": self.visible, "locked": self.locked}
        if isinstance(self.media, VisualMedia):   # un visuel est sauvegardé en entier dans le projet
            d["visual"] = self.media.visual.to_dict()
        return d


# ----------------------------------------------------------------------------
# FENÊTRE DE SORTIE : ce qui est envoyé au projecteur (mode "mapping" ou mode "vj")
# ----------------------------------------------------------------------------
class OutputWindow(QWidget):
    selection_changed = Signal(int)
    edit_toggled = Signal(bool)
    lock_requested = Signal()
    scene_requested = Signal(int)
    HANDLE_RADIUS = 10

    def __init__(self, surfaces, engine):
        super().__init__()
        self.setWindowTitle("Sortie projecteur")
        self.surfaces = surfaces
        self.engine = engine
        self.mode = "mapping"
        self.selected = -1
        self.active_corner = None     # dernier coin attrapé (pour les flèches du clavier)
        self.edit_mode = True
        # --- mire ---
        self.show_grid = False
        self.grid_mode = "px"         # "px" = taille des cases en pixels, "cols" = nombre de colonnes
        self.grid_value = 100
        self.grid_thickness = 1
        self.grid_color = "#ffffff"
        self._drag = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Raccourcis : QShortcut marche dès que la fenêtre est active (plus fiable que keyPressEvent)
        QShortcut(QKeySequence("E"), self).activated.connect(self._shortcut_edit)
        QShortcut(QKeySequence("L"), self).activated.connect(self.lock_requested.emit)
        for i in range(9):
            QShortcut(QKeySequence(str(i + 1)), self).activated.connect(lambda i=i: self.scene_requested.emit(i))

    # --- mode ---
    def set_mode(self, mode):
        self.mode = mode
        self._update_cursor()
        self.update()

    def set_edit_mode(self, on):
        self.edit_mode = on
        self._update_cursor()
        self.update()

    def _update_cursor(self):
        visible = self.mode == "mapping" and self.edit_mode
        self.setCursor(Qt.CursorShape.ArrowCursor if visible else Qt.CursorShape.BlankCursor)

    def _shortcut_edit(self):
        if self.mode == "mapping":
            self.set_edit_mode(not self.edit_mode)
            self.edit_toggled.emit(self.edit_mode)

    # --- dessin ---
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        W, H = self.width(), self.height()

        if self.mode == "vj":
            draw_vj_frame(p, QRectF(0, 0, W, H), self.engine, time.perf_counter())
            return

        p.fillRect(self.rect(), Qt.GlobalColor.black)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1) les médias déformés
        for s in self.surfaces:
            if not s.visible or s.media is None or s.media.image is None or s.media.image.isNull():
                continue
            img = s.media.image
            src = np.float32([[0, 0], [img.width(), 0], [img.width(), img.height()], [0, img.height()]])
            dst = np.float32([[x * W, y * H] for x, y in s.points])
            try:
                h = cv2.getPerspectiveTransform(src, dst)
            except cv2.error:
                continue  # coins alignés / dégénérés
            # Qt utilise la convention "ligne" -> on passe la transposée de h
            t = QTransform(h[0, 0], h[1, 0], h[2, 0],
                           h[0, 1], h[1, 1], h[2, 1],
                           h[0, 2], h[1, 2], h[2, 2])
            p.save()
            p.setTransform(t)
            p.setOpacity(s.opacity)
            p.drawImage(0, 0, img)
            p.restore()

        # 2) mire de calibration
        if self.show_grid:
            self._draw_grid(p, W, H)

        # 3) contours + poignées en mode édition
        if self.edit_mode:
            for i, s in enumerate(self.surfaces):
                if not s.visible:
                    continue
                is_sel = (i == self.selected)
                poly = QPolygonF([QPointF(x * W, y * H) for x, y in s.points])
                p.setBrush(Qt.BrushStyle.NoBrush)
                if s.locked:
                    # verrouillée : contour orange en pointillés, pas de poignées
                    pen = QPen(QColor(255, 170, 0) if is_sel else QColor(150, 105, 40), 2)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    p.setPen(pen)
                    p.drawPolygon(poly)
                    continue
                color = QColor(0, 255, 120) if is_sel else QColor(120, 120, 255)
                p.setPen(QPen(color, 2))
                p.drawPolygon(poly)
                for c, (x, y) in enumerate(s.points):
                    # le coin actif (celui des flèches du clavier) est en blanc
                    p.setBrush(QColor(255, 255, 255) if (is_sel and c == self.active_corner) else color)
                    p.drawEllipse(QPointF(x * W, y * H), self.HANDLE_RADIUS, self.HANDLE_RADIUS)

    def _draw_grid(self, p, W, H):
        """Quadrillage centré. Cases carrées : taille en pixels OU nombre de colonnes."""
        cell = self.grid_value if self.grid_mode == "px" else W / max(1, self.grid_value)
        cell = max(4.0, float(cell))
        col = QColor(self.grid_color)
        cx, cy = W / 2, H / 2
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(QPen(col, self.grid_thickness))
        for i in range(-int(cx // cell) - 1, int(cx // cell) + 2):
            x = cx + i * cell
            p.drawLine(QPointF(x, 0), QPointF(x, H))
        for i in range(-int(cy // cell) - 1, int(cy // cell) + 2):
            y = cy + i * cell
            p.drawLine(QPointF(0, y), QPointF(W, y))
        # axes du centre un peu plus épais
        p.setPen(QPen(col, self.grid_thickness * 2 + 1))
        p.drawLine(QPointF(cx, 0), QPointF(cx, H))
        p.drawLine(QPointF(0, cy), QPointF(W, cy))
        # bordure rouge = limites de l'image du projecteur
        p.setPen(QPen(QColor(255, 0, 0), 3))
        p.drawRect(1, 1, W - 3, H - 3)
        p.restore()

    # --- souris ---
    def select(self, idx):
        if idx != self.selected:
            self.selected = idx
            self.active_corner = None
            self.selection_changed.emit(idx)
        self.update()

    def mousePressEvent(self, e):
        self._drag = None
        if self.mode != "mapping" or not self.edit_mode or e.button() != Qt.MouseButton.LeftButton:
            return
        W, H = self.width(), self.height()
        pos = e.position()
        n = len(self.surfaces)
        # on teste d'abord la surface sélectionnée, puis les autres
        order = ([self.selected] if 0 <= self.selected < n else []) + \
                [i for i in reversed(range(n)) if i != self.selected]

        # 1) clic sur un coin d'une surface NON verrouillée ?
        for idx in order:
            s = self.surfaces[idx]
            if s.locked:
                continue
            for c, (x, y) in enumerate(s.points):
                if math.hypot(x * W - pos.x(), y * H - pos.y()) < self.HANDLE_RADIUS + 8:
                    self._drag = ("corner", idx, c)
                    self.select(idx)
                    self.active_corner = c
                    return
        # 2) clic à l'intérieur d'une surface ? D'abord les non verrouillées (déplacement),
        #    puis les verrouillées (sélection seulement : elles sont "transparentes" au clic)
        for locked_pass in (False, True):
            for idx in order:
                s = self.surfaces[idx]
                if s.locked != locked_pass:
                    continue
                poly = QPolygonF([QPointF(x * W, y * H) for x, y in s.points])
                if poly.containsPoint(pos, Qt.FillRule.OddEvenFill):
                    self._drag = None if s.locked else ("move", idx, pos)
                    self.select(idx)
                    self.active_corner = None
                    return
        self.select(-1)

    def mouseMoveEvent(self, e):
        if not self._drag:
            return
        W, H = self.width(), self.height()
        pos = e.position()
        kind, idx, extra = self._drag
        s = self.surfaces[idx]
        if kind == "corner":
            s.points[extra] = [pos.x() / W, pos.y() / H]
        else:  # move
            dx, dy = (pos.x() - extra.x()) / W, (pos.y() - extra.y()) / H
            for pt in s.points:
                pt[0] += dx
                pt[1] += dy
            self._drag = ("move", idx, pos)
        self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    # --- clavier ---
    def _nudge(self, dx_px, dy_px):
        if not (0 <= self.selected < len(self.surfaces)):
            return
        s = self.surfaces[self.selected]
        if s.locked:
            return
        dx, dy = dx_px / self.width(), dy_px / self.height()
        pts = [s.points[self.active_corner]] if self.active_corner is not None else s.points
        for pt in pts:
            pt[0] += dx
            pt[1] += dy
        self.update()

    def keyPressEvent(self, e):
        step = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        arrows = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                  Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}
        if e.key() == Qt.Key.Key_Escape:
            self.close()
        elif e.key() in arrows and self.mode == "mapping" and self.edit_mode:
            self._nudge(*arrows[e.key()])


# ----------------------------------------------------------------------------
# PETITS OUTILS D'INTERFACE
# ----------------------------------------------------------------------------
def make_group(title, *items):
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    for it in items:
        lay.addLayout(it) if isinstance(it, QLayout) else lay.addWidget(it)
    return box


def hbox(*widgets):
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    return row


def style_color_button(btn, hexstr):
    btn.setText(hexstr)
    btn.setStyleSheet(f"background-color: {hexstr}; color: {'#000' if QColor(hexstr).lightness() > 128 else '#fff'};")


def surface_label(s):
    return ("🔒 " if s.locked else "") + s.name


# ----------------------------------------------------------------------------
# FENÊTRE DE CONTRÔLE
# ----------------------------------------------------------------------------
class ControlWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lampyre")
        self.surfaces = []
        self.editor = None   # fenêtre "Création de visuel" (créée à la demande)
        self.engine = VjEngine()
        self.output = OutputWindow(self.surfaces, self.engine)
        self.output.selection_changed.connect(self.on_output_selection)
        self.output.edit_toggled.connect(lambda on: self.chk_edit.setChecked(on))
        self.output.lock_requested.connect(self.toggle_lock_selected)
        self.output.scene_requested.connect(self.trigger_scene)
        self.vj_panel = VjPanel(self.engine)

        # --- sortie ---
        self.combo_screen = QComboBox()
        self.refresh_screens()
        btn_show = QPushButton("Afficher / masquer la sortie")
        btn_show.clicked.connect(self.toggle_output)

        # --- surfaces ---
        self.list = QListWidget()
        self.list.setMinimumHeight(130)
        self.list.currentRowChanged.connect(self.on_list_selection)
        self.list.itemDoubleClicked.connect(lambda _: self.edit_selected_visual())
        btn_add = QPushButton("Ajouter un média…")
        btn_add.clicked.connect(self.add_media)
        btn_new_visual = QPushButton("✦ Créer un visuel…")
        btn_new_visual.clicked.connect(lambda: self.open_editor())
        btn_edit_visual = QPushButton("Éditer le visuel")
        btn_edit_visual.clicked.connect(self.edit_selected_visual)
        btn_del = QPushButton("Supprimer")
        btn_del.clicked.connect(self.remove_surface)
        btn_up = QPushButton("▲ Dessus")
        btn_up.clicked.connect(lambda: self.move_surface(1))
        btn_down = QPushButton("▼ Dessous")
        btn_down.clicked.connect(lambda: self.move_surface(-1))
        btn_reset = QPushButton("Réinitialiser les coins")
        btn_reset.clicked.connect(self.reset_corners)

        self.chk_lock = QCheckBox("🔒 Verrouiller (touche L)")
        self.chk_lock.toggled.connect(self.on_lock)
        self.chk_visible = QCheckBox("Visible")
        self.chk_visible.setChecked(True)
        self.chk_visible.toggled.connect(self.on_visible)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(100)
        self.slider.valueChanged.connect(self.on_opacity)

        # --- affichage / mire ---
        self.chk_edit = QCheckBox("Mode édition (touche E)")
        self.chk_edit.setChecked(True)
        self.chk_edit.toggled.connect(self.output.set_edit_mode)

        self.chk_grid = QCheckBox("Afficher la mire")
        self.chk_grid.toggled.connect(self.on_grid_changed)
        self.combo_grid_mode = QComboBox()
        self.combo_grid_mode.addItems(["Taille des cases (px)", "Nombre de colonnes"])
        self.combo_grid_mode.currentIndexChanged.connect(self.on_grid_mode)
        self.spin_grid = QSpinBox()
        self.spin_grid.setRange(4, 2000)
        self.spin_grid.setValue(100)
        self.spin_grid.valueChanged.connect(self.on_grid_changed)
        self.spin_thick = QSpinBox()
        self.spin_thick.setRange(1, 10)
        self.spin_thick.setValue(1)
        self.spin_thick.valueChanged.connect(self.on_grid_changed)
        self.btn_grid_color = QPushButton()
        style_color_button(self.btn_grid_color, "#ffffff")
        self.btn_grid_color.clicked.connect(self.pick_grid_color)

        # --- projet ---
        btn_save = QPushButton("Sauvegarder le projet…")
        btn_save.clicked.connect(self.save_project)
        btn_load = QPushButton("Ouvrir un projet…")
        btn_load.clicked.connect(self.load_project)

        # --- onglet Mapping (dans une zone défilante) ---
        lay = QVBoxLayout()
        lay.addWidget(make_group(
            "Surfaces (double-clic = éditer un visuel)", self.list,
            hbox(btn_add, btn_new_visual), hbox(btn_edit_visual, btn_del),
            hbox(btn_up, btn_down), btn_reset,
            self.chk_lock, self.chk_visible, QLabel("Opacité :"), self.slider))
        lay.addWidget(make_group(
            "Mire de calibration", self.chk_edit, self.chk_grid, self.combo_grid_mode, self.spin_grid,
            QLabel("Épaisseur des traits (px) :"), self.spin_thick, self.btn_grid_color))
        lay.addStretch(1)
        mapping_page = QWidget()
        mapping_page.setLayout(lay)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(mapping_page)

        # --- onglets ---
        self.tabs = QTabWidget()
        self.tabs.addTab(scroll, "Mapping")
        self.tabs.addTab(self.vj_panel, "VJ")
        self.tabs.currentChanged.connect(self.on_tab_changed)

        root = QVBoxLayout()
        root.addWidget(make_group("Sortie projecteur (commune au mapping et au VJ)",
                                  hbox(self.combo_screen, btn_show)))
        root.addWidget(self.tabs, 1)
        root.addWidget(make_group("Projet (mapping + VJ)", hbox(btn_save, btn_load)))
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.resize(780, 900)

        # --- raccourcis (actifs quand la fenêtre de contrôle est active) ---
        QShortcut(QKeySequence("E"), self).activated.connect(self.chk_edit.toggle)
        QShortcut(QKeySequence("L"), self).activated.connect(self.toggle_lock_selected)
        for i in range(9):
            QShortcut(QKeySequence(str(i + 1)), self).activated.connect(lambda i=i: self.trigger_scene(i))

        # --- boucle de rendu (~30 images/s) ---
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(33)

    # --- boucle ---
    def tick(self):
        now = time.perf_counter()
        if self.tabs.currentIndex() == 0:
            for s in self.surfaces:
                if s.media:
                    s.media.update(now)
            self.engine.last_tick = None      # le VJ est "en pause" tant qu'on est sur l'onglet Mapping
        else:
            self.engine.tick(now)
            self.vj_panel.tick()
        if self.output.isVisible():
            self.output.update()

    def on_tab_changed(self, index):
        self.output.set_mode("vj" if index == 1 else "mapping")
        self.engine.last_tick = None

    def trigger_scene(self, i):
        if self.tabs.currentIndex() == 1:
            self.engine.go(i)

    # --- écran / sortie ---
    def refresh_screens(self):
        self.combo_screen.clear()
        for i, scr in enumerate(QApplication.screens()):
            g = scr.geometry()
            self.combo_screen.addItem(f"Écran {i + 1} — {g.width()}×{g.height()}  ({scr.name()})")
        # par défaut : le dernier écran (souvent le projecteur)
        self.combo_screen.setCurrentIndex(self.combo_screen.count() - 1)

    def toggle_output(self):
        if self.output.isVisible():
            self.output.close()
            return
        screens = QApplication.screens()
        scr = screens[self.combo_screen.currentIndex()]
        self.output.setScreen(scr)
        self.output.setGeometry(scr.geometry())
        self.output.showFullScreen()
        self.output.set_edit_mode(self.chk_edit.isChecked())
        self.output.set_mode("vj" if self.tabs.currentIndex() == 1 else "mapping")
        self.output.activateWindow()

    # --- mire ---
    def on_grid_mode(self, index):
        self.spin_grid.blockSignals(True)
        if index == 0:
            self.spin_grid.setRange(4, 2000)
            self.spin_grid.setValue(100)
        else:
            self.spin_grid.setRange(1, 100)
            self.spin_grid.setValue(10)
        self.spin_grid.blockSignals(False)
        self.on_grid_changed()

    def on_grid_changed(self, *_):
        o = self.output
        o.show_grid = self.chk_grid.isChecked()
        o.grid_mode = "px" if self.combo_grid_mode.currentIndex() == 0 else "cols"
        o.grid_value = self.spin_grid.value()
        o.grid_thickness = self.spin_thick.value()
        o.update()

    def pick_grid_color(self):
        c = QColorDialog.getColor(QColor(self.output.grid_color), self, "Couleur de la mire")
        if c.isValid():
            self.output.grid_color = c.name()
            style_color_button(self.btn_grid_color, c.name())

    def grid_state(self):
        o = self.output
        return {"show": o.show_grid, "mode": o.grid_mode, "value": o.grid_value,
                "thickness": o.grid_thickness, "color": o.grid_color}

    def apply_grid_state(self, d):
        self.combo_grid_mode.blockSignals(True)
        self.spin_grid.blockSignals(True)
        self.combo_grid_mode.setCurrentIndex(0 if d.get("mode", "px") == "px" else 1)
        if d.get("mode", "px") == "px":
            self.spin_grid.setRange(4, 2000)
        else:
            self.spin_grid.setRange(1, 100)
        self.spin_grid.setValue(d.get("value", 100))
        self.combo_grid_mode.blockSignals(False)
        self.spin_grid.blockSignals(False)
        self.spin_thick.setValue(d.get("thickness", 1))
        self.output.grid_color = d.get("color", "#ffffff")
        style_color_button(self.btn_grid_color, self.output.grid_color)
        self.chk_grid.setChecked(d.get("show", False))
        self.on_grid_changed()

    # --- éditeur de visuels ---
    def open_editor(self, visual=None):
        if self.editor is None:
            self.editor = VisualEditor()
            self.editor.send_to_mapping.connect(self.add_visual_surface)
        if visual is not None:
            self.editor.set_visual(visual)
        self.editor.show()
        self.editor.raise_()
        self.editor.activateWindow()

    def add_visual_surface(self, visual):
        self.add_surface(visual=visual, points=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]])

    def edit_selected_visual(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.surfaces) and isinstance(self.surfaces[row].media, VisualMedia):
            self.open_editor(self.surfaces[row].media.visual)
        else:
            QMessageBox.information(self, "Éditer le visuel",
                                    "Sélectionne d'abord une surface créée avec « Créer un visuel ».")

    # --- gestion des surfaces ---
    def add_surface(self, path="", visual=None, **kwargs):
        try:
            media = VisualMedia(visual) if visual is not None else make_media(path)
        except ValueError as err:
            QMessageBox.warning(self, "Erreur", str(err))
            return
        name = ("✦ " + visual.name) if visual is not None else Path(path).name
        s = Surface(name=name, path=path, media=media, **kwargs)
        self.surfaces.append(s)
        self.list.addItem(surface_label(s))
        self.list.setCurrentRow(len(self.surfaces) - 1)

    def add_media(self):
        exts = " ".join("*" + e for e in VIDEO_EXT + IMAGE_EXT)
        files, _ = QFileDialog.getOpenFileNames(self, "Choisir des médias", "", f"Médias ({exts})")
        for f in files:
            offset = 0.03 * len(self.surfaces)  # léger décalage pour les distinguer
            pts = [[0.2 + offset, 0.2 + offset], [0.6 + offset, 0.2 + offset],
                   [0.6 + offset, 0.6 + offset], [0.2 + offset, 0.6 + offset]]
            self.add_surface(f, points=pts)

    def remove_surface(self):
        row = self.list.currentRow()
        if row < 0:
            return
        del self.surfaces[row]
        self.list.takeItem(row)
        self.on_list_selection(self.list.currentRow())

    def move_surface(self, delta):
        """delta = +1 : vers le dessus (fin de liste), -1 : vers le dessous."""
        row = self.list.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.surfaces)):
            return
        self.surfaces[row], self.surfaces[new] = self.surfaces[new], self.surfaces[row]
        self.list.blockSignals(True)
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)
        self.list.blockSignals(False)
        self.on_list_selection(new)

    def reset_corners(self):
        row = self.list.currentRow()
        if row >= 0 and not self.surfaces[row].locked:
            self.surfaces[row].points = [[0.25, 0.25], [0.75, 0.25], [0.75, 0.75], [0.25, 0.75]]

    # --- verrouillage ---
    def on_lock(self, checked):
        row = self.list.currentRow()
        if 0 <= row < len(self.surfaces):
            s = self.surfaces[row]
            s.locked = checked
            self.list.item(row).setText(surface_label(s))
            self.output.update()

    def toggle_lock_selected(self):
        if self.tabs.currentIndex() != 0:
            return
        row = self.list.currentRow()
        if 0 <= row < len(self.surfaces):
            self.chk_lock.setChecked(not self.surfaces[row].locked)

    # --- synchro liste <-> sortie ---
    def on_list_selection(self, row):
        self.output.select(row)
        if 0 <= row < len(self.surfaces):
            s = self.surfaces[row]
            for w in (self.chk_visible, self.slider, self.chk_lock):
                w.blockSignals(True)
            self.chk_visible.setChecked(s.visible)
            self.slider.setValue(int(s.opacity * 100))
            self.chk_lock.setChecked(s.locked)
            for w in (self.chk_visible, self.slider, self.chk_lock):
                w.blockSignals(False)

    def on_output_selection(self, idx):
        self.list.blockSignals(True)
        self.list.setCurrentRow(idx)
        self.list.blockSignals(False)
        if idx >= 0:
            self.on_list_selection(idx)

    def on_visible(self, checked):
        row = self.list.currentRow()
        if row >= 0:
            self.surfaces[row].visible = checked

    def on_opacity(self, value):
        row = self.list.currentRow()
        if row >= 0:
            self.surfaces[row].opacity = value / 100

    # --- sauvegarde / chargement ---
    def save_project(self):
        path, _ = QFileDialog.getSaveFileName(self, "Sauvegarder", "projet.json", "Projet (*.json)")
        if path:
            data = {"surfaces": [s.to_dict() for s in self.surfaces],
                    "grid": self.grid_state(),
                    "vj": self.vj_panel.state_dict()}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

    def load_project(self):
        path, _ = QFileDialog.getOpenFileName(self, "Ouvrir", "", "Projet (*.json)")
        if not path:
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.surfaces.clear()
        self.list.clear()
        for d in data.get("surfaces", []):
            kw = dict(points=d["points"], opacity=d["opacity"], visible=d["visible"],
                      locked=d.get("locked", False))
            if d.get("visual"):
                self.add_surface(visual=Visual.from_dict(d["visual"]), **kw)
            else:
                self.add_surface(d["path"], **kw)
        if "grid" in data:
            self.apply_grid_state(data["grid"])
        if "vj" in data:
            self.vj_panel.apply_state(data["vj"])

    def closeEvent(self, e):
        self.output.close()
        if self.editor is not None:
            self.editor.close()
        super().closeEvent(e)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ControlWindow()
    win.show()
    sys.exit(app.exec())
