---
description: Exporte un perimetre Shiptify en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande le traduit en filtres et pagine la collection entiere.
---

Tu produis un **export CSV Shiptify** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit, et que la conversation ne garantit pas

Un rendu en session est **borne** : il s'arrete au budget d'affichage et ne
montre qu'une partie des lignes. L'export, lui, **pagine la collection entiere**
sur le perimetre demande. C'est la difference entre regarder et livrer.

C'est aussi la seule sortie acceptable pour un controle de facture, une etude de
plan ou une piece a joindre a un mail : sans elle, la personne recopie a la main
ce que le connecteur a affiche, et c'est exactement la que les chiffres se
deforment.

## Comment tu conduis

**1. Traduis le perimetre, ne le devine pas.**

Reformule d'abord en une phrase ce que tu as comprise du perimetre demande, puis
resous ce qui doit l'etre :

- un nom maison de transporteur ou d'agence -> **`shiptify_resolve`**. « VIR » se
  cherche sur `address_dest.name` avec trois libelles, « XPO » porte quatre
  identifiants. Un filtre approximatif sort un fichier incomplet qui a l'air
  complet.
- un nom de colonne ou de filtre -> **`shiptify_list_paths`**, jamais de
  parametre devine.

**Si le perimetre n'a aucune borne de date, demande-la** avant d'exporter. Sans
date, l'export vaut la base entiere : c'est long, et ce n'est jamais ce qu'on
voulait.

**2. Choisis le bon chemin d'export.**

| Situation | Outil |
|---|---|
| Un perimetre qui tient sous le plafond de pagination | **`shiptify_export_csv`** — il appelle l'API en direct |
| Un historique long, un croisement, plus de 6 000 lignes | **`shiptify_sync`** une fois, puis **`shiptify_export_sql`** — le cache n'a pas ce plafond |

Le plafond du direct est `SHIPTIFY_MAX_PAGES`, soit 6 000 lignes. Si l'export
revient tronque, ne le livre pas tel quel : passe par le cache, ou resserre.

**3. Rends compte, et rends compte de ce qui manque.**

Une fois le fichier ecrit, donne dans cet ordre :

1. **le chemin exact du fichier** ;
2. le nombre de lignes et de colonnes ;
3. les filtres appliques, recopies depuis l'entete rendu par l'outil ;
4. **si l'export est tronque, dis-le en premier, pas en dernier.** Un export
   tronque sans le dire est un chiffre faux qui part dans un mail.

Precise aussi les colonnes dont le taux de remplissage change la lecture :
`cost` vaut zero partout — le montant est `price` — et les poids et volumes ne
sont renseignes que sur environ deux tiers des lignes.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour te rassurer sur un chiffre.** Si la question est
  « combien », la reponse est `shiptify_summary`, pas un fichier.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur. Un CSV depose dans un dossier synchronise part
  chez tout le monde, et la base de connaissance ne porte pas de donnees.
- **Tu ne renommes pas les colonnes** et tu ne « nettoies » pas le fichier :
  c'est une extraction, pas une analyse. Les objets imbriques sortent en
  colonnes pointees (`carrier.name`, `address_dest.zipcode`), et c'est voulu.
