# DVE / SuperSource

Compose 4 incrustations (PiP) sur un fond — équivalent du SuperSource ATEM. 5 entrées (Fond + Box 1–4), 1 sortie. Compositing live, mémoires avec transitions interpolées.

## Câblage

Câbler depuis la page **Câbles** :
- **Fond** : image de fond (plein cadre)
- **Box 1–4** : sources PiP

## Composer

Dans **Traitements → DVE** :
- **Glisser** une box pour la déplacer
- **Tirer le coin** bas-droit pour redimensionner (ratio préservé)
- **Crop** : rogner chaque box (haut/bas/gauche/droite)
- **Activer/désactiver** chaque box via sa case

Tous les changements sont **live et persistés**.

## Mémoires

Enregistrer une disposition (bouton « Enregistrer »), la rappeler avec une **transition interpolée** (durée réglable). Les positions, tailles et crops s'interpolent. Les mémoires sont propres à chaque DVE.

Actions par mémoire : **Rappeler**, **Exporter** (.json), **Envoyer vers** un autre DVE, **Supprimer**.

## Pilotage ATEM

Les boxes SuperSource sont pilotables depuis un pupitre ATEM. Configurer dans **Réglages → Pupitres ATEM** : affecter le DVE en SS0 ou SS1 du pupitre.

## Notes

- Le modèle de box est identique au SuperSource ATEM (centre normalisé + échelle, ratio préservé)
- Importer une mémoire depuis un fichier `.json` exporté d'un autre DVE
