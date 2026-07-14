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
Depuis la **0.10.0**, le compositing du mode avancé tourne sur le **noyau C `mvk`**
(`libbobi_mvk`, ABI 4 — image `bobi-compute` ≥ 0.15), comme le mixer, le multiview et la
pyramide : *gather* ⊗ ruban de bordure ⊗ blend en **une seule passe mémoire** (ces passes sont
memory-bound : chaque opération numpy est un aller-retour RAM complet). Colonnes « numpy » =
repli, toujours présent et bit-exact.

| Effet (4 boxes)            | 720p numpy | **720p mvk** | 1080p numpy | **1080p mvk** |
|----------------------------|-----------|-----------|-----------|-----------|
| socle (aucun effet)        |  3,2 ms   | 3,2 ms    |  6,1 ms   | 6,1 ms    |
| adoucissement (feather)    |  4,5 ms   | **1,7 ms**|  8,0 ms   | **3,1 ms**|
| coins arrondis             |  5,6 ms   | **1,7 ms**|  9,7 ms   | **3,1 ms**|
| ombre portée               |  3,6 ms   | **1,6 ms**|  6,6 ms   | **3,0 ms**|
| bordure 3D (biseau)        | 12,9 ms   | **2,4 ms**| 29,5 ms   | **4,7 ms**|
| opacité                    |  7,1 ms   | **1,5 ms**| 16,1 ms   | **2,6 ms**|
| **rotation**               | 15,1 ms   | **3,0 ms**| **38,6 ms** | **8,4 ms** |
| tous les effets            | 23,0 ms   | **4,5 ms**| 58,1 ms   | **11,2 ms**|

(minimum sur 40 trames, cache de stamp chaud, cpuset de 2 cœurs physiques ; le nœud de banc est
volontairement chargé → la moyenne est plus haute que le minimum.)

**Comment ça tient** : chaque box précalcule une *empreinte* (cartes d'échantillonnage, masque
alpha, ruban de bordure, masque d'ombre) **reconstruite seulement au changement de réglage**. La
position n'entre pas dans la clé de cache : une box qui ne fait que **translater** réutilise son
empreinte telle quelle. Tout l'habillage sauf la rotation étant fonction de la *distance aux
bords*, il est construit **séparablement** (2 rampes 1D + 4 patchs de coins) au lieu de balayages
2D. Par trame il ne reste qu'un *gather* et un blend — désormais fusionnés en C.

**★ Le plafond a bougé : ce n'est plus la rotation, c'est la rotation qui BOUGE.** En position
fixe, la rotation tient largement le 1080p50 (**8,4 ms** pour 20 ms de budget). Mais dès que
l'angle (ou la taille/le crop) **change à chaque trame** — transition en cours, curseur qu'on
glisse — l'empreinte est **reconstruite à chaque trame** : la carte d'index 2D et les masques
obliques d'une box tournée coûtent ≈ **13 ms/box** en numpy → **53 ms/trame** en 1080p pour
4 boxes. C'est le **prochain chantier** (porter la construction de l'empreinte tournée en C).
Les autres effets, eux, se reconstruisent séparablement et passent (bordure animée : 12 ms).

Le plugin **ne décroche jamais en silence** : les métriques `:8080` exposent
`adv.budget_pct`, `adv.over_budget`, `adv.rotated_boxes` et `adv.stamp_hit_rate` (un taux qui
s'effondre en régime établi = une clé de cache qui bouge sans raison), et un dépassement du
budget de trame est **loggé une fois par franchissement**. Le noyau C a ses propres témoins :
`mvk` (actif ?), `mvk_threads`, et surtout **`mvk_miss` / `mvk_why`** — le compte des **replis
numpy**. Un repli est 5 à 20× plus lent : `mvk_miss` **doit rester à zéro**, et s'il grimpe,
`mvk_why` dit sur quelle forme/dtype le kernel a refusé (jamais d'échec muet).

> **Vieille image** (`bobi-compute` < 0.15, sans ABI 4) : le plugin fonctionne à l'identique, en
> repli numpy intégral (`mvk: false` sur `:8080`) — avec les coûts de la colonne « numpy ».

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
