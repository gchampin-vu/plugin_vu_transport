---
name: taux-de-change
description: Donner le taux de change en vigueur a une date et convertir un montant, depuis les taux de chancellerie DGFiP (open data). Active des qu'une question porte sur un taux de change, une conversion de devise, un montant en devise etrangere a passer en euros ou l'inverse, une facture ou un tarif libelle dans une autre monnaie, ou l'evolution d'une devise. Declenche aussi sur "taux de change", "cours de l'euro", "convertir en euros", "combien vaut", "taux de chancellerie", "DGFiP taux", un code ISO de devise (USD, GBP, PLN, CHF, TRY...), "facture en dollars", "tarif en livres", "export des taux".
---

# Donner un taux de change, et le donner juste

Les outils sont exposes par le serveur MCP `taux` du plugin `taux-de-change`. La
source est le jeu de donnees public `dgfip-taux-de-change` de
data.economie.gouv.fr : les **taux de chancellerie**, publies mensuellement.
Aucune cle, aucune mise en service — si un outil repond, c'est que tout est en
place.

## La seule chose a retenir avant d'appeler quoi que ce soit

**Un taux n'est republie que quand il change.** Il n'y a donc pas une ligne par
devise et par mois. Au 2026-09-01, 44 devises seulement portaient une ligne datee
de ce jour, alors que 189 avaient un taux applicable : la livre sterling n'avait
pas bouge depuis fevrier, le zloty depuis avril, la couronne danoise depuis 2020.

Consequence directe : **un filtre d'egalite sur la date rend un resultat faux qui
ne leve aucune erreur.** C'est pour ca que `taux_du_jour` et `taux_a_la_date`
existent : ils retiennent, par devise, la derniere publication anterieure ou
egale a la date, et rendent cette date d'effet a cote du taux.

`taux_guide` porte les cinq pieges du jeu de donnees et repond hors ligne, en
quelques millisecondes. **Appelle-le avant d'ecrire un `where` a la main.**

## Quel outil pour quelle question

| La question | L'outil |
|---|---|
| « quel est le taux du dollar ? » | `taux_du_jour("USD")` |
| « le taux applicable au 1er juillet », « au moment de la facture » | `taux_a_la_date("2026-07-01", "USD")` |
| « combien font 12 400 PLN en euros ? » | `taux_convertir(12400, "PLN")` |
| « et 5 000 EUR en livres ? » | `taux_convertir(5000, "GBP", sens="depuis_eur")` |
| « comment le zloty a bouge depuis janvier ? » | `taux_historique("PLN", "2026-01-01")` |
| « quelle est la devise de la Turquie ? », « quel code pour la couronne ? » | `taux_devises("couronne")` |
| compter, agreger, un filtre exotique | `taux_records(select=..., group_by=...)` |
| un fichier | `taux_export_csv` — et seulement sur demande explicite |
| « ca ne repond pas », « d'ou sort ce chiffre ? » | `taux_doctor` |

## Le sens du taux : la seule erreur vraiment couteuse

`taux` est le nombre d'**euros** que vaut **une** unite de la devise. USD a
0,8589 veut dire *1 USD = 0,8589 EUR*, donc 1 EUR vaut environ 1,164 USD.

- devise vers euro : montant **x** taux
- euro vers devise : montant **/** taux

Une inversion ne leve aucune erreur : elle rend un montant plausible, du bon
ordre de grandeur, et faux. **Passe par `taux_convertir` plutot que de multiplier
toi-meme** : il rend le taux utilise, sa date d'effet et le calcul pose, donc la
personne peut verifier.

## Comment tu restitues un taux

Trois choses vont ensemble, et la troisieme est celle qu'on oublie :

1. **le taux**, sans le rearrondir — l'outil le rend a la precision publiee ;
2. **la devise**, en code ISO et en clair ;
3. **la date d'effet du taux**, qui n'est pas la date demandee.

Un taux de fevrier cite dans une reponse de septembre n'est pas une anomalie :
c'est le taux en vigueur. Mais **sans sa date d'effet, il n'est pas
verifiable** — et c'est ce chiffre-la qui part dans un mail a un transporteur.

## Ce que ce jeu de donnees n'est pas

**Ce sont des taux de chancellerie : une reference administrative mensuelle.** Pas
un cours de marche, pas un taux de banque, pas le taux de conversion d'un
reglement.

Donc, sur une question de facturation transporteur : ce connecteur donne la
reference publique, **il ne dit pas quel taux s'applique au contrat.** Le taux
contractuel, sa date de reference et sa regle d'arrondi se lisent dans
`02_TRANSPORTEURS/<NOM>/01_contrat/` et `02_tarif/`. Si la personne demande un
controle de facture en devise, dis les deux : le taux de chancellerie a cette
date, et le fait que le contrat peut en imposer un autre.

Deux autres limites a annoncer plutot qu'a laisser decouvrir :

- **L'historique n'est exploitable qu'a partir de l'ete 2005.** La plage annoncee
  commence en 1990, mais 83 lignes seulement sont anterieures a 1999, sur neuf
  devises obscures. Le dollar n'y entre que le 2005-06-01. Sur une question
  portant sur 2003, l'outil rend peu ou rien — ce n'est pas une panne, et ca ne
  dit rien du taux qui s'appliquait alors.
- **Le jeu ne porte pas les monnaies pre-euro** : ni franc francais, ni mark.

## L'export est sur demande, jamais par defaut

**La reponse par defaut va dans la session, pas dans un fichier.** N'appelle
`taux_export_csv` que si la personne a demande un fichier, un export, un CSV ou
un classeur : cet outil ecrit sur le disque. Un perimetre trop gros pour la
session ne le justifie pas de ta propre initiative — resserre les devises, ou
dis-le et laisse decider.

Deux modes, et confondre les deux produit un faux : `en_vigueur` rend une ligne
par devise (ce qu'on veut pour un controle), `brut` rend les lignes du jeu avec
leurs doublons par pays (ce qu'on veut pour une serie). La commande
`/taux-export` conduit le choix.

## Quand une reponse est vide

Une liste vide n'est **jamais** a rapporter comme « il n'y a pas de taux ». Trois
causes possibles, et l'outil les nomme :

- le code ISO n'existe pas dans le jeu -> verifie avec `taux_devises` ;
- la devise n'avait pas encore de publication a cette date -> l'erreur dit
  laquelle est la premiere ;
- la devise n'a plus cours et `vigueur_seulement=True` l'a ecartee -> relance
  avec `vigueur_seulement=False`.

Et si l'entete de la reponse porte **REPLI LOCAL**, la source n'a pas repondu :
le taux vient d'un instantane pose sur le poste. Il est exploitable, mais dis sa
date de recuperation avant qu'on le cite dans un document.

## Ce que tu ne fais pas

- **Tu n'inventes ni un taux ni une date.** Si l'outil ne rend rien, la reponse
  est « le jeu ne porte pas ce taux », pas une estimation. C'est la regle du
  vault, et une date de taux fausse part dans un mail a un transporteur.
- **Tu ne rearrondis pas un taux** et tu ne presentes pas un montant converti
  sans dire a quel taux et a quelle date.
- **Tu n'ecris rien** : ce connecteur est en lecture seule, et il n'y a pas
  d'operation d'ecriture a exposer sur un jeu de donnees public.
