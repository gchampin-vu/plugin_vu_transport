---
description: Exporte un perimetre Peripass en CSV, ouvrable dans Excel. Decris le perimetre en francais, la commande le traduit en filtres, interroge les deux sites et pagine la collection entiere.
---

Tu produis un **export CSV Peripass** a partir du perimetre decrit dans
`$ARGUMENTS`.

## Ce que cette commande garantit

Un rendu en session est **borne** : il s'arrete au budget d'affichage. L'export
**pagine la collection entiere** sur le perimetre demande, et sur **les deux
sites** si les deux sont vises.

C'est la sortie a produire pour une revue de creneaux, un litige de retard avec
un transporteur, ou une etude de temps d'immobilisation : ces sujets se traitent
sur un fichier, pas sur un tableau tronque.

## Comment tu conduis

**1. Traduis le perimetre.**

Reformule en une phrase ce que tu as comprise, puis resous :

- **le site** : `site="Moulins"`, `"Montbeugny"`, `"Amblainville"`, `"Oise"` ou
  `"Allier"` sont compris et traduits. Vide = les deux sites, et chaque ligne
  porte sa colonne `site`. **Ne retire jamais cette colonne du fichier** : sans
  elle, deux lignes de deux tenants se confondent.
- **le champ de periode**, et c'est le point qui rend un export faux sans
  prevenir. Peripass **ignore en silence** une periode sans champ de reference et
  rend alors toute la base. `timestamp_field` accepte les mots metier :
  `"creneau"` (SlotStart), `"arrivee"` (Arrived), `"check-in"`, `"depart"`.
  Demande-toi lequel repond a la question posee : un respect de creneau se lit
  sur SlotStart, un temps de presence sur Arrived.
- **un champ personnalise** -> **`peripass_fields`** pour le nom technique exact,
  sensible a la casse et accentue. Ou **`peripass_lexique`**, qui donne les cles
  et leur taux de remplissage.

**Si le perimetre n'a aucune borne de periode, demande-la** avant d'exporter.
Sur Peripass ce n'est pas qu'une question de temps : le quota compte les
**ressources lues**, pas les requetes. Un export sans borne consomme la fenetre.

**2. Choisis le bon chemin.**

| Situation | Outil |
|---|---|
| Un perimetre qui tient sous 6 000 lignes par site | **`peripass_export_csv`** — appel direct a l'API |
| Un historique long, un croisement, une distribution sur trois mois | **`peripass_sync`** une fois, puis **`peripass_export_sql`** — sans plafond et **sans quota** |

Le second chemin est le bon des que la meme question risque de revenir : le
cache se relit sans rien consommer.

**3. Rends compte, et rends compte de ce qui manque.**

1. **le chemin exact du fichier** ;
2. le nombre de lignes et de colonnes, **et le detail par site** ;
3. les filtres appliques, recopies depuis l'entete rendu par l'outil ;
4. **si un site n'a pas repondu, ou si l'export est tronque, dis-le en premier.**
   Un fichier qui ne couvre qu'AUV alors qu'on attendait les deux sites n'est pas
   un fichier incomplet, c'est un fichier faux.

Precise le remplissage des champs qui portent la lecture : « Transporteur » n'est
renseigne que sur une partie des lignes, et ce qui est vide n'est pas « un autre
transporteur ».

## Ce que tu ne fais pas

- **Tu n'exportes pas pour repondre a « combien » ou « combien de temps ».**
  C'est `peripass_summary`, qui agrege cote serveur et rend la moyenne, la
  mediane et le p90.
- **Tu n'ecris jamais dans la bibliotheque d'equipe.** L'export va dans le
  dossier local du connecteur.
- **Tu ne compares pas deux sites sans le dire.** Rien ne garantit qu'AUV et AMB
  portent les memes libelles de profil, de quai ou d'activite — les formats de
  quai different deja (`B-12` a AUV, `E - 32` a AMB).
