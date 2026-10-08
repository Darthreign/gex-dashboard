"""Barres alternatives à la fenêtre fixe de temps (`scalp.WINDOW_S=300`), et
détection de swing high/low dessus — brique du chantier /scalp v2 (piste
« swing high/low sur mèches », cf. mémoire du projet roadmap-scalp-v2).

Pourquoi : `scalp_inputs` compare le spot actuel à la bougie d'il y a 5 min,
et `net_musd`/`gross_musd` (le "soutien" annoncé par le bandeau) est sommé sur
cette même fenêtre glissante. Un cas réel du 2026-09-30 (vérifié dans
`scalp_signals`, ids 71-89) montre que cette base de mesure dit CONTRESENS
aux deux moments qui comptent : "sans soutien" pendant une vraie poussée,
"soutenu" pile au sommet avant le retournement. Hypothèse à tester : une base
alignée sur l'activité réelle (volume, nombre de trades, ou amplitude de prix)
plutôt que sur l'horloge, capture peut-être moins de bruit de microstructure.

Trois constructions, toutes pures (un DataFrame de ticks en entrée, un
DataFrame de barres en sortie) — testables sans dépendre de TickCapture :

- `tick_bars`   : une barre = N prints (N trades, peu importe leur taille).
- `volume_bars` : une barre = N contrats échangés (peut regrouper peu ou
                  beaucoup de prints selon l'activité).
- `range_bars`  : une barre se clôt dès que le prix s'écarte de `bar_range`
                  points depuis son ouverture — seule construction ancrée sur
                  le PRIX plutôt que sur l'activité.

`zigzag` détecte les swing high/low sur la série de clôtures d'un DataFrame de
barres (ou directement sur les ticks) : un nouveau extremum confirmé
seulement après un retracement d'au moins `min_move` points depuis lui —
classique, mais appliqué ici à une base ticks/volume/range plutôt qu'à des
bougies 1 min, pour tester si ça change la lecture.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _clean_ticks(ticks: pd.DataFrame) -> pd.DataFrame:
    """Même filtre que `tickcapture._last`/`_price_bar` : un côté agresseur
    indéterminé ("UNDEFINED" dxFeed) est un print potentiellement aberrant,
    on l'exclut des constructions de barres (cf. passation 2026-10-01)."""
    df = ticks[ticks["side"].isin(("BUY", "SELL"))]
    return df.sort_values("ts", kind="stable").reset_index(drop=True)


def _finish_bar(rows: list[dict]) -> dict:
    prices = [r["price"] for r in rows]
    buy_vol = sum(r["volume"] for r in rows if r["side"] == "BUY")
    sell_vol = sum(r["volume"] for r in rows if r["side"] == "SELL")
    return {
        "ts_open": rows[0]["ts"], "ts_close": rows[-1]["ts"],
        "open": prices[0], "high": max(prices), "low": min(prices),
        "close": prices[-1], "volume": buy_vol + sell_vol,
        "buy_vol": buy_vol, "sell_vol": sell_vol, "n_prints": len(rows),
    }


def tick_bars(ticks: pd.DataFrame, n_ticks: int) -> pd.DataFrame:
    """Une barre = `n_ticks` prints consécutifs (la dernière peut être
    incomplète, incluse quand même — mieux que perdre les derniers prints)."""
    if n_ticks < 1:
        raise ValueError("n_ticks doit être >= 1")
    df = _clean_ticks(ticks)
    rows = df.to_dict("records")
    bars = [_finish_bar(rows[i:i + n_ticks]) for i in range(0, len(rows), n_ticks)]
    return pd.DataFrame(bars)


def volume_bars(ticks: pd.DataFrame, bar_volume: float) -> pd.DataFrame:
    """Une barre se clôt dès que le volume cumulé atteint `bar_volume`
    contrats — peut regrouper peu de gros prints ou beaucoup de petits.

    Vectorisé : la fin de chaque barre est le premier tick où le volume
    cumulé depuis la fin de la précédente atteint `bar_volume` (recherche
    dichotomique sur le cumul, une itération PAR BARRE et non par tick), puis
    les agrégats par `reduceat`. Mêmes barres que la boucle tick par tick
    d'origine, ~50× plus rapide sur une séance pleine."""
    if bar_volume <= 0:
        raise ValueError("bar_volume doit être > 0")
    df = _clean_ticks(ticks)
    if df.empty:
        return pd.DataFrame()
    vol = df["volume"].to_numpy(dtype=float)
    cum = np.cumsum(vol)
    ends, base, n = [], 0.0, len(vol)
    while True:
        i = int(np.searchsorted(cum, base + bar_volume, side="left"))
        if i >= n:
            break
        ends.append(i)
        base = cum[i]
    if not ends or ends[-1] != n - 1:
        ends.append(n - 1)             # dernière barre incomplète, gardée
    ends = np.asarray(ends)
    starts = np.concatenate([[0], ends[:-1] + 1])
    price = df["price"].to_numpy(dtype=float)
    ts = df["ts"].to_numpy()
    is_buy = (df["side"] == "BUY").to_numpy()
    buy = np.add.reduceat(np.where(is_buy, vol, 0.0), starts)
    sell = np.add.reduceat(np.where(is_buy, 0.0, vol), starts)
    return pd.DataFrame({
        "ts_open": ts[starts], "ts_close": ts[ends],
        "open": price[starts], "high": np.maximum.reduceat(price, starts),
        "low": np.minimum.reduceat(price, starts), "close": price[ends],
        "volume": buy + sell, "buy_vol": buy, "sell_vol": sell,
        "n_prints": ends - starts + 1,
    })


