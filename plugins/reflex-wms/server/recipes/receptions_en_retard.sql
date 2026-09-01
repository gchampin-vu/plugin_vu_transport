-- Receptions previsionnelles en retard et non cloturees.
--
-- QUATRE conditions simultanees. En retirer une seule fait entrer des
-- conteneurs deja traites, et le chiffre gonfle sans qu'on comprenne pourquoi :
--   1. date d'arrivee prevue anterieure a aujourd'hui moins le seuil
--   2. top de reception previsionnelle soldee non positionne  (RPTRPS)
--   3. GEI non generes cote reception reelle (RETGEI) : la marchandise n'est
--      PAS en stock
--   4. reception reelle non validee (RETRVA)
--
-- Le numero de conteneur n'a pas d'emplacement unique et connu d'avance. Avant
-- de figer un filtre dessus, chercher ou il est reellement alimente :
-- HLRCPEP.RPRRPR, HLRECPP.RERREC, HLAEXEP.VERLIX, et les champs de numero de
-- bordereau fournisseur. Faire un LIKE sur chacun, une fois, puis figer.
--
-- A adapter : le depot, le seuil de jours.

SELECT
    rp.RPCDPO                                                                        AS depot,
    rp.RPNANN                                                                        AS millesime,
    rp.RPNRPR                                                                        AS num_rec_prev,
    rp.RPRRPR                                                                        AS reference_conteneur,
    rp.RPCFOU                                                                        AS code_fournisseur,
    rp.RPNBFO                                                                        AS num_bl_fournisseur,
    rp.RPCTRP                                                                        AS code_transporteur,
    REFLEX.RFX_DHB_DATE2DATETIME(rp.RPSRPR, rp.RPARPR, rp.RPMRPR, rp.RPJRPR, 0)      AS date_arrivee_prevue,
    DATEDIFF(day,
             REFLEX.RFX_DHB_DATE2DATETIME(rp.RPSRPR, rp.RPARPR, rp.RPMRPR, rp.RPJRPR, 0),
             GETDATE())                                                              AS jours_de_retard
FROM   reflex.HLRCPEP rp  WITH (NOLOCK)
LEFT   JOIN reflex.HLRECPP re  WITH (NOLOCK)
       ON  re.RECACT = rp.RPCACT
       AND re.RECDPO = rp.RPCDPO
       AND re.RENANN = rp.RPNANN
       AND re.RENRPR = rp.RPNRPR
WHERE  rp.RPCDPO = 'AMB'
  AND  ISNULL(rp.RPTRPS, '0') NOT IN ('O', '1', 'Y')
  AND  ISNULL(re.RETRVA, '0') NOT IN ('O', '1', 'Y')
  AND  ISNULL(re.RETGEI, '0') NOT IN ('O', '1', 'Y')
  AND  REFLEX.RFX_DHB_DATE2DATETIME(rp.RPSRPR, rp.RPARPR, rp.RPMRPR, rp.RPJRPR, 0)
           <= DATEADD(day, -2, CAST(GETDATE() AS datetime))
  AND  rp.RPSRPR > 0
ORDER BY jours_de_retard DESC, rp.RPRRPR
