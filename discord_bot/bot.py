"""Bot Discord — diffuse l'état du gamma calculé par le dashboard GEX.

Composant volontairement SÉPARÉ et léger : il ne fait aucun calcul et ne voit
jamais la donnée brute. Il interroge l'API locale du dashboard
(`/api/v1/digest`, `/api/v1/<sym>/summary`) — qui ne renvoie que des analyses
dérivées — et les relaie dans un salon Discord. On peut donc le partager avec
des amis sans qu'ils aient de compte courtier ni accès aux chaînes dxFeed.

Ce qu'il fait :
- poste l'état du gamma à heures fixes (8h30 / 15h25 / 15h35 Paris), plus un
  message de « clôture » à 16h (stop contrarien + sens des MM + bonne soirée) ;
- poste aussi à chaque CHANGEMENT DE RÉGIME pendant la session US (le verdict,
  jugé par famille S&P / Nasdaq, qui bascule) ;
- répond aux commandes : `!etat`/`!gamma` (digest complet), `!gamma SYM`
  (valeurs calculées), `!niveaux SYM [ÉCHELLE]` (niveaux, transposables),
  n'importe quel graphique en image (`!heatmap`, `!delta`…), et `!help` ;
- COLLECTE pour le backtest (cf. journal.py) : sondage de séance à réactions
  (23h05, dépouillé J+1 12h), snapshots de régime (open + changements +
  heartbeat), heatmaps aux créneaux clés, et contexte de marché objectif — le
  tout dans une base SQLite locale (`data/journal/`).

Prérequis (à faire UNE fois, côté Discord) :
  1. https://discord.com/developers/applications -> New Application -> Bot
  2. Activer « MESSAGE CONTENT INTENT » dans l'onglet Bot
  3. Copier le token du bot
  4. Inviter le bot sur ton serveur (OAuth2 -> URL Generator -> scope bot,
     permission « Send Messages »)
  5. Renseigner les variables d'environnement (cf. .env.example)

⚠️ Le token du bot est un SECRET : variable d'environnement, jamais commité.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import random
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import requests
from discord.ext import commands, tasks

import journal

# Charge un .env local s'il existe, pour ne pas avoir à toucher aux variables
# d'environnement Windows. Optionnel : sans python-dotenv, on lit directement
# os.environ (variables utilisateur/système), donc les deux voies marchent.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gex-bot")

PARIS = ZoneInfo("Europe/Paris")
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
DASHBOARD = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8050").rstrip("/")

# Heures Paris des posts fixes (h, min). Modifiable sans toucher au reste.
# 15h25 = juste avant l'open US (15h30 = 9h30 ET) ; 15h35 = mise à jour juste
# après l'open.
SCHEDULE = {(8, 30), (15, 25), (15, 35)}
# Message de « clôture » (Paris) : stop contrarien + sens des MM + bonne soirée.
CLOSE_POST = (16, 0)
# Session US en heure de Paris (15h30 = 9h30 ET open ; ~22h = 16h ET close).
SESSION_START, SESSION_END = dt.time(15, 30), dt.time(22, 0)

# --- Journal de recherche (backtest) ----------------------------------------
# Base et images rangées avec le reste des données du dashboard (D:\Gex\data),
# calculé depuis l'emplacement du bot pour rester portable. Surchargeable.
JOURNAL_DB = os.environ.get("JOURNAL_DB") or str(
    Path(__file__).resolve().parent.parent / "data" / "journal" / "journal.sqlite")
HEATMAP_DIR = Path(JOURNAL_DB).parent / "heatmaps"
# Créneaux de capture heatmap (h, min) -> libellé de slot.
HEATMAP_SLOTS = {(15, 30): "15h30", (16, 0): "16h00", (18, 0): "18h00", (22, 0): "22h00"}
HEATMAP_SYMBOLS = ("SPX", "SPY", "NDX", "QQQ", "ES", "NQ")
CONTEXT_SYMBOLS = ("NQ", "ES", "SPX", "NDX")   # vérité-marché pour daily_metrics
POLL_POST = (23, 5)                            # heure de post du sondage (Lun-Ven)
# LE sondage, défini une seule fois : (titre, [(emoji, colonne, libellé), …]).
# Ajouter/retirer une question = éditer ceci (+ la colonne côté journal). Les
# clés de colonne doivent matcher journal.POLL_COUNT_COLS.
POLL_QUESTIONS = (
    ("1. La journée a-t-elle été directionnelle ?", [
        ("😰", "q1_directionnel", "Oui, pas de retour"),
        ("🧘", "q1_retracement", "Non, on a eu des retracements (même petits)")]),
    ("2. L'ouverture a été :", [
        ("📈", "q2_haussier", "Haussière"),
        ("📉", "q2_baissier", "Baissière"),
        ("➡️", "q2_neutre", "Sans direction franche")]),
    ("3. Y a-t-il eu une phase directionnelle ?", [
        ("✅", "q3_dir_oui", "Oui"),
        ("❌", "q3_dir_non", "Non")]),
    ("4. Si oui, à partir de quand ?", [
        ("🌅", "q4_avant_1615", "Avant 16h15"),
        ("🌆", "q4_apres_1615", "Après 16h15")]),
    ("5. Ampleur du mouvement ? *(NQ pts / ES pts)*", [
        ("1️⃣", "q5_b1", "100-200 / 25-50"),
        ("2️⃣", "q5_b2", "200-400 / 50-100"),
        ("3️⃣", "q5_b3", "400-600 / 100-150"),
        ("4️⃣", "q5_b4", "600+ / 150+")]),
    ("6. Le régime affiché t'a semblé…", [
        ("🎯", "q6_repr_high", "Très représentatif"),
        ("😐", "q6_repr_mid", "Moyen"),
        ("🤷", "q6_repr_low", "Peu représentatif")]),
)
# Réaction -> colonne (dérivé ; l'ordre = ordre d'amorçage des réactions).
POLL_EMOJIS = {emoji: col for _, opts in POLL_QUESTIONS for emoji, col, _ in opts}

_last_signature: tuple | None = None
_last_digest: dict | None = None      # dernier digest, pour la raison d'un changement
_posted: dict[str, set] = {}          # jour ISO -> {(h, min) déjà postés}
_JC = None                            # connexion SQLite (ouverte à la 1re demande)


def _journal():
    """Connexion au journal, ouverte paresseusement (une fois). None si échec —
    la collecte est alors désactivée sans casser le reste du bot."""
    global _JC
    if _JC is None:
        try:
            _JC = journal.connect(JOURNAL_DB)
            log.info("Journal de recherche ouvert : %s", JOURNAL_DB)
        except Exception:  # noqa: BLE001
            log.exception("Journal indisponible (%s) — collecte désactivée", JOURNAL_DB)
            return None
    return _JC


def fetch(path: str) -> dict | None:
    try:
        r = requests.get(f"{DASHBOARD}{path}", timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException as exc:
        log.warning("Dashboard injoignable (%s) : %s", path, exc)
        return None


def _embed(d: dict) -> discord.Embed:
    return discord.Embed(description=d["text"], color=d.get("discord_color", 0x95A5A6))


intents = discord.Intents.default()
intents.message_content = True        # nécessaire pour lire les commandes « ! »
# help_command=None : on remplace le !help auto de discord.py par le nôtre, plus
# lisible et regroupé par thème (cf. la commande `help` plus bas).
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


async def _post(d: dict) -> None:
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.warning("Salon %s introuvable — le bot est-il invité et l'ID correct ?",
                    CHANNEL_ID)
        return
    await channel.send(embed=_embed(d))


async def _post_close(d: dict) -> None:
    """Message de clôture (stop contrarien + sens des MM + bonne soirée)."""
    channel = bot.get_channel(CHANNEL_ID)
    msg = d.get("close_message")
    if channel is None or not msg:
        return
    await channel.send(embed=discord.Embed(description=msg, color=0x2C3E50))


def _en_session(now: dt.datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.timetz().replace(tzinfo=None) <= SESSION_END


# --------------------------------------------------------------------------
# Collecte pour le journal de recherche (heatmaps, régimes, sondage)
# --------------------------------------------------------------------------

def _fetch_png(symbol: str, chart: str) -> bytes | None:
    try:
        r = requests.get(f"{DASHBOARD}/api/v1/{symbol}/chart/{chart}.png", timeout=45)
    except requests.RequestException:
        return None
    return r.content if r.status_code == 200 and r.content[:4] == b"\x89PNG" else None


def _sub(a, b):
    return round(a - b, 2) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None


def _market_snapshot() -> dict | None:
    """État du marché à l'instant T (NQ/ES) : prix + distances open/high/low.
    Quelques nombres, capturés surtout aux changements de régime."""
    out = {}
    for key, sym in (("nq", "NQ"), ("es", "ES")):
        c = fetch(f"/api/v1/{sym}/session_context")
        if c and c.get("available"):
            p = c.get("price")
            out[key] = {"price": p, "dist_open": _sub(p, c.get("open")),
                        "dist_high": _sub(p, c.get("high")), "dist_low": _sub(p, c.get("low"))}
    return out or None


async def _record_regime(now, kind: str, d: dict, reason=None, with_market=True) -> None:
    jc = _journal()
    if jc is None or d is None:
        return
    market = _market_snapshot() if with_market else None
    journal.record_regime(
        jc, date=now.date().isoformat(), ts=now.isoformat(), kind=kind,
        color=d.get("color"), confidence=d.get("confidence"), verdict=d.get("verdict"),
        reason=reason, families=d.get("families"), digest=d, market=market)


def _prev_for_reason() -> dict | None:
    if _last_digest is None:
        return None
    return {"color": _last_digest.get("color"), "confidence": _last_digest.get("confidence"),
            "families_json": json.dumps(_last_digest.get("families") or {})}


async def _capture_heatmaps(date: str, slot_label: str, now) -> None:
    jc = _journal()
    if jc is None:
        return
    got = 0
    for sym in HEATMAP_SYMBOLS:
        if journal.heatmap_done(jc, date, slot_label, sym):
            continue
        png = _fetch_png(sym, "heatmap")
        if png is None:
            continue
        folder = HEATMAP_DIR / date
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{slot_label}_{sym}.png"
        path.write_bytes(png)
        journal.record_heatmap(jc, date=date, slot=slot_label, symbol=sym,
                               path=str(path), ts=now.isoformat())
        got += 1
    if got:
        log.info("Heatmaps %s : %d image(s) (%s)", slot_label, got, date)


def _poll_embed(now) -> discord.Embed:
    e = discord.Embed(
        title=f"🗳️ Sondage de séance — {now.strftime('%d/%m/%Y')}",
        description="Un clic par question. Dépouillé demain à 12h — vos réponses "
                    "nourrissent la base d'étude.",
        color=0x3498DB)
    for titre, opts in POLL_QUESTIONS:
        e.add_field(name=titre,
                    value="\n".join(f"{emoji} {label}" for emoji, _, label in opts),
                    inline=False)
    return e


async def _post_poll(now) -> None:
    channel = bot.get_channel(CHANNEL_ID)
    jc = _journal()
    if channel is None or jc is None:
        return
    msg = await channel.send(embed=_poll_embed(now))
    for emoji in POLL_EMOJIS:                      # amorce les réactions (un clic pour voter)
        try:
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            pass
    tally_due = dt.datetime.combine((now + dt.timedelta(days=1)).date(), dt.time(12, 0), PARIS)
    journal.poll_open(jc, date=now.date().isoformat(), message_id=str(msg.id),
                      posted_ts=now.isoformat(), tally_due_ts=tally_due.isoformat())
    log.info("Sondage posté (%s), dépouillement prévu %s", now.date().isoformat(), tally_due)


async def _tally_due_polls(now) -> None:
    jc = _journal()
    if jc is None:
        return
    for row in journal.polls_a_depouiller(jc, now.isoformat()):
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(row["message_id"]))
        except (discord.NotFound, discord.HTTPException):
            log.warning("Sondage %s introuvable au dépouillement", row["message_id"])
            continue
        counts = {}
        for reaction in msg.reactions:
            col = POLL_EMOJIS.get(str(reaction.emoji))
            if col:
                counts[col] = max(0, reaction.count - 1)   # -1 : l'amorce du bot
        journal.poll_tally(jc, date=row["date"], counts=counts, tallied_ts=now.isoformat())
        log.info("Sondage %s dépouillé : %s", row["date"], counts)
        _build_daily_metrics(row["date"])


def _poll_ratio(jc, date, name, a, b) -> None:
    a, b = a or 0, b or 0
    if a + b > 0:
        journal.set_metric(jc, date=date, name=name, value_num=round(a / (a + b), 3))


def _build_daily_metrics(date: str) -> None:
    """Table de features (1 ligne logique/jour, format long) : agrégats de
    régime + vérité-marché + ratios du sondage. Recalculable à volonté."""
    jc = _journal()
    if jc is None:
        return
    tl = journal.regime_timeline(jc, date)
    fin = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(22, 0), PARIS).isoformat()
    for col, val in journal.minutes_par_couleur(tl, fin).items():
        journal.set_metric(jc, date=date, name=f"minutes_{col}", value_num=round(val, 1))
    journal.set_metric(jc, date=date, name="n_changes",
                       value_num=sum(1 for e in tl if e["kind"] == "change"))
    opens = [e for e in tl if e["kind"] == "open"]
    if opens:
        journal.set_metric(jc, date=date, name="open_regime", value_txt=opens[0]["color"])
        journal.set_metric(jc, date=date, name="open_confidence", value_txt=opens[0]["confidence"])
    if tl:
        journal.set_metric(jc, date=date, name="close_regime", value_txt=tl[-1]["color"])
    # Vérité-marché objective (le dashboard calcule, on stocke).
    for sym in CONTEXT_SYMBOLS:
        c = fetch(f"/api/v1/{sym}/session_context?date={date}")
        if not c or not c.get("available"):
            continue
        journal.upsert_market_context(jc, date=date, symbol=sym, ctx=c)
        for k in ("range", "gap", "max_up", "max_down", "close_location",
                  "n_reversals", "prev_atr"):
            if c.get(k) is not None:
                journal.set_metric(jc, date=date, symbol=sym, name=k, value_num=float(c[k]))
    # Sondage : ratios dérivés (les votes bruts restent en table polls).
    p = jc.execute("SELECT * FROM polls WHERE date=?", (date,)).fetchone()
    if p:
        _poll_ratio(jc, date, "poll_directionnel_ratio", p["q1_directionnel"], p["q1_retracement"])
        _poll_ratio(jc, date, "poll_haussier_ratio", p["q2_haussier"], p["q2_baissier"])
        _poll_ratio(jc, date, "poll_phase_dir_ratio", p["q3_dir_oui"], p["q3_dir_non"])
        _poll_ratio(jc, date, "poll_representatif_ratio", p["q6_repr_high"],
                    (p["q6_repr_mid"] or 0) + (p["q6_repr_low"] or 0))
        buckets = {b: p[f"q5_b{b}"] for b in (1, 2, 3, 4) if p[f"q5_b{b}"]}
        if buckets:
            journal.set_metric(jc, date=date, name="poll_amplitude_bucket",
                               value_num=max(buckets, key=buckets.get))
        av, ap = p["q4_avant_1615"] or 0, p["q4_apres_1615"] or 0
        if av or ap:                      # heure de la phase directionnelle
            journal.set_metric(jc, date=date, name="poll_phase_heure",
                               value_txt="avant_1615" if av >= ap else "apres_1615")
    log.info("daily_metrics construites (%s)", date)


async def _journal_tick(now, d) -> None:
    """Collecte data : dépouillement (tous les jours), puis captures de séance
    (semaine seulement). Isolé de tout le reste et enveloppé en try/except par
    l'appelant : un pépin de collecte ne doit jamais perturber le bot."""
    jc = _journal()
    if jc is None:
        return
    await _tally_due_polls(now)          # peut tomber un week-end (sondage du vendredi)
    if now.weekday() >= 5:
        return
    date, slot = now.date().isoformat(), (now.hour, now.minute)
    if slot in HEATMAP_SLOTS:
        await _capture_heatmaps(date, HEATMAP_SLOTS[slot], now)
    if slot == (15, 30) and not journal.regime_open_done(jc, date):
        await _record_regime(now, "open", d)
    elif _en_session(now) and now.minute % 10 == 0:
        await _record_regime(now, "heartbeat", d, with_market=False)
    if slot == POLL_POST and not journal.poll_posted(jc, date):
        await _post_poll(now)


