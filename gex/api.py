"""API JSON minimale, en lecture seule, greffée sur le serveur Flask que Dash
utilise déjà (`app.server`) — pour qu'un outil externe (indicateur de
charting, script) tournant SUR LA MÊME MACHINE puisse lire l'état courant
sans passer par l'interface.

⚠️ Portée de la licence — à ne pas confondre avec `gex.export` (qui, lui,
prépare un export destiné à être PARTAGÉ avec d'autres personnes, et filtre
donc sur `source == "cboe"` uniquement). Ici, c'est différent : ce flux sert
TOUTES les données disponibles, y compris celles issues d'un compte courtier
(dxFeed) — parce que la licence « usage personnel, non redistribuable »
autorise le titulaire du compte à utiliser SES PROPRES données dans SES
PROPRES outils (un indicateur de charting local, par exemple). Ce qu'elle
interdit, c'est de les REDISTRIBUER À DES TIERS — quelqu'un d'autre, sans son
propre compte, qui consommerait ce flux à distance. D'où la limite réelle à
respecter : ce serveur ne doit pas être exposé au-delà de la machine locale
(pas de port forwarding, pas d'écoute sur 0.0.0.0 ouverte à l'extérieur).
"""
from __future__ import annotations

from datetime import datetime

from flask import Flask, jsonify, request

from . import metrics
from .metrics import ET, EXPIRY_BUCKETS
from .scheduler import STATE


def _summary_dict(symbol: str, s) -> dict:
    return {
        "symbol": symbol,
        "source": s.source,
        "timestamp": s.timestamp.isoformat(),
        "spot": s.spot,
        "net_gex": s.net_gex,
        "net_gex_0dte": s.net_gex_0dte,
        "zero_gamma": s.zero_gamma,
        "net_dex": s.net_dex,
        "pc_oi": s.pc_oi,
        "pc_volume": s.pc_volume,
        "basis": s.basis,
    }


def _current_summary(symbol: str):
    """(summary, enriched) pour ce symbole, quelle que soit la source — cf.
    docstring du module sur la portée réelle de la licence."""
    st = STATE.get(symbol)
    with STATE.lock:
        s, df = st.summary, st.enriched
    if s is None:
        return None, None
    return s, df


def register_api(app) -> None:
    """`app` : l'instance Dash (on grimpe à `.server`) ou directement une
    instance Flask — pratique pour les tests, qui n'ont pas besoin de monter
    tout le dashboard."""
    server: Flask = app.server if hasattr(app, "server") else app

    @server.after_request
    def _cors(resp):
        # CORS large parce que le risque visé est différent de celui d'un
        # site web classique : ce serveur n'écoute qu'en local (cf. docstring
        # du module) — le vrai garde-fou est là, pas dans l'en-tête CORS.
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    @server.route("/api/v1/symbols")
    def _symbols():
        out = []
        with STATE.lock:
            items = list(STATE.per_symbol.items())
        for symbol, st in items:
            if st.summary is not None:
                out.append(symbol)
        return jsonify(sorted(out))

    @server.route("/api/v1/<symbol>/summary")
    def _summary(symbol):
        symbol = symbol.upper()
        s, _ = _current_summary(symbol)
        if s is None:
            return jsonify({"error": "indisponible (pas encore de premier pull)"}), 404
        return jsonify(_summary_dict(symbol, s))

    @server.route("/api/v1/<symbol>/levels")
    def _levels(symbol):
        symbol = symbol.upper()
        s, df = _current_summary(symbol)
        if s is None or df is None:
            return jsonify({"error": "indisponible (pas encore de premier pull)"}), 404
        levels = metrics.top_gex_levels(df, ref_spot=s.spot)
        keys = metrics.key_levels(df, s.spot, ref_spot=s.spot)
        return jsonify({
            "symbol": symbol,
            "spot": s.spot,
            "zero_gamma": s.zero_gamma,
            "hvl": metrics.zero_gamma(df, s.spot, weight_col="volume"),
            "key_levels": keys,
            "gex_walls": [
                {"strike": float(r.strike), "gex": float(r.gex), "expiry": str(r.expiry)}
                for r in levels.itertuples()
            ],
        })

    @server.route("/api/v1/<symbol>/regime")
    def _regime(symbol):
        symbol = symbol.upper()
        s, _ = _current_summary(symbol)
        if s is None:
            return jsonify({"error": "indisponible (pas encore de premier pull)"}), 404
        r = metrics.regime_read(s.net_gex, s.net_dex)
        return jsonify({
            "symbol": symbol,
            "gex_frein": r["gex_frein"],
            "dex_sign": r["dex_sign"],
            "severity": r["severity"],
            "disclaimer": "Lecture mécanique de la couverture dealers, pas un signal d'entrée.",
        })

    @server.route("/api/v1/<symbol>/strikes")
    def _strikes(symbol):
        symbol = symbol.upper()
        bucket = request.args.get("bucket", "Tout")
        s, df = _current_summary(symbol)
        if s is None or df is None:
            return jsonify({"error": "indisponible (pas encore de premier pull)"}), 404
        if bucket in EXPIRY_BUCKETS:
            today = datetime.now(ET).date()
            df = df[metrics.bucket_mask(df, bucket, today)]
        cols = ["strike", "type", "expiry", "open_interest", "gex", "dex"]
        rows = df[cols].copy()
        rows["expiry"] = rows["expiry"].astype(str)
        return jsonify({
            "symbol": symbol, "spot": s.spot, "bucket": bucket,
            "rows": rows.to_dict(orient="records"),
        })

    @server.route("/api/v1/digest")
    def _digest():
        """Verdict d'état du gamma prêt à diffuser (cf. gex/digest.py).

        C'est ce qu'un bot Discord consomme : le texte, la couleur, et la
        `signature` de régime (pour ne re-poster que sur un vrai changement).
        Renvoie une analyse dérivée, jamais la chaîne brute.
        """
        from . import digest as digest_mod
        d = digest_mod.current_digest()
        return jsonify({
            "header": d.header,
            "lines": d.lines,
            "vix_line": d.vix_line,
            "verdict": d.verdict,
            "color": d.color,
            "discord_color": d.discord_color,
            "text": d.to_text(),
            "signature": list(d.signature),
        })