def range_bars(ticks: pd.DataFrame, bar_range: float) -> pd.DataFrame:
    """Une barre se clôt dès que le prix s'écarte de `bar_range` points de
    l'ouverture de la barre (dans un sens OU l'autre) — seule construction où
    la taille de barre dépend du PRIX, pas de l'activité."""
    if bar_range <= 0:
        raise ValueError("bar_range doit être > 0")
    df = _clean_ticks(ticks)
    bars, cur = [], []
    for row in df.to_dict("records"):
        cur.append(row)
        open_px = cur[0]["price"]
        if abs(row["price"] - open_px) >= bar_range:
            bars.append(_finish_bar(cur))
            cur = []
    if cur:
        bars.append(_finish_bar(cur))
    return pd.DataFrame(bars)


def zigzag(bars: pd.DataFrame, min_move: float,
          price_col: str = "close", ts_col: str = "ts_close") -> pd.DataFrame:
    """Swing high/low confirmés : un extremum n'est retenu que lorsque le prix
    s'en est ensuite écarté d'au moins `min_move` dans l'autre sens — la même
    logique que l'indicateur ZigZag classique, appliquée ici à une série de
    barres (temps, ticks, volume ou range, au choix de l'appelant).

    Renvoie un DataFrame (`ts`, `price`, `kind` ∈ {"H", "L"}), dans l'ordre
    chronologique. Le DERNIER point est provisoire (pas encore confirmé par un
    retracement) — à l'appelant de décider s'il veut le garder."""
    if min_move <= 0:
        raise ValueError("min_move doit être > 0")
    if bars.empty:
        return pd.DataFrame(columns=["ts", "price", "kind"])

    prices = bars[price_col].to_numpy()
    tss = bars[ts_col].to_numpy()

    # direction : 0 = pas encore de pivot confirmé, 1 = on cherche le prochain
    # sommet (dernier pivot confirmé = un creux), -1 = on cherche le prochain
    # creux. hi_i/lo_i suivent le plus haut/plus bas depuis le DERNIER pivot
    # confirmé (ou depuis le début, tant qu'aucun pivot n'est confirmé — les
    # deux sont suivis en parallèle jusqu'à ce que l'un des deux déclenche).
    direction = 0
    hi_i = lo_i = 0
    swings: list[dict] = []

    for i in range(1, len(prices)):
        if prices[i] > prices[hi_i]:
            hi_i = i
        if prices[i] < prices[lo_i]:
            lo_i = i

        if direction != 1 and prices[i] - prices[lo_i] >= min_move:
            swings.append({"ts": tss[lo_i], "price": prices[lo_i], "kind": "L"})
            direction = 1
            hi_i = lo_i = i
        elif direction != -1 and prices[hi_i] - prices[i] >= min_move:
            swings.append({"ts": tss[hi_i], "price": prices[hi_i], "kind": "H"})
            direction = -1
            hi_i = lo_i = i

    # Pivot en cours, pas encore confirmé par un retracement suffisant : gardé
    # en provisoire ("?") — à l'appelant de décider s'il veut le garder.
    prov_i = hi_i if direction >= 0 else lo_i
    if not swings or swings[-1]["ts"] != tss[prov_i]:
        kind = "H?" if direction >= 0 else "L?"
        swings.append({"ts": tss[prov_i], "price": prices[prov_i], "kind": kind})

    return pd.DataFrame(swings)


def trend_move(swings: pd.DataFrame, current_price: float) -> float | None:
    """Variation de prix depuis le dernier pivot CONFIRMÉ de sens opposé —
    l'équivalent, ancré sur la structure plutôt que sur l'horloge, de
    `scalp.scalp_inputs`'s `move = spot - bougie d'il y a 5 min`.

    Ex. si le dernier pivot confirmé est un creux ("L"), on est en recherche
    d'un sommet : le mouvement rapporté est haussier, `current_price - prix
    du creux`. None si aucun pivot confirmé n'existe encore (segment initial,
    pas assez de données pour juger)."""
    confirmed = swings[~swings["kind"].str.endswith("?")]
    if confirmed.empty:
        return None
    last = confirmed.iloc[-1]
    return float(current_price - last["price"])


def swing_move(ticks: pd.DataFrame, current_price: float, *,
               bar_volume: float = 60.0, min_move: float = 15.0) -> float | None:
    """Mouvement ancré-structure, prêt à remplacer la comparaison à une
    bougie fixe de `scalp_inputs` (gex/app.py) — combine `volume_bars` +
    `zigzag` + `trend_move` en un seul appel pour l'appelant du bandeau v2.

    `ticks` : DataFrame de ticks BRUTS de la séance en cours jusqu'à
    maintenant (ex. `store.load_ticks` ou l'équivalent live côté capture) —
    le filtrage BUY/SELL est fait en interne, pas à l'appelant.

    Défauts (`bar_volume=60`, `min_move=15`) choisis par cohérence avec la
    validation du 2026-10-03 sur le cas du 30/09 (cf. mémoire du projet
    roadmap-scalp-v2) — PAS calibrés formellement, à ajuster si une
    calibration plus poussée les contredit. `None` si pas assez de ticks pour
    construire ne serait-ce qu'une poignée de barres (même convention que
    `trend_move` : pas de pivot confirmé, pas de verdict)."""
    bars = volume_bars(ticks, bar_volume=bar_volume)
    if len(bars) < 5:
        return None
    swings = zigzag(bars, min_move=min_move)
    return trend_move(swings, current_price)
