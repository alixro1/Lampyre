"""
Mode VJ
=======
Des scènes (vidéos) toutes synchronisées sur la MÊME horloge :

  - Chaque scène = une vidéo : [ intro | boucle ]   (l'intro = les N premières images)
  - Une seule "tête de lecture" globale (engine.g, en images) avance pour tout le monde.
  - Changer de scène = afficher l'autre vidéo À LA MÊME IMAGE -> les mouvements restent synchro.

Deux façons de gérer l'intro (réglable) :
  * Continu (par défaut) : la tête de lecture continue, la nouvelle scène arrive "dans le mouvement".
                           L'intro n'est jouée qu'au tout début (ou avec "Redémarrer").
  * Relancer l'intro     : à chaque changement la tête de lecture repart à 0 -> la nouvelle scène
                           joue son intro, puis sa boucle.

Option "Quantifié" : le changement est mis en attente et part au prochain début de boucle.

Fichiers : vj.py contient FrameCache, VjScene, VjEngine (la logique) + VjPreview, VjPanel (l'interface).
"""
import time
from collections import OrderedDict
from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QImage, QPainter, QPixmap, QIcon, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QPushButton, QLabel,
    QSpinBox, QDoubleSpinBox, QCheckBox, QListWidget, QListWidgetItem, QListView, QFileDialog,
    QMessageBox, QProgressDialog,
)

VIDEO_FILTER = "Vidéos (*.mp4 *.mov *.avi *.mkv *.webm *.m4v)"
MAX_W = 1280   # les images plus larges sont réduites à cette largeur (économise la RAM)


# ----------------------------------------------------------------------------
# CACHE D'IMAGES : garde les images déjà décodées en RAM (évite de re-décoder / re-chercher dans le fichier)
# ----------------------------------------------------------------------------
class FrameCache:
    def __init__(self, budget_mb=800):
        self.d = OrderedDict()
        self.bytes = 0
        self.budget = budget_mb * 1024 * 1024

    def get(self, key):
        img = self.d.get(key)
        if img is not None:
            self.d.move_to_end(key)
        return img

    def put(self, key, img):
        self.d[key] = img
        self.bytes += img.sizeInBytes()
        while self.bytes > self.budget and len(self.d) > 1:
            _, old = self.d.popitem(last=False)   # on jette la plus ancienne
            self.bytes -= old.sizeInBytes()

    def drop_scene(self, sid):
        for key in [k for k in self.d if k[0] == sid]:
            self.bytes -= self.d.pop(key).sizeInBytes()

    def clear(self):
        self.d.clear()
        self.bytes = 0