@tasks.loop(seconds=60)
async def tick() -> None:
    """Boucle minute : collecte (journal), posts aux heures fixes, et sur
    changement de régime."""
    global _last_signature, _last_digest
    now = dt.datetime.now(PARIS)
    d = fetch("/api/v1/digest")

    # Collecte pour le backtest — tourne AUSSI le week-end (pour dépouiller le
    # sondage du vendredi). Cloisonnée : jamais bloquante pour le reste.
    try:
        await _journal_tick(now, d)
    except Exception:  # noqa: BLE001
        log.exception("Collecte journal : échec (sans conséquence sur le bot)")

    # Posts Discord automatiques : silencieux le week-end, « muet » ne valant
    # que pour l'automatique — les commandes à la demande restent actives.
    if now.weekday() >= 5 or d is None:
        return
    signature = tuple(tuple(x) for x in d.get("signature", []))

    slot = (now.hour, now.minute)
    jour = now.date().isoformat()
    deja = _posted.setdefault(jour, set())
    if slot == CLOSE_POST and slot not in deja:      # message de clôture (16h)
        deja.add(slot)
        await _post_close(d)
        log.info("Message de clôture posté (%02dh%02d)", slot[0], slot[1])
        _last_signature, _last_digest = signature, d
        return
    if slot in SCHEDULE and slot not in deja:
        deja.add(slot)
        await _post(d)
        log.info("Post fixe %02dh%02d (%s)", slot[0], slot[1], d["color"])
        _last_signature, _last_digest = signature, d
        return

    # Changement de régime : uniquement en session, et seulement après un
    # premier relevé (sinon le tout premier tick posterait sans raison).
    if _en_session(now) and _last_signature is not None and signature != _last_signature:
        await _post(d)
        log.info("Changement de régime détecté -> post (%s)", d["color"])
        reason = journal.compute_reason(_prev_for_reason(), d.get("color"),
                                        d.get("confidence"), d.get("families"))
        await _record_regime(now, "change", d, reason=reason)
    _last_signature, _last_digest = signature, d


