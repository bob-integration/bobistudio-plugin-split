# DVE / SuperSource

Compose 4 incrustations (PiP) sur un fond — un composeur multi-PiP. 5 entrées (Fond + Box 1–4), 1 sortie. Compositing live, mémoires avec transitions interpolées.

## Câblage

Câbler depuis la page **Câbles** :
- **Fond** : image de fond (plein cadre)
- **Box 1–4** : sources PiP

## Composer

Dans **Traitements → DVE** :
- **Cliquer** une box pour la sélectionner ; **Maj+clic** pour en sélectionner plusieurs (la
  première sélectionnée sert de **référence** aux outils).
- **Glisser** une box pour la déplacer · **tirer le coin** bas-droit pour redimensionner ·
  **tirer une arête** pour rogner (crop).
- **Flèches du clavier** : déplacement **pixel par pixel** de la sortie ; **Maj+flèche** = 10 px.
- **Barre d'outils** (identique à l'éditeur de layout multiview) : **aligner** (gauche / centre /
  droite, haut / milieu / bas), **même taille** que la référence, **distribuer** (H/V),
  **copier/coller les réglages** d'une box (hors position), et **snap magnétique** (aimantation aux
  bords et au centre du cadre, et aux autres box — guides pointillés pendant le glissé). Les
  **flèches du clavier contournent le magnétisme** : c'est le geste du réglage au pixel près.
- **Rotatifs** : les grandeurs continues (crop, rotation, opacité, adoucissement, coins, biseau,
  ombre, lumière, habillage) se règlent au **cadran** — même composant que le correcteur de
  couleur : glissé vertical, molette, flèches (Maj = pas fin), double-clic = valeur par défaut.
  Le **champ chiffré sous le cadran reste éditable** (précision et accessibilité).
- **Activer/désactiver** chaque box via son switch.
- Le **budget de trame** du moteur est affiché en permanence dans la barre d'outils
  (% du budget, nombre de box en rotation) : au-delà de 100 %, le Split décroche — la rotation
  est le premier poste à retirer (cf. « Coût par trame » plus bas).

Tous les changements sont **live et persistés** (sauf l'image d'habillage ART, cf. plus bas).

## Mémoires

Enregistrer une disposition (bouton « Enregistrer »), la rappeler avec une **transition interpolée** (durée réglable). Les positions, tailles et crops s'interpolent. Les mémoires sont propres à chaque DVE.

Actions par mémoire : **Rappeler**, **Exporter** (.json), **Envoyer vers** un autre DVE, **Supprimer**.

## Pilotage depuis un pupitre

Les boxes sont pilotables depuis un pupitre externe compatible. Configurer dans **Réglages → Pupitres** : affecter le Split à l'un des deux emplacements de composition du pupitre.

## Notes

- Le modèle de box est un centre normalisé + une échelle, à ratio préservé par défaut
- Importer une mémoire depuis un fichier `.json` exporté d'un autre DVE

---

# Mode avancé, couche ART et transitions (0.9.0)

Tous les réglages ci-dessous sont **neutres par défaut** : tant qu'on n'y touche pas, la
composition emprunte le chemin historique (sortie **octet-identique** à 0.8.0, coût inchangé).

## Couche ART (habillage)

L'habillage graphique : logo, cadre, décor de plateau posé **au-dessus** ou **en
dessous** des boxes, avec un **incrustateur** qui y perce des trous où se logent les caméras.

- **Source = image importée** (PNG/JPEG). Ce n'est *pas* une 6ᵉ entrée vidéo, et c'est un
  arbitrage **mesuré** : une source vidéo vivante oblige à recalculer le masque de clé à chaque
  trame sur le cadre plein — **21,7 ms/trame en 1080p, soit 108 % du budget 50 fps à elle
  seule**. L'image importée précalcule sa clé une fois à l'import : **0,9 ms/trame**. Le jour où
  l'ART vivant deviendra nécessaire, il faudra le noyau C (`mvk`) — chantier dédié.
- **Incrustation** : `alpha` (canal alpha du PNG), `luma` (seuil sur la luminance), `chroma`
  (fond vert/bleu), ou `none`. Réglages : `clip` (seuil), `gain`, `invert`, `opacity`.
- Un PNG à fond transparent + clé `alpha` est le cas le plus simple et le plus propre.

## Bordure complète

- `w` / `color` : le trait plat historique (inchangé).
- `inner` / `outer` : largeurs **intérieure** et **extérieure** distinctes.
- `bevel` : **biseau** — positif = sortant (relief), négatif = rentrant (creux).
- `bevel_soft`, `bevel_pos` : adoucissement et position du biseau dans l'épaisseur.
- **Source de lumière** (globale, `POST /light` : `angle` + `altitude`) : c'est elle qui donne le
  **relief**. Un bord est éclairé, l'opposé est dans l'ombre. Sans elle, un biseau ne serait
  qu'un dégradé arbitraire — c'est la différence entre un cadre plat et un cadre 3D.

