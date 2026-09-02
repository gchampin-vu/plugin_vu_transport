---
description: Exporte des taux de change de chancellerie DGFiP en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande choisit le bon mode et la bonne date, puis ecrit le fichier complet.
---

Tu produis un **export CSV de taux de change** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit, et que la conversation ne garantit pas

Un rendu en session est **borne** : il s'arrete au budget d'affichage. L'export,
lui, ecrit le perimetre entier, et il ne passe pas par `/records` — donc il n'a
pas le plafond `offset + limit <= 10 000` qui empeche de lire les 24 103 lignes
du jeu.

C'est aussi la seule sortie acceptable pour un controle de facture en devise ou
une piece a joindre a un mail : sans elle, la personne recopie a la main ce que
le connecteur a affiche, et c'est exactement la que les chiffres se deforment.

## Comment tu conduis

**1. Choisis le mode. C'est la seule vraie decision, et elle n'est pas neutre.**

| Ce que la personne veut | Mode | Ce que le fichier porte |
|---|---|---|
| « le taux applicable au 1er juillet », un tableau de conversion, un controle de facture | **`mode="en_vigueur"`** (defaut) | une ligne par devise, le taux en vigueur a la date, avec sa `date_effet` |
| une serie, un graphique, alimenter un modele Power BI | **`mode="brut"`** | les lignes du jeu telles quelles : une par couple (devise, pays) et par publication |

En cas de doute, c'est `en_vigueur`. Le mode `brut` porte des doublons par pays
et pas un taux par mois : livre a quelqu'un qui attend « le taux de juillet », il
produit un faux.

**2. Resous la date et les devises, ne les devine pas.**

- Une date manquante vaut aujourd'hui, et c'est presque toujours ce qu'on veut.
  Un mois seul (« juillet 2026 ») est resolu au 1er : l'outil l'annonce,
  recopie-le.
- Un code ISO ne se devine pas. Si la personne parle de « la couronne » ou du
  « dollar », passe par **`taux_devises`** : il y a trois couronnes et une
  dizaine de dollars dans le jeu.
- **Ne filtre jamais sur une egalite de date.** Ce n'est pas un detail de style :
  un taux n'est republie que quand il change, donc `date = "2026-09-01"` rend 44
  devises sur 189. Si tu ecris un `where` a la main en mode brut, borne avec
  `>=` / `<=`, jamais `=`.

**3. Rends compte, et rends compte de ce qui manque.**

Une fois le fichier ecrit, donne dans cet ordre :

1. **le chemin exact du fichier** ;
2. le nombre de lignes et de colonnes ;
3. le mode et la date retenus, recopies depuis l'entete rendu par l'outil ;
4. **si le fichier est vide, dis-le en premier et ne le livre pas** : le filtre
   ne correspond a rien, et un CSV vide se lit comme « il n'y a pas de taux ».

Precise aussi ce qui change la lecture du fichier :

- `taux` est le nombre d'**euros** que vaut **une** unite de la devise. Une
  colonne de taux sans cette phrase se lit dans les deux sens ;
- `date_effet` n'est pas la date demandee : c'est la date de mise en vigueur du
  taux. Un taux de fevrier dans un export de septembre est normal ;
- `monnaievigueur = 0` signale une devise qui n'a plus cours **aujourd'hui**.
  L'export `en_vigueur` les inclut : dis-le, la personne filtrera si elle veut.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour te rassurer sur un chiffre.** Si la question est
  « quel est le taux », la reponse est `taux_du_jour` ou `taux_a_la_date` dans la
  session, pas un fichier.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans
  `~/.taux-de-change-mcp/exports`. Un CSV depose dans un dossier synchronise part
  chez tout le monde, et la base de connaissance ne porte pas de donnees.
- **Tu ne convertis pas les montants dans le fichier** et tu n'arrondis pas les
  taux : c'est une extraction, pas une analyse. Pour un montant converti, c'est
  `taux_convertir`, qui rend le taux utilise et sa date d'effet a cote du
  resultat.
- **Tu ne presentes pas ces taux comme un cours de marche.** Ce sont les taux de
  chancellerie : une reference administrative mensuelle. Le taux qui s'applique a
  un reglement transporteur depend du contrat, et il se lit dans
  `02_TRANSPORTEURS/<NOM>/01_contrat/`.
