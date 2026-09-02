---
description: Exporte les taux de TVA en CSV, ouvrable dans Excel. Décris le périmètre en français, la commande le traduit en codes pays, choisit la forme du fichier et dit ce qui n'a pas pu y entrer.
---

Tu produis un **export CSV des taux de TVA** à partir du périmètre décrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit

Un rendu en session est **borné** et se lit à l'œil. L'export écrit un fichier
daté, avec la **date de relevé sur chaque ligne** — c'est ce qui permet de le
faire circuler sans qu'il devienne un chiffre faux dans trois mois.

C'est la sortie à produire pour un contrôle de facturation multi-pays, un
chargement Power BI, ou un rapprochement avec le paramétrage de l'ERP.

## Comment tu conduis

**1. Traduis le périmètre.**

Reformule en une phrase ce que tu as compris, puis résous :

- **les pays** : codes (`FR,DE,IT`), noms français (`France, Allemagne, Suisse`),
  `UE` pour **les 27 seulement**, `Europe` ou `tous` pour **les 45
  juridictions**, `VU` pour le périmètre d'équipe. `GR` est traduit en `EL` —
  **la Grèce porte le code EL dans les nomenclatures TVA, pas GR** — et
  `Belfast` en `XI`, qui n'est pas `GB`.

  Si la demande dit « en Europe », tranche : *dans l'Union* ou *sur le
  continent* ? Les deux exports sont justes, ils n'ont pas le même contenu.
- **la forme du fichier**, et c'est le vrai choix de cette commande :

| Ce que la personne veut faire | `forme` |
|---|---|
| Une ligne par pays, les taux en colonnes — le tableau de comparaison classique | `pays` |
| Croiser un taux avec une famille de produits, charger dans Power BI | `categories` — une ligne par pays × catégorie × taux, **avec les codes de nomenclature douanière** |

En cas de doute, `pays`. Passe à `categories` dès que la question parle de
**produits, de familles, de transport de personnes, de livraison ou de
restauration** : c'est là que répondent les 87 catégories et la colonne
`codes_cn`, et le tableau par pays ne les a pas.

Rappel : les catégories et les codes CN **n'existent que pour les 27 et XI**.
Une ligne suisse ressortira avec sa catégorie vide — ce n'est pas que la Suisse
n'a pas de taux réduit, c'est que la source qui la couvre ne les qualifie pas.

**2. Vérifie ce qui ne pourra pas entrer dans le fichier — avant d'exporter.**

Deux choses à annoncer **avant** d'écrire le fichier, pas après.

**Les territoires à régime particulier** — **Canaries, Ceuta-Melilla, Madère,
Açores, Corse, DOM, Åland, Büsingen, Livigno, Mont Athos** — ne sont couverts
par **aucune** des deux sources. S'ils sont dans la demande, dis-le maintenant.
`tva_pays` donne la liste et la conséquence de chacun.

**La provenance des lignes hors Union.** Si le périmètre contient la Suisse, le
Royaume-Uni, la Norvège ou une autre juridiction hors Union, le fichier
contiendra leurs taux — mais **tenus à la main**, pas issus de la base
officielle. La colonne `provenance` le porte sur chaque ligne. **Dis-le en
remettant le fichier** : un CSV circule, se recopie, et perd les
avertissements qui n'étaient que dans la conversation.

Un fichier « des taux de TVA de nos pays » où un taux suisse tenu à la main est
présenté au même rang qu'un taux français officiel n'est pas un fichier
incomplet : c'est un fichier trompeur.

**3. Exporte.**

`tva_export_csv(pays=..., forme=...)`. Si la question porte sur l'instant —
« les taux à jour », « est-ce que ça a changé » — appelle **`tva_rafraichir`
avant** : le cache local répond par défaut, et il peut avoir jusqu'à 24 h.

**4. Rends compte, et rends compte de ce qui manque.**

1. **le chemin exact du fichier** ;
2. le nombre de lignes, de colonnes et de pays ;
3. **la date du relevé** et son origine (appel à la source, ou cache) ;
4. **ce qui n'a pas pu entrer dans le fichier**, en premier si c'est le cas.

## Ce que tu ne fais pas

- **Tu ne présentes jamais ce fichier comme une référence fiscale.** VAT Comply
  est une source publique sans engagement de mise à jour. Pour une facture, une
  déclaration ou un paramétrage d'ERP, la référence est l'administration fiscale
  du pays. Ce fichier sert à comparer et à repérer un écart.
- **Tu n'écris jamais dans la bibliothèque d'équipe.** L'export va dans le
  dossier local du connecteur. Un CSV posé dans un dossier synchronisé part chez
  tout le monde, et la base de connaissance ne porte pas de données.
- **Tu ne complètes pas une juridiction manquante de mémoire.** Si elle n'est
  pas dans le relevé, elle n'est pas dans le fichier — on ne bouche pas un trou
  avec un taux dont on ne sait pas d'où il vient.
- **Tu ne retires pas la colonne `provenance` du fichier**, même si on te
  demande « un tableau simple ». C'est la colonne qui empêche de citer un taux
  tenu à la main comme un taux officiel.