# ----------------------------------------------------------------------------
# SCÈNE : une vidéo lue "à la demande" (on lui demande l'image numéro N)
# ----------------------------------------------------------------------------
class VjScene:
    _next_id = 0

    def __init__(self, path, cache):
        self.path = path
        self.name = Path(path).stem
        self.cache = cache
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise ValueError(f"Impossible d'ouvrir la vidéo : {path}")
        self.n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if self.n <= 0:
            raise ValueError(f"Nombre d'images illisible : {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 1 else 30.0
        self.sid = VjScene._next_id
        VjScene._next_id += 1
        self.pos = -1            # numéro de la dernière image lue dans le fichier
        self.last = QImage()     # dernière image renvoyée (secours)

        # miniature = image du milieu de la vidéo
        img = self.frame(self.n // 2)
        self.thumb = QPixmap(160, 90)
        self.thumb.fill(Qt.GlobalColor.darkGray)
        if not img.isNull():
            self.thumb = QPixmap.fromImage(img).scaled(
                160, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

    def frame(self, idx):
        """Renvoie l'image numéro idx (depuis le cache si possible, sinon décodée)."""
        idx = max(0, min(idx, self.n - 1))
        img = self.cache.get((self.sid, idx))
        if img is not None:
            self.last = img
            return img
        if idx != self.pos + 1:                      # pas l'image suivante -> on doit se déplacer dans le fichier
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = self.cap.read()
        if not ok:
            self.pos = -2                            # force un nouveau déplacement au prochain appel
            return self.last
        self.pos = idx
        h, w = frame.shape[:2]
        if w > MAX_W:
            frame = cv2.resize(frame, (MAX_W, int(h * MAX_W / w)), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = QImage(frame.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.cache.put((self.sid, idx), img)
        self.last = img
        return img

    def release(self):
        self.cap.release()


# ----------------------------------------------------------------------------
# MOTEUR : horloge globale + choix de la scène affichée
# ----------------------------------------------------------------------------
class VjEngine:
    def __init__(self):
        self.scenes = []
        self.cache = FrameCache()
        self.current = -1            # scène affichée (-1 = rien)
        self.pending = -1            # scène en attente (mode quantifié)
        self.fps = 30.0              # vitesse de la tête de lecture
        self.intro = 0               # nombre d'images d'intro (identique pour toutes les scènes)
        self.g = 0.0                 # tête de lecture globale (en images)
        self.playing = True
        self.last_tick = None
        self.restart_on_switch = False
        self.quantize = False
        self.fade_ms = 0
        self.fade_start = 0.0
        self.stretch = False
        self.image = None            # image affichée en ce moment
        self.prev_image = None       # image de l'ancienne scène (figée) pour le fondu
        self.version = 0             # augmente à chaque changement d'état (pour rafraîchir l'interface)

    # --- gestion des scènes ---
    def add_scene(self, path):
        sc = VjScene(path, self.cache)       # peut lever ValueError
        if not self.scenes:
            self.fps = sc.fps
        self.scenes.append(sc)
        self.version += 1
        return sc

    def remove_scene(self, i):
        sc = self.scenes.pop(i)
        sc.release()
        self.cache.drop_scene(sc.sid)
        if i == self.current:
            self.current, self.image, self.prev_image = -1, None, None
        elif i < self.current:
            self.current -= 1
        if self.pending == i:
            self.pending = -1
        elif self.pending > i:
            self.pending -= 1
        self.version += 1

    def clear_scenes(self):
        for sc in self.scenes:
            sc.release()
        self.scenes = []
        self.cache.clear()
        self.current = self.pending = -1
        self.image = self.prev_image = None
        self.g = 0.0
        self.version += 1

    # --- changement de scène ---
    def go(self, i):
        if not 0 <= i < len(self.scenes):
            return
        if i == self.current:
            self.pending = -1                # re-clic sur la scène en cours = annule l'attente
            self.version += 1
            return
        if self.current < 0:
            self._switch(i)
        elif self.quantize and self.playing:
            self.pending = i                 # part au prochain début de boucle
            self.version += 1
        else:
            self._switch(i)

    def _switch(self, i):
        first = self.current < 0
        self.prev_image = self.image
        self.current = i
        self.pending = -1
        if first or self.restart_on_switch:
            self.g = 0.0
        self.fade_start = time.perf_counter()
        self.version += 1

    def restart(self):
        self.g = 0.0

    # --- calcul de l'image à afficher ---
    def _intro_for(self, sc):
        return max(0, min(self.intro, sc.n - 1))

    def index_for(self, sc, gi):
        """Numéro d'image de la vidéo pour la position globale gi : intro, puis boucle."""
        intro = self._intro_for(sc)
        if gi < intro:
            return gi
        return intro + (gi - intro) % max(1, sc.n - intro)

    def _loop_id(self, gi):
        sc = self.scenes[self.current]
        intro = self._intro_for(sc)
        return -1 if gi < intro else (gi - intro) // max(1, sc.n - intro)

    def tick(self, now):
        dt = 0.0 if self.last_tick is None else now - self.last_tick
        self.last_tick = now
        if self.current < 0:
            return
        if self.playing:
            prev_gi = int(self.g)
            self.g += dt * self.fps
            gi = int(self.g)
            # début de boucle atteint -> on lance la scène en attente
            if self.pending >= 0 and gi != prev_gi and self._loop_id(prev_gi) != self._loop_id(gi):
                self._switch(self.pending)
        sc = self.scenes[self.current]
        self.image = sc.frame(self.index_for(sc, int(self.g)))

    def fade_alpha(self, now):
        if self.fade_ms <= 0 or self.prev_image is None:
            return 1.0
        return min(1.0, (now - self.fade_start) / (self.fade_ms / 1000.0))


# ----------------------------------------------------------------------------
# DESSIN (utilisé par l'aperçu ET par la fenêtre de sortie)
# ----------------------------------------------------------------------------
def draw_vj_frame(p, rect, engine, now):
    p.fillRect(rect, QColor(0, 0, 0))
    img = engine.image
    if img is None or img.isNull():
        return

    def target(im):
        if engine.stretch:
            return QRectF(rect)
        s = min(rect.width() / im.width(), rect.height() / im.height())
        w, h = im.width() * s, im.height() * s
        return QRectF(rect.x() + (rect.width() - w) / 2, rect.y() + (rect.height() - h) / 2, w, h)

    a = engine.fade_alpha(now)
    if a < 1.0 and engine.prev_image is not None and not engine.prev_image.isNull():
        p.drawImage(target(engine.prev_image), engine.prev_image)
    p.setOpacity(a)
    p.drawImage(target(img), img)
    p.setOpacity(1.0)


class VjPreview(QWidget):
    """Aperçu de ce qui est envoyé sur l'écran principal."""
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.setMinimumHeight(240)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        draw_vj_frame(p, QRectF(self.rect()), self.engine, time.perf_counter())
        eng = self.engine
        lines = []
        if 0 <= eng.current < len(eng.scenes):
            lines.append(f"● EN DIRECT : {eng.scenes[eng.current].name}")
        else:
            lines.append("Aucune scène envoyée — clique sur une scène en bas")
        if 0 <= eng.pending < len(eng.scenes):
            lines.append(f"→ en attente du prochain début de boucle : {eng.scenes[eng.pending].name}")
        for i, text in enumerate(lines):
            p.setPen(QColor(0, 0, 0))
            p.drawText(13, 23 + 18 * i, text)
            p.setPen(QColor(255, 255, 255))
            p.drawText(12, 22 + 18 * i, text)


# ----------------------------------------------------------------------------
# PANNEAU VJ : aperçu en haut, réglages au milieu, banque de scènes en bas
# ----------------------------------------------------------------------------
class VjPanel(QWidget):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self._seen = -1
        self._loading = False

        self.preview = VjPreview(engine)

        self.btn_play = QPushButton("⏸ Pause")
        self.btn_play.clicked.connect(self.toggle_play)
        btn_restart = QPushButton("⟲ Redémarrer (intro)")
        btn_restart.clicked.connect(engine.restart)

        self.spin_fps = QDoubleSpinBox()
        self.spin_fps.setRange(1, 240)
        self.spin_fps.setDecimals(2)
        self.spin_fps.setValue(30)
        self.spin_intro = QSpinBox()
        self.spin_intro.setRange(0, 1000000)
        self.spin_intro.setSuffix(" images")
        self.spin_fade = QSpinBox()
        self.spin_fade.setRange(0, 5000)
        self.spin_fade.setSingleStep(50)
        self.spin_fade.setSuffix(" ms")
        self.spin_cache = QSpinBox()
        self.spin_cache.setRange(0, 16000)
        self.spin_cache.setSingleStep(100)
        self.spin_cache.setValue(800)
        self.spin_cache.setSuffix(" Mo")
        self.chk_quant = QCheckBox("Quantifié : changer au prochain début de boucle")
        self.chk_restart = QCheckBox("Relancer l'intro à chaque changement de scène")
        self.chk_stretch = QCheckBox("Étirer l'image pour remplir l'écran")
        self.lbl_cache = QLabel("")
        for w in (self.spin_fps, self.spin_intro, self.spin_fade, self.spin_cache):
            w.valueChanged.connect(self._apply_settings)
        for w in (self.chk_quant, self.chk_restart, self.chk_stretch):
            w.toggled.connect(self._apply_settings)

        g = QGroupBox("Synchro & réglages")
        grid = QGridLayout(g)
        grid.addWidget(QLabel("Images/seconde"), 0, 0)
        grid.addWidget(self.spin_fps, 0, 1)
        grid.addWidget(QLabel("Intro"), 0, 2)
        grid.addWidget(self.spin_intro, 0, 3)
        grid.addWidget(QLabel("Fondu"), 1, 0)
        grid.addWidget(self.spin_fade, 1, 1)
        grid.addWidget(QLabel("Cache RAM"), 1, 2)
        grid.addWidget(self.spin_cache, 1, 3)
        grid.addWidget(self.chk_quant, 2, 0, 1, 4)
        grid.addWidget(self.chk_restart, 3, 0, 1, 4)
        grid.addWidget(self.chk_stretch, 4, 0, 1, 4)
        grid.addWidget(self.lbl_cache, 5, 0, 1, 4)

        # banque de scènes
        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(QSize(160, 90))
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setSpacing(8)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(True)
        self.list.setMinimumHeight(170)
        self.list.itemClicked.connect(lambda it: self.engine.go(self.list.row(it)))

        btn_add = QPushButton("Ajouter des scènes…")
        btn_add.clicked.connect(self.add_scenes_dialog)
        btn_del = QPushButton("Retirer la scène sélectionnée")
        btn_del.clicked.connect(self.remove_selected)
        btn_pre1 = QPushButton("⚡ Précharger la sélection")
        btn_pre1.clicked.connect(lambda: self.preload(selected_only=True))
        btn_pre_all = QPushButton("⚡ Précharger tout")
        btn_pre_all.clicked.connect(lambda: self.preload(selected_only=False))

        lay = QVBoxLayout(self)
        lay.addWidget(self.preview, 3)
        row = QHBoxLayout()
        row.addWidget(self.btn_play)
        row.addWidget(btn_restart)
        lay.addLayout(row)
        lay.addWidget(g)
        lay.addWidget(QLabel("Scènes — clic ou touches 1 à 9 pour envoyer sur l'écran principal :"))
        lay.addWidget(self.list, 2)
        r2 = QHBoxLayout()
        r2.addWidget(btn_add)
        r2.addWidget(btn_del)
        lay.addLayout(r2)
        r3 = QHBoxLayout()
        r3.addWidget(btn_pre1)
        r3.addWidget(btn_pre_all)
        lay.addLayout(r3)

    # --- réglages ---
    def _apply_settings(self, *_):
        if self._loading:
            return
        e = self.engine
        e.fps = self.spin_fps.value()
        e.intro = self.spin_intro.value()
        e.fade_ms = self.spin_fade.value()
        e.cache.budget = self.spin_cache.value() * 1024 * 1024
        e.quantize = self.chk_quant.isChecked()
        e.restart_on_switch = self.chk_restart.isChecked()
        e.stretch = self.chk_stretch.isChecked()
        if not e.quantize:
            e.pending = -1
            e.version += 1

    def toggle_play(self):
        self.engine.playing = not self.engine.playing
        self.btn_play.setText("⏸ Pause" if self.engine.playing else "▶ Lecture")

    # --- scènes ---
    def add_scenes_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choisir des vidéos de scène", "", VIDEO_FILTER)
        if files:
            self.add_scenes(files)

    def add_scenes(self, paths):
        was_empty = not self.engine.scenes
        errors = []
        for path in paths:
            try:
                self.engine.add_scene(path)
            except ValueError as err:
                errors.append(str(err))
        if was_empty and self.engine.scenes:
            self._loading = True
            self.spin_fps.setValue(self.engine.fps)
            self._loading = False
        self.rebuild_list()
        if errors:
            QMessageBox.warning(self, "Erreur", "\n".join(errors))

    def remove_selected(self):
        row = self.list.currentRow()
        if row >= 0:
            self.engine.remove_scene(row)
            self.rebuild_list()

    def rebuild_list(self):
        self.list.clear()
        for i, sc in enumerate(self.engine.scenes):
            self.list.addItem(QListWidgetItem(QIcon(sc.thumb), f"{i + 1} · {sc.name}"))
        self.refresh_styles()

    def refresh_styles(self):
        e = self.engine
        for i in range(self.list.count()):
            it = self.list.item(i)
            if i == e.current:
                it.setBackground(QColor(0, 160, 80, 120))
            elif i == e.pending:
                it.setBackground(QColor(255, 160, 0, 120))
            else:
                it.setBackground(QColor(0, 0, 0, 0))
        self._seen = e.version

    def preload(self, selected_only):
        e = self.engine
        if selected_only:
            row = self.list.currentRow()
            scenes = [e.scenes[row]] if 0 <= row < len(e.scenes) else []
        else:
            scenes = list(e.scenes)
        if not scenes:
            return
        total = sum(sc.n for sc in scenes)
        dlg = QProgressDialog("Préchargement des images en mémoire…", "Annuler", 0, total, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        done = 0
        for sc in scenes:
            for i in range(sc.n):
                sc.frame(i)
                done += 1
                if done % 10 == 0:
                    dlg.setValue(done)
                    QApplication.processEvents()
                    if dlg.wasCanceled():
                        return
        dlg.setValue(total)

    # --- boucle ---
    def tick(self):
        self.preview.update()
        if self._seen != self.engine.version:
            self.refresh_styles()
        c = self.engine.cache
        self.lbl_cache.setText(f"Cache : {c.bytes / 1e6:.0f} / {c.budget / 1e6:.0f} Mo  ({len(c.d)} images)")

    # --- projet ---
    def state_dict(self):
        e = self.engine
        return {"scenes": [sc.path for sc in e.scenes], "fps": e.fps, "intro": e.intro,
                "fade_ms": e.fade_ms, "cache_mb": self.spin_cache.value(), "quantize": e.quantize,
                "restart": e.restart_on_switch, "stretch": e.stretch}

    def apply_state(self, d):
        self.engine.clear_scenes()
        self.add_scenes(d.get("scenes", []))
        self._loading = True
        self.spin_fps.setValue(d.get("fps", self.engine.fps))
        self.spin_intro.setValue(d.get("intro", 0))
        self.spin_fade.setValue(d.get("fade_ms", 0))
        self.spin_cache.setValue(d.get("cache_mb", 800))
        self.chk_quant.setChecked(d.get("quantize", False))
        self.chk_restart.setChecked(d.get("restart", False))
        self.chk_stretch.setChecked(d.get("stretch", False))
        self._loading = False
        self._apply_settings()
