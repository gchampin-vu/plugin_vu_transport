---
name: taux-tva
description: Consulter les taux de TVA de 45 juridictions europeennes - les 27 Etats membres et XI depuis la base officielle de la Commission (TEDB), et 17 juridictions hors Union dont la Suisse, la Norvege et le Royaume-Uni depuis un jeu de donnees tenu a la main. Taux normal, reduits, super-reduit, parking, exemptions, devise, categories de biens, codes de nomenclature douaniere et date d'effet. Active des qu'une question porte sur un taux de TVA, sur le taux applicable dans un pays, sur une comparaison de taux entre pays, sur ce qui change entre deux releves, ou des qu'il faut extraire des taux en CSV pour Excel ou Power BI. Declenche aussi sur "TVA", "VAT", "taux normal", "taux reduit", "super-reduit", "taux parking", "quel taux en Espagne", "TVA en Suisse", "TVA au Royaume-Uni", "TVA transport de personnes", "taux de TVA par pays", "code CN", "nomenclature douaniere", "export des taux".
---

# Consulter les taux de TVA en Europe

Les outils sont exposés par le serveur MCP `tva` du même plugin. Ils sont **tous
en lecture** : deux appels, l'un en POST SOAP dont les seuls paramètres sont des
codes pays et une date, l'autre en GET sur un fichier statique. Rien de
Vente-unique n'en sort — aucun identifiant, aucun numéro de TVA, aucune donnée
client.

## La chose à comprendre avant tout le reste : deux sources, deux autorités

| | Les 27 + **XI** | Les 17 autres, dont **CH, GB, NO** |
|---|---|---|
| Source | **TEDB**, Commission européenne | jeu de données MIT |
| Autorité | **officielle**, alimentée par les États membres | **tenue à la main** |
| Ce qu'on a | taux, catégories, codes CN, commentaires officiels, date d'effet | taux et devise, rien d'autre |

La colonne `provenance` porte cette différence sur **chaque ligne** : `TEDB` ou
`vatnode (main)`.

**Ne présente jamais un taux suisse avec l'assurance d'un taux français.** Quand
un rendu contient une ligne tenue à la main, le serveur affiche une réserve
nommant l'administration où vérifier (AFC, HMRC, Skatteetaten). **Reprends-la**
dans ta réponse — c'est le genre de nuance qui disparaît à la reformulation, et
c'est justement celle qui compte.

## Les trois pièges de vocabulaire

### 1. « UE » ne veut pas dire « tout »

`UE` désigne les **27 États membres**. `Europe` ou `tous` désignent les **45
juridictions**. Confondre les deux fait apparaître la Suisse dans une réponse
sur les États membres — ou l'en fait disparaître alors qu'on la voulait.

Si la question dit « en Europe », demande-toi si elle veut dire *dans l'Union*
ou *sur le continent*. Les deux réponses sont justes, elles ne sont pas la même.

### 2. La Grèce porte le code **EL**, pas GR

C'est la nomenclature TVA de l'Union, pas l'ISO 3166. `pays="GR"` est traduit —
mais un rapprochement fait à la main entre ce fichier et un référentiel ISO
**perd la Grèce en silence**. Si tu croises ces taux avec une autre table,
dis-le.

### 3. **XI n'est pas GB**

L'Irlande du Nord reste dans le champ TVA de l'Union pour les biens. TEDB la
sert comme une juridiction à part entière — avec ses catégories — et une
livraison à **Belfast** ne se traite pas comme une livraison en
Grande-Bretagne. `Belfast` est un alias reconnu.

## Un taux se cite avec sa date

Chaque rendu porte la date du relevé en tête, et les lignes TEDB portent en plus
la **date d'effet** du taux. Ce n'est pas décoratif : la règle d'équipe est qu'on
n'invente jamais un chiffre, et un taux sans date est un chiffre qui deviendra
faux sans prévenir. **Recopie la date quand tu restitues un taux.**

Le cache local vaut 24 h. Si la question porte sur l'instant — « est-ce que le
taux a changé » — appelle **`tva_rafraichir`** d'abord.

## La limite qui résume tout

**Ce connecteur n'est pas une référence fiscale.**

| Ce à quoi il sert | Ce à quoi il ne sert pas |
|---|---|
| Comparer 45 juridictions d'un coup | Établir le taux d'une facture |
| Repérer qu'un taux a bougé | Justifier une déclaration |
| Relier une catégorie fiscale à un produit par son code CN | Paramétrer un ERP sans contrôle |