@bot.command(name="etat")
async def etat(ctx: commands.Context) -> None:
    """`!etat` — le digest complet, à la demande."""
    d = fetch("/api/v1/digest")
    if d is None:
        await ctx.send("Dashboard injoignable pour l'instant.")
        return
    await ctx.send(embed=_embed(d))


@bot.command(name="vix")
async def vix_cmd(ctx: commands.Context) -> None:
    """`!vix` — la volatilité (VIX) et sa position vs le seuil du digest."""
    d = fetch("/api/v1/vix")
    if not d or not d.get("available"):
        await ctx.send("VIX indisponible pour l'instant.")
        return
    seuil = d.get("seuil", 0)
    g = d.get("grade") or {}
    grade = f"{g.get('emoji', '')} {g.get('label', '?')}".strip()
    pos = "au-dessus" if d.get("above") else "sous"
    await ctx.send(f"**VIX {d['vix']:.2f}** — {grade} · {pos} le seuil ({seuil:.0f}).")


@bot.command(name="gamma")
async def gamma(ctx: commands.Context, symbole: str | None = None) -> None:
    """`!gamma` (digest) ou `!gamma NQ` (valeurs calculées d'un symbole)."""
    if symbole is None:
        await etat(ctx)
        return
    s = fetch(f"/api/v1/{symbole.upper()}/summary")
    if s is None:
        await ctx.send(f"Pas de données pour {symbole.upper()} (pull pas encore fait ?).")
        return
    zg = f"{s['zero_gamma']:.0f}" if s.get("zero_gamma") is not None else "n/a"
    await ctx.send(
        f"**{s['symbol']}** — GEX net {s['net_gex'] / 1e9:+.2f} Bn · "
        f"DEX net {s['net_dex'] / 1e9:+.2f} Bn · Zero Gamma {zg} "
        f"(source {s['source']})"
    )


