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
        "card_spot": "Spot (délayé 15 min)",
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
        "side_call": "call",
        "side_put": "put",
        "bucket_0DTE": "0DTE",
        "bucket_week": "Semaine",
        "bucket_month": "Mois",
        "bucket_all": "Tout",
        "majors_only": "Major Walls seulement",
        "unit_index": "Indice",
        "unit_futures": "Futures",
        "basis_note": "niveaux convertis en {fut} (basis {basis:+.0f} pts)",
        "basis_unavailable": "basis futures indisponible — niveaux en points d'indice",
        "flow_day_label": "Jour de flux :",
        "last_session": "Dernière séance",
        "footer": "Données CBOE delayed (~15 min) — outil d'analyse, pas d'exécution.",
        "hover_strike": "Strike",
        "hover_net": "Net",
        "hover_flow": "Flux",
        "hover_cum": "Cumul",
    },
    "en": {
        "app_title": "Gamma / Delta Exposure",
        "waiting_first_pull": "Waiting for first CBOE pull…",
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
        "card_spot": "Spot (15-min delayed)",
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
        "side_call": "call",
        "side_put": "put",
        "bucket_0DTE": "0DTE",
        "bucket_week": "Week",
        "bucket_month": "Month",
        "bucket_all": "All",
        "majors_only": "Major Walls only",
        "unit_index": "Index",
        "unit_futures": "Futures",
        "basis_note": "levels converted to {fut} (basis {basis:+.0f} pts)",
        "basis_unavailable": "futures basis unavailable — levels in index points",
        "flow_day_label": "Flow day:",
        "last_session": "Last session",
        "footer": "CBOE delayed data (~15 min) — analysis tool, not for execution.",
        "hover_strike": "Strike",
        "hover_net": "Net",
        "hover_flow": "Flow",
        "hover_cum": "Cumulative",
    },
}


def t(lang: str, key: str, **fmt) -> str:
    s = TR.get(lang, TR["fr"]).get(key, key)
    return s.format(**fmt) if fmt else s


def wall_labels(levels) -> dict:
    """Classement non directionnel des murs de gamma : GEX1..GEXn par |GEX|.

    Les niveaux directionnels (Call Wall au-dessus du spot, Put Support en
    dessous) sont calculés à part par metrics.key_levels — les mélanger ici
    produirait des « supports » situés au-dessus du prix.
    """
    if levels is None or len(levels) == 0:
        return {}
    return {lv.strike: f"GEX{lv.rank}" for lv in levels.itertuples()}
