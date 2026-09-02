---
name: taux-tva
description: Consulter les taux de TVA des 27 Etats membres de l'Union - taux normal, taux reduits, super-reduit, taux parking, devise, et la categorie de biens a laquelle chaque taux reduit s'applique. Active des qu'une question porte sur un taux de TVA, sur le taux applicable dans un pays, sur une comparaison de taux entre pays, sur ce qui change entre deux releves, ou des qu'il faut extraire des taux en CSV pour Excel ou Power BI. Declenche aussi sur "TVA", "VAT", "taux normal", "taux reduit", "super-reduit", "taux parking", "quel taux en Espagne", "TVA transport de personnes", "taux de TVA par pays", "export des taux".
---

# Consulter les taux de TVA de l'Union

Les outils sont exposés par le serveur MCP `tva` du même plugin. Ils sont
**tous en lecture** : le connecteur fait un seul appel, en GET, sans paramètre,
sur une source publique. Rien de Vente-unique n'en sort — aucun identifiant,
aucun numéro de TVA, aucune donnée client.

Ce connecteur remplace le script Power Query qui appelait la même source et n'en
gardait que trois colonnes. Il en rend dix, dont celle qui répond réellement aux
questions qu'on se pose : **à quoi s'applique chaque taux réduit**.

## Les trois choses à ne jamais oublier

### 1. Le périmètre s'arrête aux 27 États membres

Ni la **Suisse**, ni la **Norvège**, ni le **Royaume-Uni** — trois pays où le
groupe vend. Le serveur ne rend pas une ligne vide pour eux : il les **nomme**,
dit pourquoi, et dit où chercher.

Quand une question les concerne, la bonne réponse n'est pas « je n'ai pas
trouvé », c'est **« cette source ne les couvre pas, voici où c'est »**. Les deux
phrases n'ont pas le même sens pour la personne qui écoute.

### 2. La Grèce porte le code **EL**, pas GR

C'est la nomenclature TVA de l'Union, pas l'ISO 3166. `tva_taux(pays="GR")` est
traduit — mais un rapprochement fait à la main entre ce fichier et un référentiel
ISO **perd la Grèce en silence**. Si tu croises ces taux avec une autre table,
dis-le.

### 3. Un taux se cite **avec sa date de relevé**

Chaque rendu la porte en tête, et ce n'est pas décoratif : la règle d'équipe est
qu'on n'invente jamais un chiffre, et un taux sans date est un chiffre qui
deviendra faux sans prévenir. **Recopie la date quand tu restitues un taux.**

Le connecteur garde un cache local de 24 h. Si la question porte sur l'instant —
« est-ce que le taux a changé » — appelle **`tva_rafraichir`** d'abord.

## Et la limite qui les résume toutes

**Ce connecteur n'est pas une référence fiscale.** VAT Comply est une source
publique, gratuite, sans engagement de mise à jour.

| Ce à quoi il sert | Ce à quoi il ne sert pas |
|---|---|
| Comparer 27 pays d'un coup | Établir le taux d'une facture |
| Repérer qu'un taux a bougé | Justifier une déclaration |
| Préparer une question à poser à la comptabilité | Paramétrer un ERP sans contrôle |

Dès qu'un taux va **servir à facturer**, la référence est l'administration
fiscale du pays, et le paramétrage de l'ERP fait foi. Dis-le, une fois, sans
insister.

## Les outils, dans l'ordre où on s'en sert

| Question | Outil |
|---|---|
| « Quel taux en Espagne », « compare FR, DE, IT » | **`tva_taux`** |
| « Quel taux sur le transport de personnes en Italie » | **`tva_taux(categorie=...)`** |
| « Pourquoi 5,5 % en France », « que couvre le taux réduit portugais » | **`tva_detail`** |
| « Quelles catégories existent » | **`tva_categories`** — à lire avant de passer `categorie` |
| « La Suisse ? », « c'est quoi le code de la Grèce » | **`tva_pays`** — répond hors ligne |
| « Est-ce qu'un taux a changé » | **`tva_rafraichir`** puis **`tva_changements`** |
| « Sors-moi ça dans Excel » | **`tva_export_csv`**, ou la commande `/tva-export` |
| Quelque chose coince | **`tva_doctor`** |

### Le piège des catégories

`categorie` attend un nom **technique et anglais** : `transport_passengers`,
`foodstuffs`, `private_dwellings`, `restaurant`. Un nom approché ne lève pas
d'erreur, il rend une **colonne vide** — et une colonne vide se lit comme « pas
de TVA », ce qui est faux. Lis `tva_categories` avant.

Et surtout : **une cellule vide veut dire « aucun taux réduit déclaré pour cette
catégorie »**, donc que le taux normal s'applique a priori. Ce n'est ni « zéro »
ni « exonéré ». Le serveur le rappelle sous le tableau ; reprends-le dans ta
réponse.

### Les territoires

La source rend **un taux par pays**. Elle ne sait rien des **Canaries** (IGIC,
hors champ de la TVA), de **Madère** et des **Açores** (taux régionaux), de la
**Corse**, des **DOM**, d'**Åland**, de **Livigno**. `tva_detail` signale les
territoires rattachés au pays consulté.

Si la question porte sur une livraison vers l'un d'eux, **le taux national ne
répond pas** — dis-le au lieu de le citer.

## Ce que tu ne fais pas

- **Tu n'exportes pas de CSV sans qu'on te l'ait demandé.** L'export écrit sur
  le disque ; un tableau en session suffit à décider.
- **Tu ne combles pas un manque de mémoire.** Un pays absent de la source reste
  absent de ta réponse. C'est la règle d'équipe : on n'invente jamais un chiffre.
- **Tu ne conclus pas d'un écart entre deux relevés qu'une loi a changé.** Ce
  peut être une correction de la source. `tva_changements` le dit ; garde la
  nuance.
- **Tu ne valides pas un numéro de TVA ici.** Ce connecteur ne fait que les
  taux. La validation d'un numéro passe par VIES, et enverrait un identifiant de
  tiers à un service externe — c'est une autre décision.
