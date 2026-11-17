# Lampyre 🪲✨

Lampyre est un petit logiciel de **vidéo mapping** et de **VJing** écrit en Python.
Il a été créé pour avoir un outil simple, lisible et modifiable, sans licence payante.

> ⚠️ Projet jeune, en développement actif. Des bugs sont possibles.

## Fonctionnalités

### Mapping
- Sortie plein écran sur le projecteur de ton choix
- Images, GIF animés, vidéos et visuels générés
- Déformation en perspective en tirant les 4 coins de chaque surface
- Réglage fin au clavier (flèches, Maj pour aller plus vite)
- Verrouillage des surfaces une fois bien placées
- Mire de calibration réglable (taille des cases ou nombre de colonnes)
- Sauvegarde et ouverture de projets (JSON)

### Création de visuels
- Formes de base (carré, losange, cercle, étoile…) et formes libres
- Animations cumulables : clignotement, rotation, pulsation, fondu, va-et-vient, cycle de couleur
- Les modifications s'affichent en direct sur la projection

### VJ
- Banque de scènes vidéo, envoyées sur l'écran principal en un clic ou avec les touches 1 à 9
- Toutes les scènes lues sur une même horloge : les mouvements restent synchronisés au changement
- Gestion de l'intro et de la boucle, changement quantifié, fondu, cache en RAM

## Installation

```bash
pip install PySide6 opencv-python numpy
python mapper.py
```

## Raccourcis

| Touche | Action |
|---|---|
| `E` | Mode édition |
| `L` | Verrouiller / déverrouiller la surface |
| `1` à `9` | Envoyer une scène (onglet VJ) |
| Flèches | Déplacer le coin ou la surface (Maj = 10 px) |
| `Échap` | Fermer la sortie |

## Structure

- `mapper.py` : application principale (mapping, fenêtres, projet)
- `visuals.py` : éditeur de visuels animés
- `vj.py` : mode VJ (scènes synchronisées)

## Pistes pour la suite
Masques, maillage de déformation, réaction au son, MIDI/OSC, timeline.

## Licence
MIT
