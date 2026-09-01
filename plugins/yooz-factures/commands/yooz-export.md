---
description: Exporte un perimetre de factures Yooz en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande resout le tiers, la societe et la periode, puis ecrit le fichier.
---

Tu produis un **export CSV de factures Yooz** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit

Un rendu en session est **borne**. L'export lit le perimetre entier et l'ecrit
dans un fichier. C'est la sortie a produire pour un controle de facture, un
rapprochement avec l'estimation du back-office, ou une piece a joindre a une
demande d'avoir.

## Comment tu conduis

**1. Resous le tiers. Ne compose JAMAIS un code a la main.**

C'est le piege qui rend un export vide sans prevenir. Yooz stocke le code tiers
avec son remplissage d'espaces et son suffixe : `HVIR           (H)`, pas `HVIR`.
La grille n'accepte que la forme exacte, et une forme approchee rend **zero
document en HTTP 200**, ce qui se lit comme « ce transporteur ne nous a rien
facture ».

Appelle donc **`yooz_resolve_third`** sur le nom maison, et regarde ce qu'il
rend :

- **plusieurs entites** ? Les additionner est une **decision**, pas un
  automatisme. Sur « VIR » il rend `VIR TRANSPORT`, `VIR BENELUX`, `JP HOME` et
  `AGEDISS - JP HOME`. Demande a l'utilisateur ce qu'il veut dans le fichier, ou
  exporte tout **en gardant la colonne `thirdPartyName`** pour qu'il puisse
  ventiler lui-meme.
- **plusieurs societes** ? Un export qui n'en couvre qu'une est partiel. Dis
  laquelle il couvre.
- **rien** ? Ne conclus pas a une absence de facturation. Verifie la fraicheur du
  cache avec `yooz_status`, et relance `yooz_sync` si besoin.

**2. Choisis le bon chemin.**

| Situation | Outil |
|---|---|
| L'etat courant de Yooz, quelques centaines a quelques milliers de lignes | **`yooz_live_invoices`** pour verifier le perimetre, puis **`yooz_export_csv`** sur le cache |
| Un historique long, un classement sur plusieurs annees, un croisement | **`yooz_sync`** puis **`yooz_export_csv`** avec la requete SQL du perimetre |

`yooz_export_csv` exporte le resultat d'une requete **sur le cache**. Si la
question porte sur l'etat du jour et que le cache date, resynchronise d'abord :
`yooz_status` dit de quand il date.

**3. Rends compte, et rends compte de ce qui manque.**

1. **le chemin exact du fichier** ;
2. le nombre de lignes ;
3. le perimetre : tiers resolus, societes, bornes de dates ;
4. **si le fichier est tronque, dis-le en premier.** L'outil le signale.

**Le signe des avoirs est le piege de restitution.** Dans le cache, un avoir
porte un montant **negatif** ; sur la grille en direct il est **positif**, et ce
sont les colonnes `amountSigned` / `totalAmountSigned` qui portent le sens. Dis
dans quel referentiel le fichier a ete produit, sinon la personne qui somme la
colonne se trompe du double sur chaque avoir.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour repondre a « combien ».** C'est `yooz_live_summary`,
  en un appel, avec les avoirs comptes en negatif et leur part isolee.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur — et un export de factures fournisseurs n'a rien a
  faire dans un dossier synchronise.
- **Tu ne recopies aucun montant dans une note de la base de connaissance.** Les
  chiffres vivent dans leur source ; la base porte la methode et la lecture.
