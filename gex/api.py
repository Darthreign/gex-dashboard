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
from datetime import time as dt_time

import pandas as pd
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


# Seuil (points) au-delà duquel un retournement compte comme un « vrai »
# retracement, par instrument. Sert au comptage n_reversals — première version
# volontairement simple, recalculable plus tard depuis les bougies brutes.
_REV_THRESHOLD = {"NQ": 30.0, "ES": 8.0, "SPX": 10.0, "NDX": 40.0,
                  "SPY": 1.0, "QQQ": 1.2}


def _count_reversals(closes, threshold: float) -> int:
    """Compte les retournements de la série de clôtures dépassant `threshold`.

    Zigzag : on suit le plus haut et le plus bas depuis le dernier pivot ; un
    reflux de `threshold` depuis l'extrême marque un pivot. Le tout PREMIER
    mouvement (celui qui établit la tendance de départ) ne compte pas comme un
    retournement — seuls les changements de sens suivants comptent. Mesure
    objective de « combien de fois le marché s'est retourné », indépendante de
    la perception du trader.
    """
    if not closes:
        return 0
    n, direction = 0, 0            # direction : 0 inconnue, +1 haussier, -1 baissier
    hi = lo = closes[0]
    for c in closes:
        hi, lo = max(hi, c), min(lo, c)
        if direction >= 0 and hi - c >= threshold:        # reflux depuis le haut
            if direction == 1:                            # on tendait à la hausse -> vrai retournement
                n += 1
            direction, hi, lo = -1, c, c
        elif direction <= 0 and c - lo >= threshold:      # rebond depuis le bas
            if direction == -1:
                n += 1
            direction, hi, lo = 1, c, c
    return n


def _session_context(symbol: str, day: str, rev_threshold: float | None = None) -> dict:
    """Vérité de marché OBJECTIVE d'une séance, calculée depuis les bougies
    1 min stockées (`store.load_prices`). Sert au journal de recherche : ce qui
    s'est réellement passé, à confronter au ressenti du sondage.

    Fonctionne en intraday (bougies partielles du jour) comme en fin de séance.
    Renvoie `available: False` si aucune bougie n'existe pour ce symbole/jour.
    """
    from datetime import date as _date, timedelta

    from . import store

    bars = store.load_prices(symbol, day)
    d = _date.fromisoformat(day)
    out = {"symbol": symbol, "date": day, "weekday": d.weekday(), "available": False}
    if bars is None or bars.empty:
        return out
    bars = bars.sort_values("timestamp")
    o = float(bars["open"].iloc[0])
    hi = float(bars["high"].max())
    lo = float(bars["low"].min())
    last = float(bars["close"].iloc[-1])

    # clôture de la veille + ATR (moyenne des ranges quotidiens sur ~14 jours)
    prev_close, ranges = None, []
    probe = d
    for _ in range(20):
        probe -= timedelta(days=1)
        prior = store.load_prices(symbol, probe.isoformat())
        if prior is None or prior.empty:
            continue
        if prev_close is None:
            prev_close = float(prior.sort_values("timestamp")["close"].iloc[-1])
        ranges.append(float(prior["high"].max() - prior["low"].min()))
        if len(ranges) >= 14:
            break
    prev_atr = round(sum(ranges) / len(ranges), 2) if ranges else None

    rng = hi - lo
    thr = rev_threshold or _REV_THRESHOLD.get(symbol, 30.0)
    out.update({
        "available": True,
        "open": o, "high": hi, "low": lo, "close": last, "price": last,
        "prev_close": prev_close,
        "gap": round(o - prev_close, 2) if prev_close is not None else None,
        "prev_atr": prev_atr,
        "range": round(rng, 2),
        "max_up": round(hi - o, 2),
        "max_down": round(o - lo, 2),
        "close_location": round((last - lo) / rng, 3) if rng else None,
        "n_reversals": _count_reversals(bars["close"].tolist(), thr),
        "rev_threshold": thr,
    })
    return out