def _fmt(v, nd=0):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


@bot.command(name="niveaux", aliases=["levels"])
async def niveaux(ctx: commands.Context, symbole: str | None = None,
                  echelle: str | None = None) -> None:
    """`!niveaux NQ` — niveaux GEX en texte. `!niveaux NDX NQ` — niveaux NDX
    TRANSPOSÉS en prix NQ (comme le sélecteur d'échelle du dashboard)."""
    if not symbole:
        await ctx.send("Usage : `!niveaux SYMBOLE [ÉCHELLE]` "
                       "(ex. `!niveaux NQ`, ou `!niveaux NDX NQ` pour transposer).")
        return
    sym = symbole.upper()
    path = f"/api/v1/{sym}/levels"
    if echelle:
        path += f"?scale={echelle.upper()}"
    d = fetch(path)
    if d is None:
        await ctx.send(f"Pas de niveaux pour {sym} (pull pas encore fait ?).")
        return
    k = d.get("key_levels", {})
    murs = " · ".join(
        f"{w['strike']:.0f} ({w['gex'] / 1e9:+.2f} Bn "
        f"{'call' if w['gex'] > 0 else 'put'})"
        for w in d.get("gex_walls", [])
    ) or "n/a"
    titre = f"**{sym}** — niveaux GEX"
    sc = d.get("scale")
    if sc and sc != sym:
        titre += f" (échelle {sc})"
    titre += f" · spot {_fmt(d.get('spot'))}"
    lignes = [
        titre,
        f"Gamma Flip {_fmt(d.get('zero_gamma'))} · HVL {_fmt(d.get('hvl'))}",
        f"Call Wall {_fmt(k.get('call_wall'))} · Put Support {_fmt(k.get('put_support'))}",
        f"1D min/max : {_fmt(k.get('d1_min'))} – {_fmt(k.get('d1_max'))}",
        f"Murs GEX : {murs}",
    ]
    await ctx.send("\n".join(lignes))


