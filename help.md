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
  Le **champ chiffré sous le cadran reste éditable** (précision et accessibilité), et il porte les
  **boutons −/+** du produit (appui maintenu = répétition), comme **tous** les champs numériques du
  Split (position, tailles, z-ordre, largeurs de bordure, durées). Le **pas est unique** : bouton,
  flèches du clavier et molette avancent exactement du même pas — celui déclaré par le moteur dans
  `/state.caps` (un angle ne s'incrémente donc pas comme une opacité).
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

### ★ Le piège : « mes box ont disparu »

Une **photo sans canal de transparence** (JPEG, ou PNG opaque) importée avec la clé **`alpha`**
est **opaque partout** : le canal alpha vaut 255 sur tout le cadre, la clé ne perce **aucun
trou**, et l'habillage — posé **au-dessus** — **recouvre intégralement les 4 boxes**. Elles sont
bien composées… puis effacées. (Vécu en prod : « j'importe une photo, je l'ai bien, mais aucune
box dessus ».)

Depuis la **0.10.1**, le plugin **le dit** — il ne le corrige pas d'office (un fond plein cadre
peut être voulu : c'est votre choix, pas le sien) :

- à la **cuisson** de l'image, il mesure `has_alpha` (l'image a-t-elle un canal de
  transparence ?), `cover` (opacité **moyenne** du masque) et `holes` (part réellement
  transparente — c'est par là que les boxes se voient) ;
- deux avertissements : **`no_alpha`** (image sans alpha + clé `alpha`) et **`covers_all`**
  (aucun trou + forte opacité + habillage au-dessus) ;
- affichés en **bandeau dans l'UI dès l'import**, loggés **une fois** (à la cuisson, jamais par
  trame), et exposés sur `:8080` (`adv.art_cover`, `adv.art_holes`, `adv.art_warn`).

**Remèdes** : importer un **PNG à fond transparent**, choisir la clé **`luma`** ou **`chroma`**,
ou placer l'habillage **en dessous** des boxes.

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

### ★★ Le vrai plafond : la géométrie qui BOUGE (mesuré, dell-1, 2 cœurs physiques)

Le tableau ci-dessus est celui du **régime établi** : l'empreinte est en cache, la trame ne fait
que composer. Dès que la géométrie **change à chaque trame** — transition en cours, curseur qu'on
glisse, rotation animée — l'empreinte est **rebâtie 50 fois par seconde** et c'est ELLE qui fait
la trame : mesuré, **90 % du temps** (208 ms sur 230).

| 1080p, 4 boxes | statique | animé (numpy) | animé (ABI 6) | **animé (ABI 7)** |
|----------------|----------|---------------|---------------|-------------------|
| socle          |  7,0 ms  |   6,4 ms      |   6,4 ms      |   6,4 ms          |
| sans rotation  |  9,0 ms  |  **39,3 ms**  |  39,3 ms      |  **22,7 ms**      |
| rotation seule | 10,3 ms  |  **52,9 ms**  |  **21,2 ms**  |  19,8 ms          |
| tous les effets| 18,4 ms  | **226,8 ms**  |  **125,5 ms** |  **58,3 ms**      |

Avec 8 cœurs physiques et `BOBI_MVK_THREADS=16` : rotation animée **11,2 ms**, tous effets
**65,4 ms**. (Le défaut est plafonné à 4 threads — cf. `_mvk_default_threads` : un conteneur sans
cpuset verrait toute la machine.)

**Deux corrections à ce que ce fichier affirmait auparavant** :

1. « Seule la rotation décroche, les autres effets se reconstruisent séparablement et passent » —
   **faux**. Sans rotation du tout, l'animation coûte déjà **39 ms**, soit le double du budget 50p.
2. « tous les effets » animé ne vaut pas la somme des parties (39 + 53) mais **227 ms** : la
   rotation fait **sortir tous les autres effets du chemin séparable**, chaque masque devenant un
   balayage 2D plein sur l'AABB au lieu de deux rampes 1D. C'est superlinéaire, et ça ne se voyait
   pas parce que ce régime n'avait jamais été mesuré.

### Deux plafonds de NATURE différente (ABI 6 puis ABI 7)

Le régime animé avait **deux** postes de coût, et ils ne se ressemblent pas :

**Box TOURNÉE — de gros tableaux 2D non décomposables.** Un axe oblique rend chaque masque
fonction de la position en 2D : plus de produit externe possible. Le CPU bâtissait donc une
quinzaine de tableaux plein AABB par box (carte d'index, alpha, distance aux bords, ruban).
`mvk_spl_rotmap` / `rotring` / `blur2` (ABI 6) les remplacent par une passe : rotation animée
**52,9 → 21,2 ms**.

**Box DROITE — ★ l'optimisation du chemin chaud devenue le coût du chemin froid.** Ici tout est
séparable et bon marché : deux rampes 1D, quatre patchs de coins, quatre bandes de ruban. Le coût
n'était donc pas dans la géométrie — il était dans le bloc de **pré-fusion ruban ⊗ alpha**, neuf
tableaux plein rect et ~20 passes mémoire, **17 ms sur 41 mesurées**. Or ce bloc existe pour rendre
la trame **rapide** : il pré-mâche les opérandes du compositing pour lui épargner deux balayages
par plan. Il ne se paie qu'au moment où la géométrie **bouge** — précisément le cas que le cache
d'empreinte ne couvre pas. `mvk_spl_prefuse` / `outer` (ABI 7) : **39,3 → 22,7 ms**.

Détail qui explique pourquoi personne ne l'avait vu : la pré-fusion ne s'active que si une
**opacité globale** est posée (sans elle, la box garde un intérieur opaque et le compositing
**copie** au lieu de fondre). Aucun effet pris isolément ne la déclenchait — d'où un « tous les
effets » très supérieur à la somme de ses parties.

**Où on en est.** Trame animée riche : **226,8 → 58,3 ms** (×3,9). Ça tient le 25p, pas le 50p.
Ce qui reste est de l'**arithmétique pure en C** sur ~2 M pixels d'AABB, sans lire un octet de
vidéo : il n'y a plus de gras à retirer, c'est le coût du modèle « je précalcule des cartes parce
que le CPU ne peut pas se permettre de les recalculer par pixel ». Pour aller plus loin il faut
changer de modèle — c'est ce que fait `render=gpu` (cf. « CPU ou GPU ? »).

Le plugin **ne décroche jamais en silence** : les métriques `:8080` exposent
`adv.budget_pct`, `adv.over_budget`, `adv.rotated_boxes` et `adv.stamp_hit_rate` (un taux qui
s'effondre en régime établi = une clé de cache qui bouge sans raison), et un dépassement du
budget de trame est **loggé une fois par franchissement**. Le noyau C a ses propres témoins :
`mvk` (actif ?), `mvk_threads`, et surtout **`mvk_miss` / `mvk_why`** — le compte des **replis
numpy**. Un repli est 5 à 20× plus lent : `mvk_miss` **doit rester à zéro**, et s'il grimpe,
`mvk_why` dit sur quelle forme/dtype le kernel a refusé (jamais d'échec muet).

> **Vieille image** (`bobi-compute` < 0.15, sans ABI 4) : le plugin fonctionne à l'identique, en
> repli numpy intégral (`mvk: false` sur `:8080`) — avec les coûts de la colonne « numpy ».
> **Image < 0.22 (sans ABI 6)** : tout le compositing reste en C, seule la CONSTRUCTION
> d'empreinte retombe en numpy (colonne « animé avant ABI 6 »). Les deux jeux de kernels ont des
> drapeaux séparés — une image intermédiaire n'est pas un tout-ou-rien.

## ★★ CPU ou GPU ? (`render`)

**Réponse courte : laissez `cpu`.** Le GPU ne rend pas le CPU obsolète — il déplace **un seul**
plafond, celui de la **géométrie qui bouge**. Si votre composition est posée et ne bouge pas, le
CPU suffit largement, et le GPU vous ferait payer des transferts pour rien.

### La seule mesure qui décide

Tout tient dans le rapport entre une trame **fixe** et une trame **animée** (transition en cours,
curseur qu'on glisse, rotation animée). 1080p, 4 boxes, tous les effets, budget 20 ms à 50 fps :

| | trame fixe | trame **animée** | rapport |
|---|---|---|---|
| CPU (2 cœurs) | 18,4 ms | 58,3 ms | **× 3,2** |
| CPU (8 cœurs, 16 threads) | ~15 ms | ~35 ms | × 2,3 |
| **GPU** (Quadro P5000) | 7,2 ms | **7,4 ms** | **× 1,03** |

Sur CPU, une composition qui bouge coûte plusieurs fois ce qu'elle coûte à l'arrêt : chaque
changement de géométrie oblige à reconstruire l'« empreinte » de chaque box (cartes
d'échantillonnage, masques, ruban de bordure, silhouette d'ombre) — cinquante fois par seconde.
Les kernels C ont ramené ce surcoût de ×12 à ×3,2, ce qui fait tenir le 25p ; le 50p en
composition riche animée reste hors d'atteinte du CPU.
Sur GPU cette empreinte **n'existe pas** : chaque pixel évalue sa propre géométrie. D'où un coût
identique à l'arrêt et en mouvement. **C'est tout ce que le GPU achète — c'est beaucoup, mais
seulement si vous animez.**

### Choisir en trois questions

1. **Ma composition bouge-t-elle en direct ?** Rappels de mémoire avec transition, mouvements de
   box à l'antenne, réglages manipulés pendant la diffusion. **Non → `cpu`, sans hésiter.** Et un
   rappel de mémoire ponctuel (quelques transitions par émission) ne justifie pas une carte : la
   trame décroche *pendant* la transition, pas après.
2. **Ai-je besoin de la latence sous-trame ?** Le **mode tranche** et le GPU sont **exclusifs** :
   le mode tranche publie la sortie bande par bande pour retirer une image de latence, un
   aller-retour vers la carte se fait par trame et la réintroduit. Si `slice_mode` est actif, le
   GPU est refusé — et le plugin le **dit** (`gpu_why` sur `:8080`), il ne l'ignore pas en silence.
3. **Puis-je immobiliser une carte ?** Un split GPU réserve un GPU du nœud. Compté à 50 fps sur une
   P5000 : ~16 % de la carte en calcul et ~21 % du temps en transferts PCIe, soit **4 à 5 splits
   par carte** — et c'est le **PCIe** qui plafonne, pas le calcul. Sur un parc où le multiview
   réclame déjà des cartes, c'est un arbitrage, pas un détail.

### Ce que coûte le GPU, dit franchement

- **Les transferts.** Sur les 7,4 ms d'une trame GPU, **4,3 ms sont du PCIe** (montée des entrées,
  descente de la sortie) et 3,1 ms seulement du calcul. Le bus MXL vit en mémoire hôte : ces
  transferts sont incompressibles. Une composition simple et fixe est donc **plus rapide sur CPU**.
- **Le rendu n'est pas identique à l'octet près.** Le CPU mélange en entiers 256e, le GPU en
  flottant : ce sont **deux chemins de rendu distincts**. Basculer une composition existante de
  `cpu` à `gpu` change ses octets (pas son aspect) — à valider visuellement une fois, comme un
  changement d'étage.
- **Une carte, une image.** Le conteneur bascule sur l'image `bobi-compute-gpu`. Le chemin CPU y
  est identique : un split `render=cpu` déployé sur l'image GPU rend exactement comme avant.

### Les trois valeurs

| `render` | Comportement |
|----------|--------------|
| `cpu` | **Défaut.** Chemin historique, strictement inchangé. Aucune carte réservée. |
| `gpu` | **EXIGE** le GPU. Pas de carte, pas de cupy, ou mode tranche actif → le script **refuse de démarrer** en nommant la cause. On ne rend pas en silence dix fois plus lentement que ce qui a été demandé. |
| `auto` | GPU si disponible, sinon CPU **en le disant** (log + `gpu_why` sur `:8080`). |

Le chemin réellement actif est toujours lisible sur `:8080` : `render` (demandé), `gpu` (effectif),
`gpu_name`, `gpu_why` (raison du repli). Un `auto` retombé sur CPU ne se déduit jamais d'un écart
de fps.

### En résumé

> Composition posée, même complexe → **CPU**. Composition qui **bouge à l'antenne** en 1080p50,
> avec rotations et habillage → **GPU**. Latence sous-trame requise → **CPU en mode tranche** ; le
> GPU n'est alors pas une option.

## ★★ « Le split ne compose aucune box » — pourquoi, et comment il le dit (0.11.0)

**Le bug vécu** : sortie **vide**, à la bonne cadence, **sans une seule erreur** — `fps 25,0`,
budget 2,5 %, 0 repli, 0 exception… et `slice.layers: 0`. Un split qui ne compose **rien**
ressemblait **exactement** à un split qui va bien.

**La cause** : le producteur (ici `avsync`) a été **redéployé après** le split, **en changeant de
format** (1080p50 → 640×360p25). Le lecteur du split est resté **collé sur l'ancienne génération**
du flux : tête figée, grains à l'**ancien format** → rejetés à chaque trame par la garde
format-fixe, **en silence**. Et le filet « génération périmée » ne jouait pas : il testait un
écart d'index **signé** (`fi_out − hi > 250`), alors que l'ancienne génération vivait sur la
grille TAI **50 fps** (tête ≈ 8,9·10¹⁰) et le split sur celle à **25 fps** (≈ 4,5·10¹⁰) — la tête
morte était **deux fois plus grande**, l'écart **négatif**, le filet muet. Corrigé : c'est le
**figement** de la tête qui trahit une génération morte, pas le **signe** de l'écart (valeur
absolue) — et **tout** slot muet depuis 50 trames (≈ 2 s) est **relâché et rouvert**, quelle que
soit la raison.

**Ce que le plugin dit désormais** — plus aucun renoncement muet. Chaque slot non composé est
**compté avec sa raison**, loggé **une fois** (pas par trame), et publié sur `:8080` :

- `composed` — nombre de couches **réellement composées** (0 = le split ne fait rien : l'alerte) ;
- `slots.<n>` — par slot : `shm`, `reason`, `why` (libellé), `detail`, `frames` (trames
  consécutives), `skips` (cumul par raison). Raisons : `not_wired` (état **normal** : slot non
  câblé), `input_absent`, `no_grain`, **`format`** (« grain 4 147 200 o ≠ canvas 460 800 o
  (640×360 422 8 bits) »), `stale_gen`, `stamp_none`, `geom_none`, **`exception`** (l'exception de
  la boucle de rendu n'est plus avalée).

Le rétablissement est loggé lui aussi (« slot 0 RÉTABLI après N trames »).

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

## ★ Canal Global — le calque de transformation du groupe (0.12.0)

**Ce n'est pas une multi-sélection.** La multi-sélection **écrit** N valeurs dans N box (elle
écrase les réglages individuels). Le **Global** est un **objet à part**, persistant, qui se
**compose** par-dessus les réglages propres de chaque box :

> **rendu(box i) = Global ∘ box_i**

Chaque box garde **ses** x/y/taille/rotation/opacité ; le Global apporte **sa** transformation
(**décalage**, **échelle**, **rotation**, **opacité** du groupe). ⇒ toute la composition **sort du
cadre d'un bloc**, chaque box gardant sa **position relative** : c'est l'**animation de groupe**,
impossible avec une multi-édition. Il est **enregistré dans les mémoires** et **interpolé pendant
les transitions**, comme une box.

**Coût : nul.** Il est **replié dans l'empreinte** de chaque box (quelques flottants par trame),
jamais en seconde passe plein cadre. Mesuré en 1080p, 4 box : socle **6,01 ms** → Global actif
**6,23 ms**.

*Écartés volontairement* : le **miroir** de groupe (il échangerait la gauche et la droite des box
sans miroiter leur contenu — geste ambigu) et **adoucissement / coins / bordure / ombre** (ce sont
des attributs de **bord d'une box** : les appliquer « au groupe » supposerait de composer le groupe
dans un buffer intermédiaire, c'est-à-dire précisément la passe plein cadre qu'on s'interdit).

## ★ Hors cadre (0.12.0)

Les positions ne sont plus bornées au cadre : `x`/`y` vont de **−1 à 2** (bornes publiées dans
`/state.caps.pos` — l'UI n'en code aucune). **Sans hors-cadre, aucune entrée ni sortie de champ
n'est possible.** Le rendu clippait déjà correctement une box à moitié dehors : c'est la **borne**
qui l'interdisait. Dans l'éditeur, le cadre est désormais **entouré d'une marge** : le hors-cadre
est visible (voilé), et on peut y tirer une box. Glisser une box **sélectionnée** entraîne **toute
la sélection** (en **décalage** : les écarts relatifs sont conservés) ; la **poignée de taille**
en multi redimensionne chaque box **autour de son propre centre** (aucune ne se déplace).

## ★ Fond : noir, source vidéo, ou couleur unie (0.12.0)

Trois modes explicites. La couleur est saisie en **hex (sRGB)** ; c'est le **moteur** qui la
convertit — **à sa profondeur** (8/10/12 bits) et **à sa colorimétrie déclarée** (601/709/2020,
lues dans le format du conteneur, jamais devinées). La conversion est faite **une fois** (cache) :
par trame il ne reste que trois `memset` de plan — le socle ne bouge pas (6,01 ms).
Par défaut la couleur est encodée en **plage légale** (broadcast) : `16-235` en 8 bits, `64-940` en
10 bits, `256-3760` en 12 bits (**calculé**, jamais tabulé). Une couleur hors plage est **écrêtée —
et on le dit** (`bg.clipped` + bandeau) ; « Plage légale » se décoche pour encoder en plage complète
en connaissance de cause. Le **Global n'affecte pas le fond** : il transforme le *groupe de box*,
pas la toile.

## ★ Opacité en pourcent (0-100) — une seule unité

Le **moteur** parle en pourcent (box, ombre, habillage, Global) et `/state.caps` l'expose : l'UI ne
convertit rien, il n'y a pas deux vérités. La migration des valeurs persistées **n'est pas
idempotente par nature** (un `1.0` veut dire « opaque » en ancienne unité et « 1 % » en nouvelle) :
un **marqueur** (`opacity_unit: "pct"`) tranche, et le hook de persistance le réécrit — jamais deux
conversions.

## ★ Fond DÉGRADÉ et WIPE — le même objet (0.13.0)

Le mode de fond `gradient` mélange **2 à 4 arrêts** de couleur, avec un **angle** (0-360°) et une
**douceur** (0-1). ★ **La douceur unifie mélange et wipe** : à **0**, la frontière entre deux
arrêts est **nette = un wipe** ; à **1**, c'est un dégradé progressif ; entre, une transition
molle. **Un wipe n'est qu'un dégradé de douceur nulle** — une seule fonctionnalité, pas deux.

Chaque arrêt est une couleur **hex (sRGB)**, convertie par la conversion unique du plugin
(colorimétrie déclarée, profondeur courante, plage légale — comme la 0.12.2). L'interpolation
entre arrêts se fait **en YUV à la profondeur courante**, dans le **noyau C** (`mvk_spl_gradient`),
avec **tramage** (dither ordonné Bayer 8×8, ±½ LSB) pour éviter le **banding** en 8 bits.

**L'animation vient gratuitement.** Le fond est déjà **enregistré dans les mémoires** et
**interpolé pendant les transitions** (0.12.0), sur **tous** les champs du dégradé (arrêts,
positions, angle, douceur). Donc : mémoire A (une frontière à gauche) → mémoire B (la même à
droite), **rappel avec une durée = un wipe animé qui balaie l'écran**. Idem un fondu de couleurs
(bleu→rouge) ou un angle qui tourne. Aucune mécanique nouvelle. Bascule `source`↔`gradient` = à
mi-course (pas d'entre-deux honnête).

**Coût mesuré** (1080p, banc peu chargé) :

| | 720p50 | 1080p50 | 1080p25 |
|---|---|---|---|
| **statique** (rendu caché, recopie) | 0,3 ms | **0,3 ms** | 0,3 ms |
| **animé** 2 arrêts (wipe) | ~6 ms | **~13 ms** | ~13 ms |
| **animé** 3-4 arrêts | ~8 ms | **~20 ms** | ~20 ms |

Un dégradé **statique** ne coûte **rien** (rendu une fois, mis en cache, recopié — comme un fond
uni). **Animé plein cadre**, il est refait à chaque trame :

- **720p50** : confortable ;
- **1080p50** : un wipe **2 arrêts** tient le budget (~13 ms) avec un peu de marge pour les box ;
- **1080p50, 3-4 arrêts** : **au budget** (~20 ms) — **avec des box composées par-dessus, la
  trame déborde pendant le balayage** (comme la rotation animée). Réservez le wipe animé riche au
  **720p** ou au **1080p25** ; en 1080p50, un wipe 2 arrêts passe.

Le noyau a son garde-fou habituel : **repli numpy identique** (`bg` reste correct sans
`bobi-compute ≥ 0.16`, aux coûts numpy — ~250 ms/trame animé, donc **statique seulement** sur
vieille image) et **`mvk_miss.gradient`** exposé sur `:8080` (doit rester à 0).

## ★ Couleurs : la colorimétrie DÉCLARÉE fait foi (0.12.2)

Toutes les couleurs du plugin — **bordure**, **ombre**, **clé chroma**, **habillage (ART)**,
**fond** — passent par **une seule conversion**, paramétrée par le **format du conteneur** :

- **matrice** = `colorimetry` déclarée (**BT.601 / BT.709 / BT.2020**) ;
- **échelle** = `bit_depth` (8/10/12), bornes **calculées** par `2^(depth−8)`, jamais tabulées ;
- **plage légale** (broadcast) par défaut ; écrêtage **jamais silencieux**.

**Preuve** — barres de couleur 100 %, 8 bits, plage légale : les **16 valeurs de référence**
(SMPTE/EBU) tombent **exactement**, en 601 **et** en 709 :

| | blanc | jaune | cyan | vert | magenta | rouge | bleu | noir |
|---|---|---|---|---|---|---|---|---|
| **709** | 235/128/128 | 219/16/138 | 188/154/16 | 173/42/26 | 78/214/230 | 63/102/240 | 32/240/118 | 16/128/128 |
| **601** | 235/128/128 | 210/16/146 | 170/166/16 | 145/54/34 | 106/202/222 | 81/90/240 | 41/240/110 | 16/128/128 |

En **10 bits**, le rouge 709 donne **250/409/960** — la valeur exacte du standard (un simple ×4 de
la valeur 8 bits arrondie donnerait 252 : on **calcule**, on ne multiplie pas). En **BT.2020**, les
couleurs **changent** (le vert y est plus lumineux : Kg = 0,678 vs 0,715) — **c'est le but**.

> ⚠ **Les couleurs d'une composition existante peuvent légèrement changer.** Elles étaient
> **fausses** : `_hex_to_yuv` (bordure/ombre/clé chroma) et l'habillage portaient une matrice
> **BT.601 pleine échelle codée en dur**, quelle que soit la colorimétrie du conteneur — dans un
> pipeline 709 (le cas de ce parc), un rouge de bordure n'était pas le rouge demandé. Elles sont
> désormais **conformes au format déclaré**.

**Si `colorimetry` est absent du format**, le moteur **suppose BT.709 et le DIT** (log au boot +
`colorimetry_assumed` dans `/state`) — il ne devine pas en silence.

## ⚠ Bug corrigé au passage : l'opacité (et l'adoucissement) ignorés à l'intérieur des box

Le repli numpy ne mélangeait que le **ruban** de bord et **copiait l'intérieur**, en supposant qu'il
y vaut alpha = 256. **Faux** dès que le masque n'y est pas plein :

- en **4:2:0**, la marge chroma est **deux fois plus petite** que le rect chroma → l'intérieur
  n'est pas vide : une box **semi-transparente** gardait une **chroma à pleine opacité** (la luma
  fondait, la couleur non) ;
- sur une box **tournée + rognée**, l'intérieur « sûr » ne l'était plus : **adoucissement et coins
  y étaient ignorés**.

Mesuré sur un cas de référence (fond et source **plats**, donc valeur juste calculable depuis le
masque) : la 0.9.x était **fausse sur 18,4 % des pixels de la box** (erreur max **119**) ; la 0.12.2
est **exacte à 100 %** — **kernel et repli numpy**. Le kernel faisait déjà le bon calcul : c'est le
**repli** qui mentait — soit exactement le piège « un repli qui diverge du kernel est un bug
invisible au banc et bien réel en prod ».

## Non-régression : la base, redéfinie honnêtement

- **Octet-identique à la 0.9.x** sur tout le compositing **sans couleur** (géométrie, gather, alpha,
  adoucissement, coins, opacité, rotation, crop, miroir), 720p et 1080p, 422/420/444, **kernel et
  repli**, **trame entière et mode tranche** — `max|Δ| = 0`.
- **Divergences VOULUES et prouvées** : les **couleurs** (elles étaient fausses), l'**ombre** (le
  flou s'épand enfin vers l'extérieur), l'**opacité/adoucissement à l'intérieur** (ci-dessus), et le
  cas `genlock` + fond non câblé (la 0.11.x publiait du noir sans composer les box).
- **Invariant absolu, toujours vérifié** : `kernel ≡ repli numpy` et `trame entière ≡ mode tranche`
  (`max|Δ| = 0`, 0 repli mvk sur 4 623 appels).
