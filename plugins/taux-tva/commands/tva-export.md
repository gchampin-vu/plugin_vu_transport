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

- **les pays** : codes (`FR,DE,IT`), noms français (`France, Allemagne`), `UE`
  pour les 27, `VU` pour le périmètre d'équipe. `GR` est traduit en `EL` —
  **la Grèce porte le code EL dans les nomenclatures TVA, pas GR.**
- **la forme du fichier**, et c'est le vrai choix de cette commande :

| Ce que la personne veut faire | `forme` |
|---|---|
| Une ligne par pays, les taux en colonnes — le tableau de comparaison classique | `pays` |
| Croiser un taux avec une famille de produits, charger dans Power BI | `categories` — une ligne par pays × catégorie × taux |

En cas de doute, `pays`. Passe à `categories` dès que la question parle de
**produits, de familles, de transport de personnes, de livraison ou de
restauration** : c'est là que la colonne `rate_categories` répond, et le tableau
par pays ne l'a pas.

**2. Vérifie ce qui ne pourra pas entrer dans le fichier — avant d'exporter.**

La source ne couvre **que les 27 États membres**. Si le périmètre demandé
contient la **Suisse**, la **Norvège**, le **Royaume-Uni**, ou un territoire à
régime particulier (**Canaries, DOM, Madère, Açores, Corse**), dis-le
**maintenant**, pas après. `tva_pays` donne la liste et dit où chercher la
réponse ailleurs.

Un fichier « des taux de TVA de nos pays » où la Suisse manque sans que
personne ne l'ait remarqué n'est pas un fichier incomplet : c'est un fichier
faux.

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
- **Tu ne complètes pas un pays manquant de mémoire.** Si la Suisse n'est pas
  dans la source, elle n'est pas dans le fichier — on ne bouche pas un trou avec
  un taux dont on ne sait pas d'où il vient.