def _close_context(symbol: str, day: str) -> dict:
    """Pinning de clôture d'une séance : le prix s'est-il collé sur un strike /
    un mur GEX à 16h ET ? Calcul DÉRIVÉ à la demande depuis le brut (snapshot de
    chaîne ~16h + bougies), rien n'est stocké. `available: False` si les sources
    manquent (chaîne ou bougies)."""
    from . import pinning, store

    out = {"symbol": symbol, "date": day, "available": False}

    # Chaîne la plus proche de 16h ET : natif (_RT) prioritaire, sinon CBOE.
    chain = None
    for key in (f"{symbol}_RT", symbol):
        chain = store.load_snapshot_near(key, day)
        if chain is not None and not chain.empty:
            break
    if chain is None or chain.empty or "strike" not in chain or "gex" not in chain:
        out["reason"] = "pas de snapshot de chaîne pour cette séance"
        return out

    bars = store.load_prices(symbol, day)
    if bars is None or bars.empty:
        out["reason"] = "pas de bougies (prix de clôture indisponible)"
        return out
    bars = bars.sort_values("timestamp")
    target = pd.Timestamp(f"{day} 16:00:00")
    ts = pd.to_datetime(bars["timestamp"])
    close_price = float(bars["close"].iloc[(ts - target).abs().values.argmin()])

    # Fenêtre pré-clôture 15h50-16h00 ET (franchissements de strike).
    window = bars[(ts.dt.time >= dt_time(15, 50)) & (ts.dt.time <= dt_time(16, 0))]
    window_closes = [float(c) for c in window["close"].tolist()] or None

    out.update({"available": True})
    out.update(pinning.pin_metrics(chain, close_price, window_closes))
    return out


def _tick_context(symbol: str, day: str, entry: float | None = None,
                  stop: float | None = None, direction: int = 1) -> dict:
    """Fenêtre de clôture à la résolution du TICK (brut capturé 15h45-16h05 ET).

    Métriques d'excursion avant/après la clôture, et — si `entry`/`stop` sont
    fournis — le rejeu « un stop aurait-il sauté ? ». `available: False` si aucun
    tick n'a été capturé ce jour-là (capture = compte courtier requis)."""
    from . import store, tickstats

    ticks = store.load_ticks(symbol, day)
    out = {"symbol": symbol, "date": day}
    if ticks is None or ticks.empty:
        out.update({"available": False, "reason": "pas de ticks capturés cette séance"})
        return out
    split = datetime.combine(datetime.fromisoformat(day).date(),
                             dt_time(16, 0), ET).timestamp()
    out.update(tickstats.window_metrics(ticks, split))
    if entry is not None and stop is not None:
        out["stop_check"] = tickstats.stop_swept(ticks, entry, stop, direction, after_ts=split)
    return out


def _preferred(symbol: str) -> str:
    """Clé de STATE à lire : la chaîne native _RT si un compte est configuré et
    qu'elle a un état, sinon le symbole de base — même règle que l'interface
    (app.chain_state) pour que l'API montre ce que le dashboard montre."""
    from .rtquote import credentials_present
    if symbol in ("SPX", "NDX", "SPY", "QQQ") and credentials_present():
        rt = STATE.get(f"{symbol}_RT")
        with STATE.lock:
            if rt.summary is not None:
                return f"{symbol}_RT"
    return symbol


