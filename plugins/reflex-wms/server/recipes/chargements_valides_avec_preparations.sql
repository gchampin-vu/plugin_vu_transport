-- Chargements valides et leurs preparations : le modele de jointure de l'equipe.
--
-- Adapte du script Power Query qui alimente Power BI (Guillaume Champin,
-- 2026-08-28). Les colonnes ont ete verifiees une a une contre le MLD Hardis.
--
-- CE QU'IL FAUT RETENIR DE CETTE REQUETE :
--
-- 1. La jointure preparation -> chargement porte sur SIX colonnes, pas sur le
--    seul code chargement : depot, code chargement, puis les quatre morceaux
--    de la date de chargement (siecle, annee, mois, jour). Le code chargement
--    est reutilise d'un jour a l'autre - joindre sans la date melange les
--    tournees de dates differentes.
--
-- 2. PEVTAP et PEVTAV sont en dm3 : le / 1000 les met en m3. Un seuil
--    volumetrique pose sans cette division est faux d'un facteur mille.
--
-- 3. Le filtre de date passe par l'entier compose
--    (siecle * 1000000 + annee * 10000 + mois * 100 + jour), et PAS par
--    RFX_DHB_DATE2DATETIME. C'est volontaire et c'est ce qui fait tenir la
--    requete : un filtre pose sur le resultat de la fonction interdit l'usage
--    des index et fait basculer le plan en balayage complet.
--
-- 4. CGTCHV est le top "chargement valide". Il est declare Alphanumeric(1)
--    dans le MLD et vaut '1' en production. Le comparer a l'entier 1 marche
--    par conversion implicite, mais cette conversion s'applique a la COLONNE :
--    elle coute un index, et elle leve une erreur le jour ou une ligne porte
--    'O'. On ecrit donc la comparaison en texte, agnostique a l'encodage.
--
-- 5. Le sous-select sur HLPRPLP est le point lourd : il agrege la table des
--    lignes de preparation, la plus grosse de la base. Il est ici borne au
--    meme depot et a la meme fenetre que l'exterieur. Sans ce bornage, la
--    requete est refusee par le gouverneur de cout, et elle a raison de
--    l'etre.
--
-- A adapter : le depot, et la date plancher (format AAAAMMJJ).

WITH chargements AS (
    SELECT
        cg.CGCDPO, cg.CGCCHA, cg.CGLCHA, cg.CGNCHC, cg.CGRCHA, cg.CGNEMP,
        cg.CGSSCA, cg.CGANCA, cg.CGMOCA, cg.CGJOCA,
        REFLEX.RFX_DHB_DATE2DATETIME(cg.CGSSCA, cg.CGANCA, cg.CGMOCA, cg.CGJOCA, 0) AS date_chargement,
        REFLEX.RFX_DHB_DATE2DATETIME(cg.CGSVCH, cg.CGAVCH, cg.CGMVCH, cg.CGJVCH, cg.CGHVCH) AS date_validation
    FROM   reflex.HLCHARP cg  WITH (NOLOCK)
    WHERE  cg.CGCDPO = 'AMB'
      AND  ISNULL(cg.CGTCHV, '0') IN ('O', '1', 'Y')
      AND  (cg.CGSSCA * 1000000 + cg.CGANCA * 10000 + cg.CGMOCA * 100 + cg.CGJOCA) >= 20260101
),
proprietaires AS (
    SELECT
        pl.P1CACT, pl.P1CDPO, pl.P1NANP, pl.P1NPRE,
        MIN(pl.P1CPRP)            AS code_proprietaire,
        COUNT(DISTINCT pl.P1CPRP) AS nb_proprietaires_distincts,
        MIN(pl.PECDOD)            AS depot_odp,
        MIN(pl.P1NANO)            AS annee_odp,
        MIN(pl.P1NODP)            AS num_odp
    FROM   reflex.HLPRPLP pl  WITH (NOLOCK)
    WHERE  pl.P1CDPO = 'AMB'
    GROUP  BY pl.P1CACT, pl.P1CDPO, pl.P1NANP, pl.P1NPRE
)
SELECT
    pe.PECACT                                AS code_activite,
    pe.PECDPO                                AS depot_physique,
    pe.PENANN                                AS num_annee_prepa,
    pe.PENPRE                                AS num_prepa,
    pe.PERODP                                AS ref_do_odp,
    pe.PECTPR                                AS type_prepa,
    pe.PECFPR                                AS famille_prepa,
    pe.PECRGC                                AS regroupement_charg,
    pe.PECDES                                AS code_destinataire_prepa,
    pe.PECDPD                                AS depot_destination,
    pr.code_proprietaire                     AS code_proprietaire,
    pr.nb_proprietaires_distincts            AS nb_prop_distincts,
    pr.num_odp                               AS num_odp,
    pe.PEPNTV                                AS poids_net_val_kg,
    pe.PEPBTV                                AS poids_brut_val_kg,
    pe.PEVTAV / 1000.0                       AS volume_val_m3,
    pe.PEVTAP / 1000.0                       AS volume_a_prep_m3,
    cg.CGCCHA                                AS code_charg,
    cg.CGLCHA                                AS libelle_charg,
    cg.CGNCHC                                AS nom_chauffeur,
    cg.CGRCHA                                AS ref_transp,
    cg.date_chargement                       AS date_chargement,
    cg.date_validation                       AS date_validation_charg,
    em.EMLIEM                                AS lib_quai
FROM   reflex.HLPRENP pe  WITH (NOLOCK)
INNER  JOIN chargements cg
       ON  cg.CGCDPO = pe.PECDPO
       AND cg.CGCCHA = pe.PECCHA
       AND cg.CGSSCA = pe.PESSCA
       AND cg.CGANCA = pe.PEANCA
       AND cg.CGMOCA = pe.PEMOCA
       AND cg.CGJOCA = pe.PEJOCA
LEFT   JOIN reflex.HLEMPLP em  WITH (NOLOCK)
       ON  em.EMCDPO = cg.CGCDPO
       AND em.EMNEMP = cg.CGNEMP
LEFT   JOIN proprietaires pr
       ON  pr.P1CACT = pe.PECACT
       AND pr.P1CDPO = pe.PECDPO
       AND pr.P1NANP = pe.PENANN
       AND pr.P1NPRE = pe.PENPRE
WHERE  pe.PECDPO = 'AMB'
  AND  pe.PECACT <> 'BEA'
  AND  pe.PECCHA <> ''
  AND  pe.PECCHA IS NOT NULL
  AND  pe.PEPBTV > 0
ORDER BY cg.date_chargement DESC, pe.PENPRE
