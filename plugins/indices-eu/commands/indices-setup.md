---
description: Mettre en service le connecteur des indices europeens, ou comprendre pourquoi une des trois sources ne repond pas.
---

Tu mets en service le connecteur `indices-eu`, ou tu diagnostiques une source
qui ne repond pas.

## Ce qu'il faut savoir avant de commencer

**Il n'y a aucun secret a saisir.** Ni cle d'API, ni jeton, ni compte : le
Weekly Oil Bulletin de la Commission et les API de diffusion d'Eurostat sont des
donnees ouvertes. Si quelqu'un te demande une cle pour ce connecteur, c'est une
erreur - ne la reclame pas, et ne l'ecris nulle part.

Le connecteur **demarre sans aucun reglage**, et depuis le 2026-09-02 il n'y a
plus d'exception : tout ce qui suit est du confort. Le certificat casse du
bulletin petrolier, qui etait le seul point bloquant, a ete corrige par la
Commission. **N'arme rien d'avance** - le mode strict est le bon mode.

## Comment tu conduis

**1. Lance `indices_doctor()`.** Il affiche l'interpreteur utilise, la racine
locale, le dossier d'export, la configuration retenue et l'etat des trois
sources. Lis son verdict avant de proposer quoi que ce soit.

**2. Si toutes les sources repondent, tu as fini.** Le verdict de `doctor` en donne le compte - il y en a six, plus une par mesure Eurostat. Dis ou atterrissent les
exports, rappelle que `/indices-export` produit un fichier, et arrete-toi la.

**3. Si le gazole echoue sur le certificat**, c'est la rechute d'un incident
connu. Le 2026-09-01, la Commission servait sur `energy.ec.europa.eu` un
certificat emis pour `europa.eu` et `*.europa.eu` seulement. Un joker ne couvre
**qu'un** label, donc il valait pour `ec.europa.eu` et **pas** pour
`energy.ec.europa.eu`. Ce n'etait ni le poste, ni le proxy de l'entreprise :
l'emetteur etait bien Amazon, une erreur de rotation cote Commission. **Corrige
le 2026-09-02** - le telechargement strict passe depuis.

Donc si tu vois cette erreur aujourd'hui, la rotation a recasse. Avant de
proposer un contournement, **verifie que ce n'est pas transitoire** : relance
`indices_refresh("gazole")` une fois. Si l'echec persiste, presente les **deux**
issues, dans cet ordre, et laisse choisir :

| Issue | Ce que ca implique |
|---|---|
| **Telecharger le classeur a la main** dans un navigateur, puis le pointer par le champ « Classeur pose a la main » de `/plugin` (ou `INDICES_GASOIL_FILE`) | Rien n'est affaibli. En contrepartie le fichier ne se met plus a jour tout seul, et chaque reponse rappelle qu'elle lit un fichier local |
| **Armer `chaine-seule`** dans le champ « Verification du certificat » de `/plugin` (ou `INDICES_GASOIL_TLS=chaine-seule`) | La chaine de certification reste verifiee, **et** le certificat doit appartenir a `europa.eu` - sans quoi le telechargement est refuse. Seul le nom d'hote n'est plus verifie. Chaque reponse batie sur ce classeur porte l'avertissement |

**Ne desactive jamais la verification TLS en bloc**, et ne propose pas de le
faire : le prix du gazole entre dans un calcul de surcharge, donc dans un
echange contractuel.

Et **c'est un reglage temporaire** : dis-le en le posant. Des que la source
repond en strict, `chaine-seule` se retire - sinon le poste garde une
verification affaiblie pour un incident qui n'existe plus, et chaque reponse
continue de porter un avertissement devenu faux.

**4. Si le gazole echoue sur un HTTP 404**, la Commission a change
l'identifiant du document. La bonne correction n'est pas sur ce poste : c'est
`INDICES_GASOIL_URL` dans le fichier d'equipe
`08_ENGINE/04_mcp/00_config/indices.shared.env`, qui vaut alors pour tout le
monde. Retrouve l'URL sur la page « Weekly Oil Bulletin » du site de la DG ENER,
propose la ligne a ecrire, et **rappelle qu'ecrire dans le collectif se trace**
(`updated` / `updated_by`).

**5. Si Eurostat echoue**, c'est le reseau ou un proxy : les deux flux passent
par `ec.europa.eu`, dont le certificat est sain. Verifie qu'aucune variable
`HTTP_PROXY` / `HTTPS_PROXY` ne manque.

**6. Si une source NATIONALE echoue**, il y en a trois, et aucune ne demande de
reglage :

| Source | Ce qu'elle sert | Si elle echoue |
|---|---|---|
| `gov.uk` API de contenu | le prix du carburant britannique, et le salaire minimum | La page a demenage ou change de structure. Le serveur le dit explicitement. La correction est `INDICES_DESNZ_API` ou `INDICES_NMW_API` dans le fichier d'equipe |
| `data.ssb.no` | le prix du carburant norvegien | PxWebApi v2 a change ses parametres de selection. **Le symptome a surveiller n'est pas une erreur** : c'est une serie de 13 mois au lieu de 480, rendue avec un HTTP 200. Le serveur le journalise |

**La SUISSE n'a rien a regler et ne repondra jamais sur le prix du carburant** :
il n'existe pas d'API publique suisse pour ca (verifie le 2026-09-02). Si
quelqu'un demande un prix du gazole suisse, la reponse est
`indices_inflation(pays='CH', poste='carburants')`, qui rend un **indice**. Ne
laisse pas croire que c'est un prix, et ne va pas chercher un site prive pour
combler : un prix de carburant qui entre dans une surcharge doit venir d'une
source opposable.

## Ce que tu ne fais pas

- **Tu ne demandes aucun secret**, et tu n'en ecris aucun : il n'y en a pas.
- **Tu n'ecris pas dans le fichier d'equipe sans le dire.** Une valeur qui casse
  le connecteur casse les vingt-six postes en meme temps. Avant d'ecrire,
  verifie qu'aucun fichier suffixe du nom d'une machine (`-VU-XXXXX`) ne traine
  dans `00_config/` : SharePoint ne fusionne pas les ecritures concurrentes, il
  duplique.
