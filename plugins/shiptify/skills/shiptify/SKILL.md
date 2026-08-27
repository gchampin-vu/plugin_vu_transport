---
name: shiptify
description: Interroger la base Shiptify - envois, demandes de transport, acheminement middle mile vers les agences, commandes, lieux, transporteurs, factures et lignes de facture, evenements. Active des qu'une question porte sur Shiptify, sur un envoi ou une demande de transport, sur un acheminement vers une agence, sur un numero de tracking Shiptify, ou des qu'il faut extraire des envois en CSV. Declenche aussi sur "shiptify", "middle mile", "demande de transport", "expedition vers l'agence", "export des envois".
---

# Interroger Shiptify

Shiptify porte les **demandes de transport middle mile vers les agences**. C'est
la source pour une question d'acheminement, et un complement du controle de
facturation cote lignes de facture.

Les outils sont exposes par le serveur MCP `shiptify` du meme plugin. Ils sont
**tous en lecture**. Creer un transport, annuler un envoi ou confirmer un
enlevement ne se font pas ici : ces operations engagent un transporteur, elles
restent un geste humain dans l'interface Shiptify.

## L'export est sur demande, jamais par defaut

**La reponse par defaut va dans la session, pas dans un fichier.** N'appelle
`shiptify_export_csv` que si l'utilisateur a demande un fichier, un export, un
CSV ou un classeur : cet outil ecrit sur le disque, ce n'est pas une etape de
routine. Sans cette demande, reponds avec les outils de liste, une projection
`fields` serree, et cite le chiffre depuis ce que la liste a rendu.

Un perimetre trop gros pour la session ne justifie pas un export de ta propre
initiative : resserre les dates, filtre sur une agence ou un transporteur, ou
dis a l'utilisateur que le perimetre demande un export et laisse-le decider.

## Si la cle manque

Un outil qui repond `SHIPTIFY_API_KEY manquant` veut dire que le connecteur
n'est pas encore configure sur ce poste. Appelle `shiptify_setup_status` : il
donne la marche a suivre. La commande `/shiptify-setup` fait le tour complet.

**Ne demande jamais a l'utilisateur de coller sa cle dans la conversation**, et
n'appelle aucun outil en lui passant une cle en parametre - aucun ne l'accepte.
La saisie se fait dans l'interface de Claude Code (`/plugin` > shiptify >
configuration), ou la cle reste hors du contexte du modele.

## L'ordre qui marche

1. **`shiptify_list_paths`** ou **`shiptify_dictionary`** d'abord, pour les noms
   exacts des filtres et des valeurs permises. Sauter cette etape, c'est
   deviner un nom de parametre et se prendre un `HTTP 400`.
2. **`shiptify_list_carriers`** / **`shiptify_list_locations`** pour les
   identifiants a mettre dans les filtres.
3. **`shiptify_list_shipments`** avec un perimetre de dates serre, pour
   regarder.
4. **`shiptify_export_csv`** uniquement si l'utilisateur a demande un fichier
   ou un export - voir la section ci-dessus. Jamais pour te rassurer sur un
   chiffre.

Si quelque chose coince, **`shiptify_doctor`** avant tout : il dit d'ou vient la
cle, si l'API repond, et ce que le serveur a lu comme configuration.

## Les quatre pieges

**Le nombre de lignes rendu n'est pas un volume.** L'API Shiptify ne renvoie
aucun total : ni `total`, ni `X-Total-Count`. Les outils de liste s'arretent a
`max_rows` et le disent en majuscules quand c'est tronque. **Un chiffre ne se
cite que depuis un rendu non tronque** : si la liste annonce une troncature, le
chiffre n'est pas citable en l'etat - resserre le perimetre jusqu'a ce qu'il
tienne, ou propose un export a l'utilisateur et attends sa reponse.

**Un envoi porte 114 colonnes.** Utilise `fields` pour projeter ce qui est
utile, en notation pointee :

```
fields="id,status,carrier.name,address_dest.city,address_dest.zipcode,price"
```

`fields="*"` rend tout, et sature la conversation pour rien.

**Un nom Shiptify n'est pas le nom maison du transporteur.** `Prevote I Meru`
est un transporteur actif cote Shiptify et un prospect messagerie non retenu
cote equipe. Croise avec l'annuaire du contexte d'equipe
(`01_CONTEXTE/PORTEFEUILLE_ET_ZONES.md`) avant de conclure sur le portefeuille.
Meme prudence sur les entites multiples : « Rhenus » designe quatre entites,
« Posten Bring » et « Bring » sont deux prestataires distincts.

**Les dates sont au format `YYYY-MM-DD`**, les mois comptables au format
`YYYY-MM`. `/events` exige un type d'evenement : il n'y a pas de journal global.

## Le resultat d'abord, la methode ensuite

**L'ordre de restitution n'est pas libre.** Une reponse Shiptify se rend dans cet ordre, toujours :

1. **Le resultat.** Le chiffre, le tableau, le graphique. En premier, sans preambule. Pas de
   « je vais regarder », pas d'annonce de ce que tu t'apprêtes a faire, pas de recit des etapes
   que tu as suivies pour y arriver.
2. **La methode, apres.** Une fois le resultat pose : les filtres exactes que tu as appliques,
   le perimetre de dates, le champ sur lequel tu as compte, le nombre de lignes obtenues.
3. **Les risques que tu as identifies.** Ce qui peut rendre le chiffre faux : un champ vide, un
   referentiel incomplet, un libelle saisi en deux langues, une troncature, un statut annule
   inclus ou exclu, un mois en cours. Nomme-les, ne les sous-entends pas.

Ce qui est interdit : commenter ta demarche **avant** d'avoir produit le resultat. Si une piste
s'est revelee fausse en route, ca se dit dans la partie methode, pas en ouverture.

## Citer un chiffre Shiptify

La regle de l'equipe s'applique sans changement : **un chiffre se cite avec son
perimetre et ses hypotheses.** Pas « 104 envois vers l'Espagne », mais « 104
envois AMB vers ES, crees du 26 au 27/08/2026, filtre `created_date_from`,
rendu non tronque ». Chaque outil rappelle en tete la requete exacte qu'il a
jouee : recopie-la.

Et la regle qui ne bouge pas : **on n'invente jamais un chiffre.** Si l'API rend
une liste vide, la reponse est « aucune ligne sur ce perimetre », pas une
estimation.

## La facturation, a verifier avant de s'en servir

`shiptify_list_invoices` et `shiptify_list_invoice_lines` fonctionnent
techniquement, mais la surface facturation s'est revelee **pauvre** a la recette
du 2026-08-27 : une seule facture rendue, datee de 2024, et aucune ligne sur
juin-aout 2026. Soit la facturation Shiptify n'est pas alimentee pour le compte,
soit la cle n'a pas la portee.

**Verifie avec Shiptify avant de fonder un controle de facture dessus**, et dis
au lecteur ce que tu as reellement obtenu plutot que de conclure a l'absence de
facturation.