# Graphiques disponibles à la demande, en image. Nom de commande -> (nom du
# graphique côté API, légende affichée). N'importe quel graphe du dashboard
# peut sortir en PNG (cf. /api/v1/<sym>/chart/<name>.png).
CHARTS = {
    "heatmap": ("heatmap", "Heatmap — gamma par strike + parcours du prix"),
    "gex": ("gex", "Gamma Exposure par strike"),
    "delta": ("dex", "Delta Exposure par strike"),
    "dex": ("dex", "Delta Exposure par strike"),
    "flow": ("tape", "Order flow signé cumulé"),
    "skew": ("smile", "Skew IV par échéance"),
    "profile": ("profile", "Profil de GEX selon le spot"),
    "vanna": ("vanna", "Vanna Exposure par strike"),
    "charm": ("charm", "Charm Exposure par strike"),
    "history": ("history", "GEX net — historique"),
    "positionnement": ("oi", "Variation d'open interest vs veille"),
}


# Échéance : alias saisis -> bucket côté dashboard.
_BUCKETS = {"0dte": "0DTE", "semaine": "Semaine", "week": "Semaine",
            "mois": "Mois", "month": "Mois", "tout": "Tout", "all": "Tout"}
# Échelles d'affichage transposables (comme le sélecteur du dashboard).
_SCALES = {"SPX", "NDX", "SPY", "QQQ", "ES", "NQ"}

# Vannes tirées au sort quand une COMMANDE n'existe pas (`{cmd}` = son nom).
_UNKNOWN_QUIPS = (
    "🤷 `!{cmd}` ? Jamais entendu parler. Tape `!help` pour les vraies.",
    "🎲 `!{cmd}` : c'est pas dans mon deck. `!help` ?",
    "🧐 `!{cmd}`… tu inventes des commandes maintenant ? `!help`.",
    "🙃 `!{cmd}` n'existe pas, mais l'effort est noté. `!help`.",
    "📡 `!{cmd}` : aucune réponse de la station. Essaie `!help`.",
    "🤨 `!{cmd}` ? Tente `!help`, tu seras peut-être surpris.",
    "🛎️ `!{cmd}` : y'a personne à ce guichet. `!help`.",
    "🕵️ `!{cmd}` introuvable au dossier. `!help` pour la liste.",
)

# Vannes tirées au sort quand un mot n'est pas reconnu (`{toks}` = les tokens).
_IGNORE_QUIPS = (
    "🤨 {toks} ? C'est un grec que je connais pas, celui-là. Ignoré.",
    "🧐 {toks} : ni échéance, ni %, ni symbole. J'ai fait semblant de rien.",
    "🥸 J'ai zappé {toks} — pas dans mon vocabulaire d'options.",
    "🙈 {toks} m'a filé entre les doigts.",
    "🃏 {toks} ? Poliment ignoré, sans rancune.",
    "😎 {toks} n'a pas passé le contrôle qualité. Dehors.",
    "🫡 {toks} : ignoré avec le sourire.",
    "🧹 J'ai balayé {toks} sous le tapis (pas compris).",
    "🛸 {toks} vient d'une autre galaxie, je l'ai laissé repartir.",
    "🎣 {toks} ? Rien mordu, je relâche.",
)


