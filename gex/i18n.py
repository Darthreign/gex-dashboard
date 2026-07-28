"""Traductions de l'interface. Les termes de trading standards (Call Wall,
Put Wall, Gamma Flip, HVL, 0DTE, GEX…) restent en anglais dans les deux
langues — c'est le vocabulaire courant des traders d'options, FR compris.
"""
from __future__ import annotations

LANGS = ["fr", "en"]

TR: dict[str, dict[str, str]] = {
    "fr": {
        "app_title": "Gamma / Delta Exposure",
        "waiting_first_pull": "En attente du premier pull CBOE…",
        "waiting_native": "Collecte native en cours (chaîne CME complète, ~3 à 5 min)…",
        "card_spot_delayed": "Spot (délayé, sans compte)",
        "card_spot_delayed_sub": "flux public dxFeed, ~15-20 min de retard",
        "native_no_chain": "Chaîne d'options native indisponible sans compte courtier.",
        "native_no_chain_delayed": "Chaîne d'options native indisponible sans compte — spot délayé ci-dessous.",
        "native_banner": "Données {sym} indisponibles : nécessitent un compte courtier (identifiants dxFeed manquants).",
        "native_more_info": "Plus d'infos",
        "native_overlay_title": "{sym} nécessite un compte courtier",
        "native_overlay_body": "Les chaînes natives sur futures CME ({sym}) exigent un flux temps réel "
                        "dxFeed via un compte courtier — gratuit avec le compte, mais pas configuré ici. "
                        "Ce n'est pas pénalisant en attendant : {alt} affiche des niveaux quasi "
                        "identiques, transposables vers {sym} via l'échelle dans le sélecteur d'unité.",
        "native_overlay_link": "Comment activer le temps réel",
        "native_overlay_ok": "OK, afficher {alt}",
        "no_data_window": "Pas de données dans la fenêtre de strikes",
        "no_flow_day": "Aucun flux enregistré le {day}",
        "not_enough_history": "Historique insuffisant (revenez après quelques snapshots)",
        "no_iv": "Pas d'IV exploitable",
        "gex_title": "Gamma Exposure par strike — {bucket}",
        "dex_title": "Delta Exposure par strike — {bucket}",
        "flow_title": "Flux delta options (proxy Δvolume×δ, barres 1 min — délayé ~15 min)",
        "hist_title": "GEX net — historique ($Bn par 1%)",
        "spotzg_title": "Spot vs Gamma Flip",
        "smile_title": "Skew IV (options OTM) par expiration",
        "axis_bn_per_move": "$Bn par 1% de move",
        "axis_m_per_min": "$M/min",
        "axis_cum_m": "Cumul $M",
        "axis_iv": "IV %",
        "legend_spot": "Spot",
        "legend_zg": "Gamma Flip",
        "legend_flow": "Flux/min",
        "legend_cum": "Cumul",
        "heat_title": "Profil de gamma et parcours du prix — {day}",
        "heat_axis_time": "Heure",
        "heat_axis_bn": "Gamma Exposure ($Bn par 1%)",
        "legend_gex_oi": "GEX (open interest)",
        "legend_gex_vol": "GEX (volume du jour)",
        "heat_axis_strike": "Strike",
        "heat_day_label": "Séance :",
        "heat_hint": "Barres = gamma par strike (axe du haut, $Bn) ; courbe = parcours du prix (axe du bas). Les deux partagent l'axe des prix : c'est leur croisement qui renseigne.",
        "heat_none": "Aucun snapshot enregistré le {day}",
        "tab_heat": "Heatmap",
        "gflow_title": "Gamma échangé cumulé — calls vs puts",
        "legend_gcalls": "Calls (gamma +)",
        "legend_gputs": "Puts (gamma −)",
        "legend_gnet": "Net",
        "axis_gflow_bn": "$Bn de gamma cumulé",
        "card_spot": "Spot (délayé 15 min)",
        "card_spot_rt": "Spot (temps réel)",
        "card_spot_live": "GEX net recalculé à ce spot",
        "card_feed": "feed {local} ({et} ET)",
        "card_net_gex": "GEX net / 1%",
        "stabilizing": "stabilisant",
        "destabilizing": "déstabilisant",
        "card_zero_gamma": "Gamma Flip",
        "card_zg_sub": "spot {sign}{pts} pts (régime γ{reg})",
        "card_gex_0dte": "GEX 0DTE",
        "card_pc_oi": "P/C Open Interest",
        "card_pc_vol": "P/C Volume",
        "card_status": "Statut",
        "waiting_short": "en attente du premier pull",
        "levels_prefix": "Niveaux 0DTE ({exp}) :",
        "levels_unavailable": "Niveaux GEX 0DTE : indisponibles",
        "tv_copy_title": "Copier les niveaux pour TradingView (échelle {scale})",
        "side_call": "call",
        "side_put": "put",
        "bucket_0DTE": "0DTE",
        "bucket_week": "Semaine",
        "bucket_month": "Mois",
        "bucket_all": "Tout",
        "majors_only": "Major Walls seulement",
        "unit_index": "Indice",
        "unit_futures": "Futures",
        "brand_sub": "SPX · NDX · SPY · QQQ — données CBOE delayed",
        # spot temps réel actif : les CHAÎNES restent délayées, seul le prix
        # courant est en direct — la nuance doit rester lisible
        "brand_sub_rt": "SPX · NDX · SPY · QQQ — spot temps réel · chaînes CBOE delayed",
        "rt_connected": "Spot temps réel actif",
        "rt_degraded": "Flux dégradé — cotations en retard, repli sur le spot CBOE",
        "rt_disconnected": "Flux déconnecté — spot CBOE delayed utilisé",
        "lbl_expiry": "Échéance",
        "lbl_window": "Fenêtre",
        "tab_main": "Vue principale",
        "tab_profile": "Gamma Profile",
        "tab_greeks2": "Vanna & Charm",
        "tab_pos": "Positionnement",
        "profile_title": "Profil de GEX net selon le spot ($Bn par 1%)",
        "profile_by_exp": "Profil par échéance",
        "profile_axis": "Spot hypothétique",
        "profile_hint": "IV et maturités figées : seul le spot varie. La pente au prix "
                        "actuel indique la vitesse de dégradation du régime.",
        "slope_card": "Pente au spot",
        "slope_unit": "$Bn par 1% de move",
        "vex_title": "Vanna Exposure par strike ($M par point de vol)",
        "cex_title": "Charm Exposure par strike ($M de delta par jour)",
        "vex_card": "Vanna Exposure nette",
        "cex_card": "Charm Exposure nette",
        "vex_hint": "Vanna : re-hedging quand l'IV bouge d'un point. "
                    "Charm : flux mécanique par jour écoulé, moteur des dérives de fin de séance.",
        "pos_title": "Variation d'open interest par strike (vs {day})",
        "pos_hint": "L'OI est publié une fois par jour : cet écart mesure le positionnement "
                    "net ouvert ou fermé, à distinguer du gamma résiduel.",
        "pos_no_prev": "Pas de séance précédente en base — revenez après quelques jours de collecte.",
        "pos_no_change": "Aucune variation : l'open interest n'est publié qu'une fois par jour "
                         "(le matin). Comparaison significative à partir de la prochaine séance.",
        "legend_calls": "Calls",
        "legend_puts": "Puts",
        "opened": "ouvertes",
        "closed": "fermées",
        "lbl_scale": "Échelle",
        "scale_basis": "niveaux convertis en {scale} (basis appliqué)",
        "scale_ratio": "niveaux transposés en {scale} (ratio ×{ratio})",
        "scale_cross": "⚠ transposition croisée en {scale} (ratio ×{ratio}) — "
                       "repère instantané, le ratio dérive dans le temps",
        "flow_day_label": "Jour de flux :",
        "gflow_series_label": "Séries :",
        "heat_levels_label": "Niveaux :",
        "heat_levels_gex_walls": "Murs GEX",
        "last_session": "Dernière séance",
        "footer": "Données CBOE delayed (~15 min) — outil d'analyse, pas d'exécution.",
        "hover_strike": "Strike",
        "hover_net": "Net",
        "hover_flow": "Flux",
        "hover_cum": "Cumul",
        "regime_label": "Lecture du régime",
        "regime_frein": "Régime freiné (gamma positif) : les dealers vendent les hausses et "
                        "achètent les baisses, range probable. Delta dealers {sens_delta} — "
                        "pression de couverture {pression} latente en toile de fond, léger "
                        "biais {biais} si le range casse.",
        "regime_accel_modere": "Régime accélérateur (gamma négatif) : un mouvement s'auto-"
                        "entretient une fois lancé. Delta dealers {sens_delta} (pression de "
                        "couverture {pression}) : biais {biais} si un mouvement démarre. "
                        "Rester prudent, le trading contrarien est risqué dans ce régime.",
        "regime_accel_fort": "Régime accélérateur (gamma négatif) ET forte pression de "
                        "couverture {pression} des dealers (delta {sens_delta}) : les deux "
                        "mécaniques s'additionnent. Risque élevé de mouvement {biais} "
                        "auto-entretenu — le trading contrarien y est particulièrement risqué.",
        "regime_disclaimer": "Lecture mécanique de la couverture dealers, pas un signal "
                        "d'entrée ni une direction garantie.",
    },
    "en": {
        "app_title": "Gamma / Delta Exposure",
        "waiting_first_pull": "Waiting for first CBOE pull…",
        "waiting_native": "Native collection in progress (full CME chain, ~3 to 5 min)…",
        "card_spot_delayed": "Spot (delayed, no account)",
        "card_spot_delayed_sub": "public dxFeed stream, ~15-20 min behind",
        "native_no_chain": "Native options chain unavailable without a broker account.",
        "native_no_chain_delayed": "Native options chain unavailable without an account — delayed spot below.",
        "native_banner": "{sym} data unavailable: requires a broker account (dxFeed credentials missing).",
        "native_more_info": "More info",
        "native_overlay_title": "{sym} requires a broker account",
        "native_overlay_body": "Native CME futures chains ({sym}) require a real-time dxFeed stream via "
                        "a broker account — free with the account, but not configured here. Nothing is "
                        "lost in the meantime: {alt} shows nearly identical levels, transposable to "
                        "{sym} via the scale picker.",
        "native_overlay_link": "How to enable real-time",
        "native_overlay_ok": "OK, show {alt}",
        "no_data_window": "No data in the strike window",
        "no_flow_day": "No flow recorded on {day}",
        "not_enough_history": "Not enough history yet (check back after a few snapshots)",
        "no_iv": "No usable IV",
        "gex_title": "Gamma Exposure by strike — {bucket}",
        "dex_title": "Delta Exposure by strike — {bucket}",
        "flow_title": "Options delta flow (Δvolume×δ proxy, 1-min bars — ~15 min delayed)",
        "hist_title": "Net GEX — history ($Bn per 1%)",
        "spotzg_title": "Spot vs Gamma Flip",
        "smile_title": "IV skew (OTM options) by expiration",
        "axis_bn_per_move": "$Bn per 1% move",
        "axis_m_per_min": "$M/min",
        "axis_cum_m": "Cumulative $M",
        "axis_iv": "IV %",
        "legend_spot": "Spot",
        "legend_zg": "Gamma Flip",
        "legend_flow": "Flow/min",
        "legend_cum": "Cumulative",
        "heat_title": "Gamma profile and price path — {day}",
        "heat_axis_time": "Time",
        "heat_axis_bn": "Gamma Exposure ($Bn per 1%)",
        "legend_gex_oi": "GEX (open interest)",
        "legend_gex_vol": "GEX (session volume)",
        "heat_axis_strike": "Strike",
        "heat_day_label": "Session:",
        "heat_hint": "Bars = gamma by strike (top axis, $Bn); line = price path (bottom axis). Both share the price axis: what informs you is where they meet.",
        "heat_none": "No snapshot recorded on {day}",
        "tab_heat": "Heatmap",
        "gflow_title": "Cumulative gamma traded — calls vs puts",
        "legend_gcalls": "Calls (gamma +)",
        "legend_gputs": "Puts (gamma −)",
        "legend_gnet": "Net",
        "axis_gflow_bn": "$Bn cumulative gamma",
        "card_spot": "Spot (15-min delayed)",
        "card_spot_rt": "Spot (real-time)",
        "card_spot_live": "net GEX recomputed at this spot",
        "card_feed": "feed {local} ({et} ET)",
        "card_net_gex": "Net GEX / 1%",
        "stabilizing": "stabilizing",
        "destabilizing": "destabilizing",
        "card_zero_gamma": "Gamma Flip",
        "card_zg_sub": "spot {sign}{pts} pts (γ{reg} regime)",
        "card_gex_0dte": "0DTE GEX",
        "card_pc_oi": "P/C Open Interest",
        "card_pc_vol": "P/C Volume",
        "card_status": "Status",
        "waiting_short": "waiting for first pull",
        "levels_prefix": "0DTE levels ({exp}):",
        "levels_unavailable": "0DTE GEX levels: unavailable",
        "tv_copy_title": "Copy levels for TradingView ({scale} scale)",
        "side_call": "call",
        "side_put": "put",
        "bucket_0DTE": "0DTE",
        "bucket_week": "Week",
        "bucket_month": "Month",
        "bucket_all": "All",
        "majors_only": "Major Walls only",
        "unit_index": "Index",
        "unit_futures": "Futures",
        "brand_sub": "SPX · NDX · SPY · QQQ — CBOE delayed data",
        "brand_sub_rt": "SPX · NDX · SPY · QQQ — real-time spot · CBOE delayed chains",
        "rt_connected": "Real-time spot active",
        "rt_degraded": "Feed degraded — quotes lagging, falling back to CBOE spot",
        "rt_disconnected": "Feed disconnected — using CBOE delayed spot",
        "lbl_expiry": "Expiry",
        "lbl_window": "Window",
        "tab_main": "Main view",
        "tab_profile": "Gamma Profile",
        "tab_greeks2": "Vanna & Charm",
        "tab_pos": "Positioning",
        "profile_title": "Net GEX profile vs spot ($Bn per 1%)",
        "profile_by_exp": "Profile by expiration",
        "profile_axis": "Hypothetical spot",
        "profile_hint": "IV and maturities frozen: only spot moves. The slope at the current "
                        "price shows how fast the regime degrades.",
        "slope_card": "Slope at spot",
        "slope_unit": "$Bn per 1% move",
        "vex_title": "Vanna Exposure by strike ($M per vol point)",
        "cex_title": "Charm Exposure by strike ($M of delta per day)",
        "vex_card": "Net Vanna Exposure",
        "cex_card": "Net Charm Exposure",
        "vex_hint": "Vanna: re-hedging when IV moves one point. "
                    "Charm: mechanical flow per day elapsed, driver of end-of-day drifts.",
        "pos_title": "Open interest change by strike (vs {day})",
        "pos_hint": "OI is published once daily: this delta measures net positioning opened "
                    "or closed, as opposed to legacy residual gamma.",
        "pos_no_prev": "No previous session stored yet — check back after a few days of collection.",
        "pos_no_change": "No change: open interest is published once a day (in the morning). "
                         "Meaningful comparison starts from the next session.",
        "legend_calls": "Calls",
        "legend_puts": "Puts",
        "opened": "opened",
        "closed": "closed",
        "lbl_scale": "Scale",
        "scale_basis": "levels converted to {scale} (basis applied)",
        "scale_ratio": "levels transposed to {scale} (ratio ×{ratio})",
        "scale_cross": "⚠ cross-family transposition to {scale} (ratio ×{ratio}) — "
                       "instantaneous reference, the ratio drifts over time",
        "flow_day_label": "Flow day:",
        "gflow_series_label": "Series:",
        "heat_levels_label": "Levels:",
        "heat_levels_gex_walls": "GEX walls",
        "last_session": "Last session",
        "footer": "CBOE delayed data (~15 min) — analysis tool, not for execution.",
        "hover_strike": "Strike",
        "hover_net": "Net",
        "hover_flow": "Flow",
        "hover_cum": "Cumulative",
        "regime_label": "Regime read",
        "regime_frein": "Dampened regime (positive gamma): dealers sell rallies and buy dips, "
                        "range likely. Dealer delta {sens_delta} — latent {pression} pressure "
                        "in the background, mild {biais} bias if the range breaks.",
        "regime_accel_modere": "Accelerating regime (negative gamma): a move feeds on itself "
                        "once started. Dealer delta {sens_delta} (latent {pression} pressure): "
                        "{biais} bias if a move starts. Stay cautious, contrarian trading may be "
                        "risky in this regime.",
        "regime_accel_fort": "Accelerating regime (negative gamma) AND strong latent {pression} "
                        "pressure from dealers (delta {sens_delta}): both mechanics add up. High "
                        "risk of a self-reinforcing {biais} move — contrarian trading may be "
                        "particularly risky here.",
        "regime_disclaimer": "A mechanical read of dealer hedging, not an entry signal or a "
                        "guaranteed direction.",
    },
}