Dès qu'un taux va **servir à facturer**, la référence est l'administration
fiscale du pays. Dis-le, une fois, sans insister.

## Les outils, dans l'ordre où on s'en sert

| Question | Outil |
|---|---|
| « Quel taux en Espagne », « compare FR, DE, IT », « et en Suisse ? » | **`tva_taux`** |
| « Quel taux sur le transport de personnes en Italie » | **`tva_taux(categorie=...)`** |
| « Pourquoi 5,5 % en France », « que couvre le taux réduit portugais », « quels produits » | **`tva_detail`** |
| « Quelles catégories existent » | **`tva_categories`** — à lire avant de passer `categorie` |
| « Qu'est-ce qui est couvert, et par quelle source » | **`tva_pays`** — répond hors ligne |
| « Est-ce qu'un taux a changé » | **`tva_rafraichir`** puis **`tva_changements`** |
| « Sors-moi ça dans Excel » | **`tva_export_csv`**, ou la commande `/tva-export` |
| Quelque chose coince | **`tva_doctor`** — il teste les deux sources séparément |

### Le piège des catégories

`categorie` attend un nom **technique et anglais** : `TRANSPORT_PASSENGERS`,
`FOODSTUFFS`, `PRIVATE_DWELLINGS`, `RESTAURANT`. Il y en a **87**. Un nom
approché ne lève pas d'erreur, il rend une **colonne vide** — et une colonne vide
se lit comme « pas de TVA », ce qui est faux. Lis `tva_categories` avant.

Une cellule vide veut dire **« aucun taux réduit déclaré pour cette catégorie »**,
donc que le taux normal s'applique a priori. Ce n'est ni « zéro » ni « exonéré ».

Et les catégories **n'existent que pour les 27 et XI**. Demander une catégorie
pour la Suisse rendra une colonne vide : ce n'est pas que la Suisse n'a pas de
taux réduit, c'est que la source tenue à la main ne les qualifie pas.

### Les exemptions ne sont pas un taux zéro

TEDB distingue `EXEMPTED`, `OUT_OF_SCOPE` et `NOT_APPLICABLE` d'un taux à 0 %.
`tva_detail` les liste à part. Sur une facture, ces situations ne se traitent pas
comme un taux nul — ne les confonds pas, et ne les convertis pas en chiffre.

### Les codes de nomenclature douanière

Pour les juridictions TEDB, chaque catégorie porte les **codes CN** concernés,
avec leur libellé douanier. C'est ce qui relie une catégorie fiscale à un
**produit réel du catalogue** — la question « quel taux sur ce meuble » se
raccroche là. `tva_detail` en montre un aperçu, l'export `categories` les sort
tous.

### Les territoires

Aucune des deux sources ne couvre les **Canaries** (IGIC), **Ceuta-Melilla**
(IPSI), **Madère** et les **Açores** (taux régionaux), la **Corse**, les
**DOM**, **Åland**, **Büsingen**, **Livigno**, le **Mont Athos**. Elles rendent
un taux par juridiction.

Si la question porte sur une livraison vers l'un d'eux, **le taux national ne
répond pas** — dis-le au lieu de le citer. C'est la première cause d'erreur de
facturation sur ce sujet, précisément parce qu'un taux national existe et se
cite par réflexe.

## Ce que tu ne fais pas

- **Tu n'exportes pas de CSV sans qu'on te l'ait demandé.** L'export écrit sur
  le disque ; un tableau en session suffit à décider.
- **Tu ne gommes pas la provenance.** Si le rendu dit qu'un taux est tenu à la
  main, ta réponse le dit aussi. C'est l'information la plus facile à perdre en
  reformulant, et la plus coûteuse à perdre.
- **Tu ne combles pas un manque de mémoire.** Une juridiction absente du relevé
  reste absente de ta réponse. On n'invente jamais un chiffre.
- **Tu ne conclus pas d'un écart entre deux relevés qu'une loi a changé.** Ce
  peut être une correction de la source. `tva_changements` le dit ; garde la
  nuance.
- **Tu ne valides pas un numéro de TVA ici.** Ce connecteur ne fait que les
  taux. La validation passe par VIES, et enverrait un identifiant de tiers à un
  service externe — c'est une autre décision.
