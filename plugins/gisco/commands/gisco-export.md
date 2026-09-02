---
description: Exporte un perimetre geographique GISCO en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande le traduit en filtres et ecrit toutes les lignes, pas seulement celles affichees.
---

Tu produis un **export CSV GISCO** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit, et que la conversation ne garantit pas

Un rendu en session est **borne** : il s'arrete au budget d'affichage et ne
montre qu'une partie des lignes. L'export, lui, ecrit **toutes** les lignes du
perimetre demande. C'est la difference entre regarder et livrer.

C'est aussi la seule sortie acceptable pour une etude de zonage, un
rapprochement avec un fichier transporteur ou une table a charger dans Power BI :
sans elle, la personne recopie a la main ce que le connecteur a affiche, et
c'est exactement la que les chiffres se deforment.

## Comment tu conduis

**1. Verifie d'abord que la couche est la.**

`gisco_couches()` dit ce qui est rapatrie sur ce poste, dans quel millesime et
depuis quand. Une couche absente n'est pas une donnee manquante : c'est une
synchronisation qui n'a pas encore ete faite. Lance `gisco_sync` avant, pas
apres avoir conclu que la donnee n'existe pas.

**2. Traduis le perimetre, ne le devine pas.**

Reformule en une phrase ce que tu as compris, puis resous ce qui doit l'etre :

- un code pays -> attention aux deux qui ne suivent pas l'ISO : **EL** pour la
  Grece, **UK** pour le Royaume-Uni. Un fichier maison en GR ou GB rate ces deux
  pays sans lever d'erreur.
- un nom de region ou de commune -> `gisco_nuts` ou `gisco_lau` d'abord, pour
  obtenir le code exact. Un filtre sur un nom approche sort un fichier
  incomplet qui a l'air complet.
- une notion de « ville » -> demande **laquelle**. `gisco_villes` distingue la
  ville administrative (C), son noyau dense (K) et la zone urbaine
  fonctionnelle (F), et l'ecart de surface va de un a dix.

**3. Choisis le bon chemin d'export.**

| Situation | Outil |
|---|---|
| Un perimetre qui s'exprime en filtres simples (pays, prefixe de code postal, NUTS3, densite) | **`gisco_export_csv`** |
| Un croisement entre deux couches, un regroupement, une condition que les filtres ne disent pas | **`gisco_export_sql`** — lis `gisco_tables()` avant d'ecrire la requete |

**N'ajoute `avec_geometrie=True` que si on te l'a demande.** Un contour de
commune au 01M pese des dizaines de milliers de caracteres : le fichier passe de
quelques centaines de kilo-octets a plusieurs centaines de mega-octets, et c'est
inutile dans un tableur. C'est utile pour une carte Power BI, et seulement la.

**4. Rends compte, et rends compte de ce qui manque.**

Une fois le fichier ecrit, donne dans cet ordre :

1. **le chemin exact du fichier** ;
2. le nombre de lignes et de colonnes ;
3. **le millesime de la couche et sa date de rapatriement**, recopies depuis
   l'en-tete rendu par l'outil. Un decoupage administratif sans millesime est
   inverifiable : les communes fusionnent, les codes postaux naissent ;
4. **si quelque chose manque ou est tronque, dis-le en premier, pas en dernier.**

Precise aussi les colonnes dont le taux de remplissage change la lecture :

- **`pop` vaut 0 pour la France et l'Espagne entieres au millesime LAU 2024.**
  C'est une absence de mesure, pas un chiffre. Pour une question de population
  sur ces deux pays, il faut le millesime 2023.
- **`pop` reste vide pour le Royaume-Uni** : l'ONS ne la publie pas dans la
  couche des districts.
- **un code postal n'a qu'une commune de rattachement** dans GISCO, meme quand
  il en couvre plusieurs. 60110 sort avec Meru, pas avec Amblainville.

## Ce que tu ne fais pas

- **Tu n'exportes pas pour te rassurer sur un chiffre.** Si la question est
  « combien », la reponse est `gisco_repartition`, pas un fichier.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur. Un CSV depose dans un dossier synchronise part
  chez tout le monde, et la base de connaissance ne porte pas de donnees. Le
  connecteur refuse d'ecrire dans un chemin qui ressemble a OneDrive ou
  SharePoint - ne contourne pas ce refus.
- **Tu ne renommes pas les colonnes** et tu ne « nettoies » pas le fichier :
  c'est une extraction, pas une analyse. La colonne `source` (GISCO ou ONS) et
  la colonne `edition` restent dans le fichier, parce que c'est ce qui rend
  l'extraction verifiable.
