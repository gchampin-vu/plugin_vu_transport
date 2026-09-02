---
description: Exporte en CSV une serie d'indice europeen - gazole, inflation ou salaire minimum. Decris le perimetre en francais, la commande le traduit en filtres et rend la serie complete, sans plafond d'affichage.
---

Tu produis un **export CSV d'indice europeen** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit

Un rendu en session est **borne** : il s'arrete au budget d'affichage, et le
gazole a plus de mille semaines d'historique par pays. L'export rend la **serie
complete** sur le perimetre demande.

C'est la sortie a produire pour une etude de revision tarifaire, un controle de
surcharge carburant ou un dossier de negociation : ces sujets se traitent sur un
fichier, pas sur un tableau tronque.

## Comment tu conduis

**1. Traduis le perimetre.** Reformule en une phrase ce que tu as compris, puis
resous :

- **l'indice** : `gazole`, `inflation` ou `salaire_minimum`. S'il n'est pas
  explicite, demande - « les indices » ne veut rien dire dans un fichier.
- **les pays** : noms francais et codes ISO acceptes, separes par des virgules.
  Vide = les principaux pays de livraison de Vente-unique. `indices_pays()`
  donne la liste et signale les ecarts de code (**la Grece est `EL` chez
  Eurostat et `GR` dans le bulletin petrolier**).
- **la periode** : `depuis` et `jusqu_a` acceptent `2024`, `2024-06`,
  `2024-S1`, `2024-Q3`, `2024-06-15`.
- **la variante**, et c'est elle qui change le chiffre :
  - gazole → `produit` (diesel par defaut, ou euro95, heating_oil,
    fuel_oil_1, fuel_oil_2, LPG) et `taxes` (`ttc` ou `ht`). **Hors taxes pour
    comparer deux pays**, TTC pour ce que paie le transporteur.
  - inflation → `mesure` : `annuel`, `mensuel`, `moyenne_12m` (celle des
    clauses d'indexation), `indice` (la seule comparable entre deux dates).
  - salaire_minimum → `devise` : `EUR`, `NAC`, `PPS`.

**Si la periode n'a aucune borne, demande-la** avant d'exporter : le gazole
remonte a 2005, et un fichier de 30 000 lignes n'est pas ce qu'on voulait.

**2. Appelle `indices_export_csv`** avec ces parametres.

**3. Rends compte, et rends compte de ce qui manque.**

1. **le chemin exact du fichier** ;
2. le nombre de lignes et de colonnes ;
3. **le millesime de la source**, recopie depuis l'en-tete rendu par l'outil -
   un prix de gazole sans sa semaine ne veut rien dire ;
4. **si un pays n'a pas de donnee, dis-le en premier.** Un fichier qui ne
   couvre que quatre pays sur six n'est pas incomplet, il est trompeur.

Deux avertissements a **recopier tels quels** s'ils apparaissent :

- « NOM D'HOTE NON VERIFIE » : le classeur du bulletin a ete telecharge en mode
  `chaine-seule`. A dire si le fichier part dans un echange contractuel.
- **La colonne `source`, et la colonne `unite`.** Depuis que le gazole a trois
  sources, un export multi-pays peut porter des euros par 1000 litres, des pence
  par litre et des couronnes par litre DANS LA MEME COLONNE `valeur`. C'est
  voulu - le connecteur ne convertit pas, faute de taux de change a la date -
  mais il faut le dire en livrant le fichier, sinon quelqu'un sommera la colonne.
  Rappelle que la comparaison inter-pays se fait sur la variation en pourcentage.
- **Le parametre `source`** (`auto` par defaut, `europe`, `national`). Si
  l'utilisateur veut un export homogene en euros par 1000 litres pour comparer
  des pays, c'est `source='europe'` qu'il faut - au prix du Royaume-Uni, qui y
  est fige en 2020 et sortira alors avec son avertissement.
- **Le parametre `poste`** sur l'inflation (`total` par defaut, `carburants`).
  C'est le seul moyen d'avoir une mesure carburant pour la SUISSE - et c'est un
  indice, pas un prix : a ecrire dans le message qui accompagne le fichier.
- pays sans salaire minimum legal (DK, IT, AT, FI, SE, NO, IS, CH) : ils
  sortent sans ligne, et **une case vide n'est pas un zero**.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour repondre a « de combien ca a monte ».** C'est
  `indices_variation`, qui compare la moyenne de deux periodes et rend l'ecart
  en pourcentage.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur : un CSV depose dans un dossier synchronise part
  chez tout le monde, et la base de connaissance ne porte pas de donnees.
- **Tu n'appliques aucune clause tarifaire.** Le fichier porte l'indice ; la
  formule de revision est dans le contrat du transporteur, sous
  `02_TRANSPORTEURS/<NOM>/02_tarif/`.
