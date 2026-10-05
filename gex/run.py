"""Point d'entrée du dashboard, exposé comme commande ``gex-dashboard``.

Démarre l'ingestion planifiée puis sert le dashboard Dash sur
http://127.0.0.1:8050. Utilisable de trois façons équivalentes :

    gex-dashboard            (après `pip install .`)
    python -m gex.run
    python run.py            (raccourci à la racine du dépôt)
"""
from __future__ import annotations

from gex import flowtape
from gex.app import create_app, start_scalp_indicator_scheduler
from gex.capturebus import RemoteTape, remote_url
from gex.flowtape import TAPE
from gex.logsetup import setup_logging
from gex.rtquote import PUBLIC_QUOTES, QUOTES
from gex.scheduler import start_scheduler
from gex.tickcapture import CAPTURE


def main(host: str = "127.0.0.1", port: int = 8050) -> None:
    # console + logs/gex.log (rotatif) : la trace survit à la fermeture du terminal
    setup_logging()
    url = remote_url()
    # Mode SÉPARÉ (GEX_CAPTURE_URL défini) : ticks, order flow et bougies vivent
    # dans le process `gex.capture`, que redémarrer le dashboard ne coupe plus.
    # Le dashboard s'y abonne et lit un miroir (RemoteTape, même interface).
    start_scheduler(embedded_capture=url is None)
    # spot temps réel pour l'AFFICHAGE : sans identifiants courtier, sans effet
    QUOTES.start()
    # repli gratuit NQ/ES délayé : ne démarre que si QUOTES ne tourne pas
    PUBLIC_QUOTES.start()
    if url:
        # les vues du dashboard font `from .flowtape import TAPE` à l'appel : on
        # remplace l'attribut du module AVANT de servir, elles lisent le miroir
        flowtape.TAPE = RemoteTape(url)
        flowtape.TAPE.start()
    else:
        # order flow signé sur options : sans identifiants, sans effet
        TAPE.start()
        # capture tick-par-tick continue NQ/ES (24/5) : session dxLink dédiée
        CAPTURE.start()
    # Moteur planifié des indicateurs /scalp (2026-10-04, cf. gex/app.py) —
    # séparé de start_scheduler() ci-dessus à dessein, PAS appelé depuis
    # create_app() : les tests construisent l'app en boucle sans jamais
    # vouloir de vrai travail de fond planifié.
    start_scalp_indicator_scheduler()
    # `threaded=True` sur le serveur de dev Werkzeug a été essayé le
    # 2026-10-03 et retiré dans la minute (observé pire en direct). Diagnostic
    # confirmé le 2026-10-04 par un test isolé (app Flask jouet, hors
    # gex/app.py) : Werkzeug `threaded=True` lève bien le blocage HTTP de
    # base, MAIS spawn un thread PAR CONNEXION, sans aucune limite — sous
    # rafale (plusieurs onglets, callbacks ~1s chacun), ça peut lancer des
    # dizaines de threads qui se contentent le GIL en même temps pendant du
    # travail pandas synchrone (confluence/order_flow), d'où le ressenti
    # "pire". waitress règle ce point précis avec un pool BORNÉ.
    #
    # Panne réelle le 2026-10-04 (même jour, ~10h03) avec `threads=8` :
    # /api/v1/<symbol>/stream (ticker de prix /scalp, SSE) tient son thread
    # OUVERT EN PERMANENCE tant que l'onglet reste ouvert (boucle
    # `while True: ... time.sleep(0.1)`, cf. gex/api.py::_last_trade_stream).
    # Chaque onglet /scalp ouvert consomme donc un thread du pool pour toute
    # sa durée de vie — avec seulement 8, une poignée d'onglets simultanés
    # (plusieurs onglets de test + un accès distant) suffit à épuiser le
    # pool : plus aucun thread pour servir le reste, logs "Task queue depth"
    # qui grimpe jusqu'à "connection limit reached", serveur injoignable
    # (confirmé : `curl` timeout direct, pas juste le navigateur).
    # Relevé à 48 en conséquence : ces threads SSE sont endormis l'essentiel
    # du temps (I/O, pas CPU) — contrairement au risque du `threaded=True`
    # de Werkzeug ci-dessus (threads CPU-bound qui se contentent le GIL),
    # en avoir plusieurs dizaines d'inactifs ne recrée PAS ce problème. Reste
    # un pool BORNÉ (jamais d'explosion illimitée), juste dimensionné pour
    # supporter une vraie poignée d'onglets ouverts en continu.
    #
    # 2e panne, 2026-10-04 (~22h49), causes CUMULÉES diagnostiquées en
    # direct :
    #  1. Un vrai bug : _scalp_indicators_stream (gex/app.py) avalait les
    #     erreurs d'ÉCRITURE (client déconnecté) dans le même try/except que
    #     les erreurs de CALCUL — un thread servant un onglet fermé/une
    #     connexion morte ne se libérait donc JAMAIS. Corrigé (le yield est
    #     sorti du try/except).
    #  2. Aucun des deux flux SSE (_last_trade_stream, gex/api.py ;
    #     _scalp_indicators_stream) n'émettait quoi que ce soit tant que rien
    #     ne changeait — une connexion morte (onglet fermé sans fermeture TCP
    #     propre, wifi coupé côté client) pouvait donc rester invisible des
    #     heures : aucune écriture ne peut échouer si on n'écrit rien. Les
    #     deux envoient maintenant un commentaire SSE ("keepalive") toutes les
    #     ~15s d'inactivité — une connexion morte échoue vite, une connexion
    #     vivante ignore juste la ligne (convention SSE standard).
    #  3. Pas de filet de sécurité côté SERVEUR : même avec (1) et (2)
    #     corrigés, une connexion franchement orpheline (TCP RST jamais reçu,
    #     cas réseau rare) pouvait en théorie survivre indéfiniment.
    #     `channel_timeout` ferme côté waitress tout canal sans AUCUN octet
    #     envoyé/reçu depuis ce délai — réglé à 90s, au-dessus du heartbeat de
    #     15s (large marge), donc aucune connexion active n'est jamais coupée
    #     à tort.
    # Seuil de threads remonté à 128 en complément (garde-fou temporaire,
    # moins nécessaire une fois (1)/(2)/(3) en place, mais pas de raison de
    # revenir en arrière) — migration propre vers un serveur asynchrone
    # (gevent) prévue séparément, pas ce soir : testé en isolation, un patch
    # gevent naïf (monkey.patch_all() par défaut) BLOQUE la vraie connexion
    # wss:// vers dxFeed utilisée par rtquote.py/flowtape.py/tickcapture.py
    # (confirmé en direct) — `patch_all(thread=False)` contourne le blocage
    # mais laisse une friction résiduelle (exceptions LoopExit côté résolveur
    # DNS de gevent) pas encore assez éprouvée pour la production.
    # 3e panne, 2026-10-04 (~23h27), MÊME SOIR : relevé threads=48->128 plus
    # haut INSUFFISANT seul — "connection limit reached" est piloté par
    # `connection_limit` (paramètre SÉPARÉ de `threads`, jamais touché,
    # waitress le fixe à 100 par défaut), pas par le nombre de threads.
    # Avec plusieurs flux SSE par onglet (prix + indicateurs) et le
    # scheduler planifié, 100 connexions simultanées se saturent vite même
    # sans trafic massif. Relevé à 300, large marge au-dessus de threads=128
    # (une connexion en file d'attente ne consomme pas forcément un thread
    # tout de suite, les deux compteurs ne sont pas le même budget).
    #
    # Relevé 128->256 le 2026-10-05 (~10h ET, lundi, vraie heure de marché) :
    # première fois que le serveur encaisse du trafic RÉEL multi-utilisateurs
    # en semaine (toute la nuit précédente était du test solo, week-end,
    # quasi sans trafic). 200 ESTABLISHED + file d'attente waitress à 70+
    # tâches + scheduler qui saute ses propres cycles ("maximum number of
    # running instances reached") — CloseWait restait bas (1), donc ce n'est
    # PAS un retour de la fuite corrigée cette nuit, juste un pool de
    # threads insuffisant pour le volume réel. La plupart de ces connexions
    # sont des flux SSE endormis l'essentiel du temps (I/O, pas CPU, cf.
    # panne du 2026-10-04 pour le même raisonnement) — en ouvrir plus ne
    # recrée pas le risque de contention du `threaded=True` Werkzeug déjà
    # écarté. connection_limit relevé en proportion (300->500). Stopgap
    # immédiat : la vraie solution reste la migration gevent (cf. passation),
    # pas encore assez éprouvée pour la production ce soir-là.
    from waitress import serve
    serve(create_app().server, host=host, port=port, threads=256,
         channel_timeout=90, connection_limit=500)


if __name__ == "__main__":
    main()
