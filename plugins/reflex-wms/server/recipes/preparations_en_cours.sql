-- Preparations en cours : ni soldees, ni sorties de stock.
--
-- ATTENTION - ECART AVEC LA COMPETENCE D'EQUIPE `reflex-mld`, releve le
-- 2026-08-28 contre le MLD Hardis. La competence donne comme filtre canonique
-- "PESLEF > 0 AND PESSOL = 0 AND PESSOR = 0" en lisant PESLEF comme
-- "lancement effectif". Le MLD dit autre chose :
--
--     PESLEF = Date livraison effectuee - siecle       (LEF = Livraison
--     PEULEF = Code utilisateur livraison effectuee     EFfectuee, pas
--     PETLEF = Top livraison effectuee                  Lancement EFfectif)
--
-- Le filtre d'origine se lit donc "livraison effectuee ET ni soldee ni sortie
-- de stock", ce qui n'est pas une preparation en cours. Cette recette est
-- ecrite sur les tops que le MLD nomme sans ambiguite. A arbitrer avec Jimmy
-- Mieuzet, porteur de la competence, avant d'en faire un indicateur.
--
-- Les tops sont testes de facon agnostique a l'encodage : la doc Hardis dit
-- 'O'/'N', la production Vente-unique stocke '1'/'0'.
--
-- A adapter : le depot. AMB = Amblainville, AUV = Montbeugny/Moulins.

SELECT
    pe.PENANN                                                                            AS millesime,
    pe.PENPRE                                                                            AS num_preparation,
    pe.PECDPO                                                                            AS depot_physique,
    pe.PECACT                                                                            AS code_activite,
    pe.PECTPR                                                                            AS type_preparation,
    pe.PECFPR                                                                            AS famille_preparation,
    pe.PECETL                                                                            AS code_etat_livraison,
    et.T0LEPP                                                                            AS libelle_etat,
    pe.PERODP                                                                            AS reference_donneur_ordre,
    pe.PECCHA                                                                            AS code_chargement,
    pe.PEVATP / 1000.0                                                                   AS volume_attribue_m3,
    pe.PEPATP                                                                            AS poids_attribue_kg,
    pe.PETSOL                                                                            AS top_soldee,
    pe.PETSOP                                                                            AS top_sortie_stock,
    REFLEX.RFX_DHB_DATE2DATETIME(pe.PESCRE, pe.PEACRE, pe.PEMCRE, pe.PEJCRE, pe.PEHCRE)  AS date_creation
FROM   reflex.HLPRENP pe  WITH (NOLOCK)
LEFT   JOIN reflex.HLETPPP et  WITH (NOLOCK)
       ON  et.T0CEPP = pe.PECETL
WHERE  pe.PECDPO = 'AMB'
  AND  ISNULL(pe.PETSOL, '0') NOT IN ('O', '1', 'Y')   -- non soldee
  AND  ISNULL(pe.PETSOP, '0') NOT IN ('O', '1', 'Y')   -- sortie de stock non effectuee
  AND  pe.PESSOL = 0                                   -- coherence : pas de date de solde
  AND  pe.PESSOR = 0                                   -- coherence : pas de date de sortie
  AND  pe.PEACRE >= 26                                 -- borne la fenetre, garde l'index
ORDER BY pe.PEACRE DESC, pe.PEMCRE DESC, pe.PEJCRE DESC, pe.PEHCRE DESC