# Mots dérivés des codes neutres renvoyés par metrics.regime_read (pas de mot
# figé dans une langue à la source, cf. commentaire dans regime_read).
_PRESSURE_WORD = {"fr": {"sell": "vendeuse", "buy": "acheteuse"},
                  "en": {"sell": "selling", "buy": "buying"}}
_BIAS_WORD = {"fr": {"down": "baissier", "up": "haussier"},
             "en": {"down": "downward", "up": "upward"}}


def t(lang: str, key: str, **fmt) -> str:
    s = TR.get(lang, TR["fr"]).get(key, key)
    return s.format(**fmt) if fmt else s


def regime_text(lang: str, regime: dict) -> str:
    """Compose le texte de lecture de régime à partir du dict `metrics.regime_read`."""
    p = regime["params"]
    words = {"sens_delta": p["sens_delta"],
             "pression": _PRESSURE_WORD[lang][p["pression_code"]],
             "biais": _BIAS_WORD[lang][p["biais_code"]]}
    return t(lang, regime["i18n_key"], **words)


def wall_labels(levels) -> dict:
    """Classement non directionnel des murs de gamma : GEX1..GEXn par |GEX|.

    Les niveaux directionnels (Call Wall au-dessus du spot, Put Support en
    dessous) sont calculés à part par metrics.key_levels — les mélanger ici
    produirait des « supports » situés au-dessus du prix.
    """
    if levels is None or len(levels) == 0:
        return {}
    return {lv.strike: f"GEX{lv.rank}" for lv in levels.itertuples()}