def _parse_chart_opts(args: tuple[str, ...]) -> tuple[dict, str, list[str]]:
    """Analyse des options d'un graphe, dans N'IMPORTE QUEL ordre :
    échéance (0dte/semaine/mois/tout), concentration (2 / 4 / 10 %), échelle
    (SPX/NDX/… pour transposer). Renvoie (query params, suffixe de légende,
    tokens NON reconnus)."""
    params, bits, unknown = {}, [], []
    for tok in args:
        low = tok.lower()
        if low in _BUCKETS:
            params["bucket"] = _BUCKETS[low]
            bits.append(params["bucket"])
            continue
        num = low.replace("%", "").replace(",", ".")
        try:
            pct = float(num)
            params["window"] = pct / 100 if pct >= 1 else pct   # 2 -> 0.02
            bits.append(f"±{params['window'] * 100:g}%")
            continue
        except ValueError:
            pass
        if tok.upper() in _SCALES:
            params["scale"] = tok.upper()
            bits.append(f"échelle {params['scale']}")
        else:
            unknown.append(tok)
    return params, " · ".join(bits), unknown


async def _send_chart(ctx: commands.Context, symbole: str, chart: str, legende: str,
                      *args: str) -> None:
    """Récupère le PNG du dashboard et le poste en pièce jointe. Options
    optionnelles (échéance / concentration / échelle) dans n'importe quel ordre ;
    prévient si un mot n'est pas reconnu."""
    sym = symbole.upper()
    params, suffixe, unknown = _parse_chart_opts(args)
    if unknown:
        toks = ", ".join(f"`{u}`" for u in unknown)
        quip = random.choice(_IGNORE_QUIPS).format(toks=toks)
        await ctx.send(f"{quip}\n-# Valides : échéance (0dte/semaine/mois/tout) · "
                       f"concentration ±% · échelle (SPX/NDX/SPY/QQQ/ES/NQ).")
    try:
        r = requests.get(f"{DASHBOARD}/api/v1/{sym}/chart/{chart}.png",
                         params=params, timeout=45)
    except requests.RequestException:
        await ctx.send("Dashboard injoignable pour l'instant.")
        return
    if r.status_code != 200 or r.content[:4] != b"\x89PNG":
        await ctx.send(f"🃏 **{sym}** ? Soit c'est pas un symbole que je suis, soit "
                       f"le pull n'est pas encore fait. (Essaie SPX, NDX, NQ, ES, "
                       f"SPY, QQQ…)")
        return
    leg = f"{legende} — {suffixe}" if suffixe else legende
    fichier = discord.File(io.BytesIO(r.content), filename=f"{sym}_{chart}.png")
    await ctx.send(f"**{sym}** — {leg}", file=fichier)


@bot.command(name="graph")
async def graph(ctx: commands.Context, symbole: str | None = None,
                nom: str | None = None, *args: str) -> None:
    """`!graph NQ gex 0dte 2` — n'importe quel graphique ; échéance,
    concentration (±%) et échelle (ex. NQ) optionnelles, dans tout ordre."""
    if not symbole or not nom or nom.lower() not in CHARTS:
        dispo = ", ".join(sorted(CHARTS))
        await ctx.send(f"Usage : `!graph SYMBOLE NOM [ÉCHÉANCE] [±%] [ÉCHELLE]`. "
                       f"Graphiques : {dispo}.")
        return
    chart, legende = CHARTS[nom.lower()]
    await _send_chart(ctx, symbole, chart, legende, *args)


def _make_chart_command(cmd_name: str, chart: str, legende: str):
    @bot.command(name=cmd_name)
    async def _cmd(ctx: commands.Context, symbole: str | None = None, *args: str):
        if not symbole:
            await ctx.send(f"Usage : `!{cmd_name} SYMBOLE [ÉCHÉANCE] [±%] [ÉCHELLE]` "
                           f"(ex. `!{cmd_name} NQ 0dte 2`, ou `!{cmd_name} NDX NQ`).")
            return
        await _send_chart(ctx, symbole, chart, legende, *args)
    return _cmd


# Raccourcis directs : !heatmap NQ, !delta NQ, !flow NQ, !skew SPX, etc.
for _name, (_chart, _leg) in CHARTS.items():
    _make_chart_command(_name, _chart, _leg)


@bot.command(name="sondage")
async def sondage(ctx: commands.Context) -> None:
    """`!sondage` — poste le sondage de séance à la demande (sinon auto à 23h05).

    Utile pour tester, ou relancer manuellement. Si un sondage a déjà été posté
    aujourd'hui, le dépouillement restera sur le premier (anti-doublon)."""
    await _post_poll(dt.datetime.now(PARIS))