def _current_summary(symbol: str):
    """(summary, enriched) pour ce symbole, quelle que soit la source — cf.
    docstring du module sur la portée réelle de la licence."""
    st = STATE.get(_preferred(symbol))
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
        # Source UNIQUE des niveaux (cf. metrics.compute_levels) : mêmes murs que
        # le dashboard. structural_spot = clôture veille (magnitude), live_spot =
        # spot courant en séance (côté). `?bucket=` fixe le périmètre d'échéances.
        from .app import market_is_open, ref_spot as _ref_spot
        bucket = request.args.get("bucket", "0DTE")
        if bucket not in EXPIRY_BUCKETS:
            bucket = "0DTE"
        structural = _ref_spot(symbol, s.spot)
        live = s.spot if market_is_open() else structural
        res = metrics.compute_levels(df, structural, live, bucket=bucket)
        levels, keys = res["levels"], res["keys"]
        hvl = metrics.zero_gamma(df, s.spot, weight_col="volume")

        # Transposition d'échelle optionnelle : ?scale=NQ exprime les niveaux
        # NDX en prix NQ (cf. app._transform_for / le sélecteur d'unité). Utile
        # quand on trade le future mais que les niveaux viennent de l'indice.
        scale = request.args.get("scale")
        xf = (lambda v: v)
        if scale and scale.upper() != symbol:
            from .app import _transform_for
            xf, _, _ = _transform_for(symbol, scale.upper())

        def _t(v):
            return float(xf(v)) if isinstance(v, (int, float)) else v

        return jsonify({
            "symbol": symbol,
            "scale": (scale.upper() if scale else symbol),
            "spot": _t(s.spot),
            "zero_gamma": _t(s.zero_gamma),
            "hvl": _t(hvl),
            "key_levels": {k: _t(v) for k, v in keys.items()},
            "gex_walls": [
                {"strike": _t(float(r.strike)), "gex": float(r.gex), "expiry": str(r.expiry)}
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

    @server.route("/api/v1/<symbol>/session_context")
    def _session(symbol):
        """Vérité de marché objective d'une séance (OHLC, gap, ATR veille,
        excursions, retournements) — pour le journal de recherche.

        `?date=YYYY-MM-DD` (défaut : jour ET courant). `?rev=` force le seuil de
        retournement. En intraday, renvoie l'état courant (bougies partielles).
        """
        symbol = symbol.upper()
        day = request.args.get("date") or datetime.now(ET).date().isoformat()
        rev = request.args.get("rev", type=float)
        return jsonify(_session_context(symbol, day, rev))

    @server.route("/api/v1/<symbol>/close_context")
    def _close(symbol):
        """Pinning de clôture (16h ET) : distance au strike/mur GEX, pin_ratio,
        franchissements pré-clôture — calcul à la demande, pour le backtest du
        comportement de clôture. `?date=YYYY-MM-DD` (défaut : jour ET courant)."""
        symbol = symbol.upper()
        day = request.args.get("date") or datetime.now(ET).date().isoformat()
        return jsonify(_close_context(symbol, day))

    @server.route("/api/v1/<symbol>/tick_context")
    def _tick(symbol):
        """Fenêtre de clôture au tick. `?date=` ; `?entry=&stop=&dir=long|short`
        pour rejouer « un stop aurait-il sauté ? »."""
        symbol = symbol.upper()
        day = request.args.get("date") or datetime.now(ET).date().isoformat()
        entry = request.args.get("entry", type=float)
        stop = request.args.get("stop", type=float)
        direction = -1 if request.args.get("dir", "long").lower().startswith("s") else 1
        return jsonify(_tick_context(symbol, day, entry, stop, direction))

    @server.route("/api/v1/vix")
    def _vix():
        """VIX courant + seuil du digest (pour une interrogation directe)."""
        from . import digest as digest_mod
        v = digest_mod._current_vix()
        if v is None:
            return jsonify({"available": False})
        return jsonify({"available": True, "vix": round(float(v), 2),
                        "seuil": digest_mod.VIX_SEUIL,
                        "above": bool(v > digest_mod.VIX_SEUIL),
                        "grade": digest_mod.vix_grade(float(v))})

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
            "confidence": d.confidence,
            "families": d.families,
            "close_message": d.close_message,
            "text": d.to_text(),
            "signature": list(d.signature),
        })
