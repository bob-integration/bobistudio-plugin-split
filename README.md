# Split — DVE 2D quatre canaux de Bobi.Studio

*[English version](README.en.md)*

Une DVE 2D : quatre canaux plus un fond, composés en direct vers une sortie unique. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `plugins/split/`.

## Ce que ça fait

Cinq entrées, le fond et quatre canaux. Chaque canal dispose des réglages classiques (déplacement,
rognage, échelle, rotation, opacité, coins arrondis, adoucissement, ombre portée, miroir, ordre de
superposition, bordure 3D).

Les dispositions s'enregistrent, se rappellent avec une transition interpolée (neuf courbes
d'accélération, décalage inter-boîtes), s'exportent et s'envoient vers un autre split. Tous les
paramètres sont interpolés, pas seulement la géométrie.

## Composer

Un éditeur semblable à celui d'un multiview vous permet de créer votre composition. Glisser pour
déplacer, tirer un coin pour redimensionner, tirer une arête pour rogner ; les flèches du clavier
déplacent au pixel et contournent l'aimantation. Les positions vont de −1 à 2, pour les entrées et
sorties de champ.

En plus des quatre canaux, il existe un **canal Global**. Ce n'est pas une multi-sélection : c'est
un objet à part, qui se compose par-dessus les réglages de chacune des boîtes,
`rendu(boîte i) = Global ∘ boîte_i`. Toute la composition sort du cadre d'un bloc, chaque boîte
gardant sa position relative. Il est enregistré dans les mémoires et interpolé comme une boîte,
pour un coût nul.

## Le fond

Noir, source vidéo, couleur unie ou dégradé de deux à quatre arrêts, avec un angle et une
**douceur**. À douceur nulle la frontière est nette : le fond sert alors de wipe, animé par un
rappel de mémoire avec durée.

## Habillage

Une image importée (PNG, JPEG) se pose au-dessus ou en dessous des canaux, avec sa transparence
s'il y en a. Trois incrustations : **alpha** (le canal de transparence de l'image), **luma** et
**chroma**. Une image sans canal alpha reste opaque en incrustation alpha.

## Coût par trame

Le compositing tourne sur le noyau C `libbobi_mvk` — gather, ruban de bordure et blend en une
seule passe mémoire, ces passes étant limitées par la RAM et non par le calcul.

Attention concernant les ressources nécessaires : la vraie consommation CPU se fait sur les
animations.

| 1080p, 4 boîtes, 2 cœurs | statique | animé |
|---|---|---|
| socle | 7,0 ms | 6,4 ms |
| sans rotation | 9,0 ms | 22,7 ms |
| rotation seule | 10,3 ms | 19,8 ms |
| tous les effets | 18,4 ms | 58,3 ms |

Sans aucune rotation, l'animation coûte déjà le double du budget 50p : la rotation n'est pas seule
en cause. Et « tous les effets » animé ne vaut pas la somme des parties — la croissance est
superlinéaire.

## CPU ou GPU

Le rendu CPU est le défaut et convient à toutes les compositions fixes ou peu animées. Le rendu
GPU (`render=gpu`, carte NVIDIA) est réservé aux compositions animées en permanence : il ramène la
trame animée riche sous les 8 ms, au prix d'une carte immobilisée pour quatre à cinq splits. Il est
exclusif du mode tranche, et en `auto` le repli sur CPU est publié sur `:8080`. Les deux chemins
ne sont pas identiques à l'octet près : basculer une composition change ses octets, pas son aspect.

## Mode tranche

La sortie est publiée bande par bande, pour retirer une trame de latence à l'aval. Réglage global
Réglages → MXL.

## Contrôle et diagnostic

Tous les paramètres continus sont pilotables par les macros et les déclencheurs (`param_tree`,
bornes publiées en direct), et les mémoires se rappellent par action. `GET /state` rend la
composition effective ; `:8080` publie la cadence, le budget de trame, les couches réellement
composées et la raison de chaque canal non composé. Ce dernier compteur compte : un split peut
tenir sa cadence en ne composant rien, et c'est `composed` qui le dit.

## Couleurs

Bordure, ombre, couleur d'incrustation chroma, habillage et fond partagent une seule conversion,
réglée sur la colorimétrie déclarée du format (601, 709, 2020) ; absente, 709 est supposé et
annoncé. Les versions antérieures à la 0.24 appliquaient une matrice 601 pleine échelle quel que
soit le format : les teintes d'une composition d'alors peuvent avoir légèrement bougé.

## Ressources

Trois cœurs, 2 Gio déclarés, épinglés, plus un GPU facultatif. Relevé le 2026-10-30 (dell-1,
quatre boîtes, rendu CPU, 1080p50) : 92 à 100 Mio.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