@bot.command(name="pin")
async def pin_cmd(ctx: commands.Context, symbole: str | None = None,
                  date: str | None = None) -> None:
    """`!pin QQQ` (ou `!pin SPX 2026-08-01`) — pinning de clôture : le prix
    s'est-il collé sur un strike / un mur GEX à 22h ? Pertinent après la clôture."""
    if not symbole:
        await ctx.send("Usage : `!pin QQQ` (ou `!pin SPX 2026-08-01`). "
                       "Le plus parlant après 22h.")
        return
    sym = symbole.upper()
    path = f"/api/v1/{sym}/close_context" + (f"?date={date}" if date else "")
    d = fetch(path)
    if not d or not d.get("available"):
        raison = (d or {}).get("reason", "dashboard injoignable")
        await ctx.send(f"Pinning indisponible pour {sym} ({raison}).")
        return
    pr = d.get("pin_ratio")
    jauge = ("collé au strike" if pr is not None and pr < 0.15 else
             "proche d'un strike" if pr is not None and pr < 0.4 else
             "entre deux strikes")
    lignes = [
        f"**{sym}** — pinning de clôture ({d.get('date')})",
        f"Clôture {d.get('close')} · strike le plus proche "
        f"{d.get('nearest_strike')} (écart {d.get('dist_nearest_strike')})",
        f"pin_ratio {pr} → **{jauge}**" if pr is not None else "pin_ratio n/a",
        f"Mur GEX1 {d.get('gex1_strike')} (écart {d.get('dist_gex1')}) · "
        f"GEX2 {d.get('gex2_strike')} (écart {d.get('dist_gex2')})",
    ]
    cr = d.get("strike_crossings_preclose")
    if cr is not None:
        lignes.append(f"Franchissements de strike (15h50-16h) : {cr}")
    await ctx.send("\n".join(lignes))


@bot.command(name="tick")
async def tick_cmd(ctx: commands.Context, symbole: str | None = None,
                   date: str | None = None) -> None:
    """`!tick NQ` (ou `!tick ES 2026-08-01`) — fenêtre de clôture au tick
    (21h45-22h05) : range avant/après 22h, expansion, excursions."""
    if not symbole:
        await ctx.send("Usage : `!tick NQ` (ou `!tick ES 2026-08-01`). "
                       "Capture 21h45-22h05, compte courtier requis.")
        return
    sym = symbole.upper()
    path = f"/api/v1/{sym}/tick_context" + (f"?date={date}" if date else "")
    d = fetch(path)
    if not d or not d.get("available"):
        raison = (d or {}).get("reason", "dashboard injoignable")
        await ctx.send(f"Ticks indisponibles pour {sym} ({raison}).")
        return
    lignes = [
        f"**{sym}** — fenêtre de clôture ({d.get('date')}) · {d.get('n_ticks')} ticks",
        f"Range {d.get('range')} (haut {d.get('high')} / bas {d.get('low')})",
    ]
    if d.get("post_range") is not None:
        exp = f" (×{d.get('post_expansion')})" if d.get("post_expansion") else ""
        lignes.append(f"Avant 22h : range {d.get('pre_range')} · après : "
                      f"range {d.get('post_range')}{exp}")
        lignes.append(f"Excursion post-clôture depuis {d.get('close_2200')} : "
                      f"+{d.get('post_max_up')} / −{d.get('post_max_down')}")
    await ctx.send("\n".join(lignes))


@bot.command(name="setup")
async def setup_cmd(ctx: commands.Context, *, valeur: str | None = None) -> None:
    """`!setup MOC A` (ou `!setup NONE`) — tag le setup MOC du jour.

    Info métier, pour étudier « quand MA stratégie marche » et pas seulement le
    marché. Unique par jour, corrigible (ré-écrire remplace)."""
    jc = _journal()
    if jc is None:
        await ctx.send("Journal indisponible.")
        return
    today = dt.datetime.now(PARIS).date().isoformat()
    if not valeur:
        actuel = journal.get_setup(jc, today)
        await ctx.send(f"Usage : `!setup MOC A` ou `!setup NONE`. "
                       f"Setup du jour : **{actuel or '—'}**.")
        return
    now = dt.datetime.now(PARIS)
    v = valeur.strip().upper()
    journal.set_setup(jc, date=today, value=v, ts=now.isoformat())
    await ctx.send(f"✅ Setup du {now:%d/%m} enregistré : **{v}**.")


def _split_linked_date(texte: str) -> tuple[str | None, str]:
    """Détache une éventuelle date AAAA-MM-JJ en tête (séance concernée)."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s+(.*)", texte, re.DOTALL)
    return (m.group(1), m.group(2).strip()) if m else (None, texte.strip())


async def _add_log(ctx, texte: str | None, type_: str) -> None:
    """Cœur commun de `!note` et `!hypo` : liste si vide, sinon consigne."""
    jc = _journal()
    if jc is None:
        await ctx.send("Journal indisponible.")
        return
    if not texte:
        rows = journal.list_entries(jc)[:10]
        if not rows:
            await ctx.send("Journal vide. `!note <texte>` ou `!hypo <hypothèse>` "
                           "pour commencer.")
            return
        lignes = [f"#{r['id']} [{r['type']}/{r['status']}] {r['text']} "
                  f"— *{r['author']}*" for r in rows]
        await ctx.send("**Journal de recherche :**\n" + "\n".join(lignes))
        return
    linked, texte = _split_linked_date(texte)
    now = dt.datetime.now(PARIS)
    nid = journal.add_entry(jc, text=texte, created=now.isoformat(), type=type_,
                            author=ctx.author.display_name,
                            linked_date=linked or now.date().isoformat())
    quand = f" (séance {linked})" if linked else ""
    await ctx.send(f"📝 #{nid} [{type_}]{quand} : « {texte} » — *{ctx.author.display_name}*")


@bot.command(name="note")
async def note_cmd(ctx: commands.Context, *, texte: str | None = None) -> None:
    """`!note [AAAA-MM-JJ] <texte>` — consigne une observation (mémoire du labo).
    Sans argument : liste les 10 dernières entrées. L'auteur Discord est capté."""
    await _add_log(ctx, texte, "observation")


