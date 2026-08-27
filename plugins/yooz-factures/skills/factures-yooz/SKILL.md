---
name: factures-yooz
description: Interroger la base de factures fournisseurs Yooz - facture recue, montant facture, facture bloquee, cause de blocage, echeance, avoir, fiche fournisseur, unite d'organisation, export comptable. Active des qu'une question porte sur Yooz, sur une facture fournisseur ou son montant, sur ce qui est bloque ou en attente de validation, sur ce qu'un transporteur a facture, ou des qu'il faut le cote facture d'un controle de facturation. Declenche aussi sur "yooz", "facture fournisseur", "facture bloquee", "montant facture", "avoir", "echeance de paiement", "ce que VIR nous a facture".
---

# Interroger Yooz

Yooz porte les **factures fournisseurs** des societes du groupe : ce qui a ete
recu, saisi, bloque, valide, comptabilise. C'est la source du **montant
facture** - le troisieme des trois indicateurs du controle de facturation, en
face du FL calcule en interne et du recalcul sur grille.

Les outils sont exposes par le serveur MCP `yooz` du meme plugin. Ils sont
**tous en lecture**. Valider, bloquer, comptabiliser ou importer une facture ne
se font pas ici : ces gestes engagent la comptabilite, ils restent humains dans
l'interface Yooz.

## Deux chemins, et le bon reflexe est de choisir

L'API Yooz **n'a aucune recherche de document**. Il faut donc savoir lequel des
deux chemins repond a la question posee, sinon on paie un rapatriement complet
pour une question a une ligne.

**Une question sur un referentiel ou sur la structure -> chemin direct, un appel.**

- `yooz_referential` : la fiche d'un fournisseur par son code, le comptage d'un
  referentiel, ou une page de 100 lignes. Familles : `fournisseur`, `compte`,
  `cause_blocage`, `devise`, `mode_paiement`, `journal`, `categorie_facture`...
  Premier appel sans `referential` pour obtenir les codes disponibles.
- `yooz_org_units` : relier un `orgUnitCode` de facture (7000, 7005...) a
  l'entite qui la porte.
- `yooz_reports` : les data reports disponibles, avec leur identifiant.
- `yooz_report_peek` : une page courte d'un rapport, pour verifier la fraicheur
  ou decouvrir sa structure. Ne touche pas au cache.

**Une question sur des factures -> chemin cache, en deux temps.**

1. `yooz_status` pour savoir de quand date le cache. Si la synchro est vieille
   ou absente, `yooz_sync` d'abord.
2. `yooz_invoices` (filtres prets a l'emploi), `yooz_summary` (agregation) ou
   `yooz_sql` (SQL libre, lecture seule sur la table `factures`).
   `yooz_columns` donne les noms exacts des colonnes - ils ne sont pas
   devinables.

`yooz_export_csv` des que le resultat depasse quelques dizaines de lignes ou
qu'il doit etre joint a un mail.

## Les cinq pieges

**Le cache n'est pas la production.** Un chiffre sorti d'ici a l'age de la
derniere synchro. `yooz_status` la donne par societe. Une question du type
"est-ce que la facture est arrivee aujourd'hui" demande un `yooz_sync` avant de
repondre, ou un `yooz_report_peek` qui lit en direct.

**`mode="full"` est le defaut, et c'est voulu.** `lastExecutionDatetime` ne
filtre que si le data report porte lui-meme un filtre sur cette date : un
`mode="delta"` peut donc rendre le meme volume qu'un `full` sans que rien ne le
signale. Le cache etant en `INSERT OR REPLACE`, un `full` est toujours juste.

**Les lignes de facture ne sont pas dans le cache par defaut.** La colonne
`YZ_INVOICE_LINE` est ecartee. Pour du ligne a ligne, il faut un data report
dedie rapatrie dans son propre `dataset` - `yooz_reports` dira s'il en existe un.

**Le telechargement d'un export comptable peut avoir un effet.** L'API marque le
fichier comme telecharge par defaut, ce qui peut le faire sauter a
l'integration comptable. `yooz_export_download` envoie
`ignoreMarkAsDownloaded=true` d'office : ne passe `mark_as_downloaded=True` que
si tu veux deliberement consommer le fichier.

**Le nom Yooz d'un tiers n'est pas le nom maison du transporteur.** Croise avec
l'annuaire du contexte d'equipe (`01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`) avant
de conclure sur le portefeuille : « Rhenus » designe quatre entites,
« Posten Bring » et « Bring » sont deux prestataires distincts.

## Citer un chiffre Yooz

Avec son perimetre, sa periode et sa date de synchro, sinon il est inutilisable
en negociation ou en revue :

> 312 factures VIR, DistriService + Vente-Unique, dates de facture du
> 2026-07-01 au 2026-08-31, 418 260,55 EUR TTC - cache Yooz synchronise le
> 2026-08-27.

Et la regle qui ne bouge pas : **on n'invente jamais un chiffre**. S'il n'est pas
dans le cache ou rendu par un appel, il n'existe pas.

## Quand ca coince

`yooz_status` en premier. Il dit d'ou vient la configuration, si le jeton
s'obtient, si l'API repond, et ou en est le cache. Les deux erreurs qui
reviennent :

- `403 EMPTY_OR_BAD_APPLICATION_ID` : l'`applicationId` est faux. C'est un
  en-tete HTTP, un par societe, et **ce n'est pas le `client_id`**.
- `invalid_grant` sur le jeton : le refresh token est revoque ou expire. Il faut
  en regenerer un dans Yooz et le remettre dans la configuration du plugin.