## Mode avancé (par box)

`rotation` (la **tuile entière** bascule, cadre compris — pas seulement le contenu),
`opacity`, `feather` (adoucissement des bords), `radius` (coins arrondis),
`shadow` (ombre portée : `dx`/`dy`/`blur`/`opacity`/`color`), `mirror_h`/`mirror_v`,
`aspect: free` + `size_x`/`size_y` (**ratio libre** — à ratio conservé, la box garde le format de
sa source), `z` (**ordre de superposition libre** — au lieu de l'ordre figé 1→2→3→4).

## Transitions

- **9 courbes d'accélération** (`linear`, `ease_in/out/in_out`, `cubic_*`, `sine_in_out`,
  `expo_out`). En 0.8.0 l'interpolation était strictement **linéaire** → mouvement mécanique.
- **Décalage inter-boxes** (`stagger_ms`) : les boxes arrivent l'une après l'autre. Le décalage
  est compté *dans* la durée totale — la dernière box finit à l'heure demandée.
- **Tous** les paramètres sont interpolés (géométrie, bordure, rotation, opacité, ombre,
  coins…). En 0.8.0 la **bordure sautait** à sa valeur cible pendant que le reste glissait.

## ★ Coût par trame (mesuré, nœud 30, 4 boxes, Y+U+V, cache chaud)

Budget : **40 ms** à 25 fps, **20 ms** à 50 fps.

| Effet (4 boxes)            |  720p   |  1080p  |
|----------------------------|---------|---------|
| socle (aucun effet)        |  3,1 ms |  6,2 ms |
| adoucissement (feather)    |  4,1 ms |  8,0 ms |
| coins arrondis             |  5,0 ms |  8,9 ms |
| ombre portée               |  3,3 ms |  6,7 ms |
| bordure 3D (biseau)        |  4,9 ms | 10,9 ms |
| opacité                    |  5,8 ms | 12,8 ms |
| **rotation**               |**11,4 ms**|**31,9 ms**|
| tous SAUF rotation         | ~10,7 ms| ~22,6 ms|

**Comment ça tient** : chaque box précalcule une *empreinte* (cartes d'échantillonnage, masque
alpha, ruban de bordure, masque d'ombre) **reconstruite seulement au changement de réglage**. La
position n'entre pas dans la clé de cache : une box qui ne fait que **translater** réutilise son
empreinte telle quelle. Tout l'habillage sauf la rotation étant fonction de la *distance aux
bords*, il est construit **séparablement** (2 rampes 1D + 4 patchs de coins) au lieu de balayages
2D — mesuré **14,5 → 0,8 ms/box** en 1080p. Par trame il ne reste qu'un *gather* et un blend de
**ruban** (l'intérieur opaque est copié, jamais mélangé).

**★ Le plafond : la rotation.** C'est la seule primitive qui ne tient pas le budget en numpy —
elle impose un rééchantillonnage 2D sur la boîte englobante et des masques obliques non
séparables. Concrètement :

- **720p** : confortable (11,4 ms).
- **1080p 25 fps** : marginal (31,9 ms = 80 % du budget) — pas de marge.
- **1080p 50 fps** : **hors budget**.
- **Pendant une transition** : la géométrie change à chaque trame, donc les cartes 2D se
  reconstruisent à chaque trame → **hors budget** dès 1080p. Les autres effets, eux, se
  reconstruisent séparablement et passent.

Le plugin **ne décroche jamais en silence** : les métriques `:8080` exposent
`adv.budget_pct`, `adv.over_budget`, `adv.rotated_boxes` et `adv.stamp_hit_rate` (un taux qui
s'effondre en régime établi = une clé de cache qui bouge sans raison), et un dépassement du
budget de trame est **loggé une fois par franchissement**, en nommant la rotation.

## Contrat de contrôle (pour l'UI)

- `GET /state` — publie les réglages **et leur domaine** dans `caps` (`easings`, `art_keys`,
  `box_fields` min/max/défaut, `box_flags`, `box_enums`, `border_fields`, `shadow_fields`,
  `light_fields`, `stagger_ms`) : **l'UI n'a rien à coder en dur**. `art` est renvoyé sans le
  blob base64 (`has_image`/`loaded` suffisent).
- `POST /box` — **fusion profonde** : on peut ne patcher qu'un champ (`{idx, rotation}`,
  `{idx, shadow:{blur}}`). Les sous-dicts `crop`/`border`/`shadow` sont fusionnés, pas remplacés.
- `POST /light` — `{angle, altitude}`.
- `POST /transition` — `{curve, stagger_ms}` (défauts).
- `POST /recall` — `{boxes, duration_ms, curve, stagger_ms}`.
- `POST /art` — `{enabled, above, image (base64), key, clip, gain, invert, opacity, chroma_key}`.