@bot.command(name="hypo")
async def hypo_cmd(ctx: commands.Context, *, texte: str | None = None) -> None:
    """`!hypo [AAAA-MM-JJ] <hypothèse>` — consigne une hypothèse à tester
    (statut pending)."""
    await _add_log(ctx, texte, "hypothesis")


@bot.command(name="help", aliases=["aide", "commandes"])
async def aide(ctx: commands.Context) -> None:
    """`!help` — la liste des commandes, regroupées par thème."""
    graphes = ", ".join(f"`!{n}`" for n in sorted(CHARTS))
    e = discord.Embed(
        title="Commandes du bot — état du gamma",
        description="Le bot relaie le **verdict** calculé par le dashboard GEX "
                    "(analyses dérivées, jamais la donnée brute).",
        color=0x3498DB,
    )
    e.add_field(
        name="📊 État & verdict",
        value=("`!etat` — le digest complet (état par symbole, verdict "
               "couleur, confiance).\n"
               "`!gamma` — idem `!etat`.\n"
               "`!gamma NQ` — les valeurs calculées d'un symbole (GEX net, "
               "DEX net, Zero Gamma).\n"
               "`!vix` — la volatilité (VIX), son régime (calme→panique) et sa "
               "position vs le seuil."),
        inline=False,
    )
    e.add_field(
        name="🎯 Niveaux",
        value=("`!niveaux NQ` (ou `!levels NQ`) — Gamma Flip, HVL, Call Wall, "
               "Put Support, 1D min/max, murs GEX.\n"
               "`!niveaux NDX NQ` — niveaux NDX **transposés en prix NQ**."),
        inline=False,
    )
    e.add_field(
        name="🖼️ Graphiques (image)",
        value=(f"`!graph NQ heatmap` — n'importe quel graphique du dashboard.\n"
               f"Raccourcis directs : {graphes}.\n"
               f"Options (tout ordre) : **échéance** (0dte/semaine/mois/tout), "
               f"**concentration** ±%, **échelle** (SPX/NDX/…) — ex. "
               f"`!gex NQ 0dte 2`, `!gex NDX NQ` (NDX en prix NQ)."),
        inline=False,
    )
    e.add_field(
        name="🗳️ Sondage de séance",
        value=("Chaque soir (23h05), un sondage à réactions sur la journée "
               "(directionnelle ? ampleur ? régime représentatif ?). Vos votes "
               "alimentent une base d'étude. `!sondage` le relance à la demande."),
        inline=False,
    )
    e.add_field(
        name="🔬 Recherche (journal du labo)",
        value=("`!pin QQQ` — pinning de clôture (collé sur un strike ? mur GEX ?).\n"
               "`!tick NQ` — fenêtre de clôture au tick (range avant/après 22h).\n"
               "`!setup MOC A` (ou `!setup NONE`) — tag ton setup MOC du jour.\n"
               "`!hypo <hypothèse>` — consigne une hypothèse à tester.\n"
               "`!note <observation>` — une observation. `!note` seul liste les "
               "dernières. Préfixe optionnel `AAAA-MM-JJ` pour viser une séance "
               "passée ; l'auteur est capté automatiquement."),
        inline=False,
    )
    e.add_field(
        name="ℹ️ Comment se lit le verdict",
        value=("Le régime est jugé par **famille** indépendante — **S&P** "
               "(SPX/SPY/ES) et **Nasdaq** (NDX/QQQ/NQ) — et non symbole par "
               "symbole. 🔴 2 familles négatives ou une en fort négatif · "
               "🟠 1 famille négative ou VIX élevé · 🟢 sinon. La **confiance** "
               "(forte/moyenne/faible) reflète la couverture des données."),
        inline=False,
    )
    e.set_footer(text="Posté automatiquement à 8h30 / 15h25 / 15h35 (Paris), "
                      "message de clôture à 16h, et à chaque changement de régime. "
                      "Silencieux le week-end.")
    await ctx.send(embed=e)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    """Commande inconnue → petite vanne (au lieu du silence). Les vraies erreurs
    sont journalisées, pas avalées."""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(random.choice(_UNKNOWN_QUIPS).format(cmd=ctx.invoked_with))
        return
    log.error("Erreur commande %s : %s", ctx.command, error, exc_info=error)


@bot.event
async def on_ready() -> None:
    log.info("Bot connecté : %s (salon cible %s)", bot.user, CHANNEL_ID)
    _journal()                    # ouvre (et crée au besoin) la base de recherche
    if not tick.is_running():
        tick.start()


def main() -> None:
    if not TOKEN or not CHANNEL_ID:
        raise SystemExit(
            "DISCORD_BOT_TOKEN et DISCORD_CHANNEL_ID doivent être définis "
            "(cf. .env.example et le README)."
        )
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
