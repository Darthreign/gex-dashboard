"""Bot Discord — diffuse l'état du gamma calculé par le dashboard GEX.

Composant volontairement SÉPARÉ et léger : il ne fait aucun calcul et ne voit
jamais la donnée brute. Il interroge l'API locale du dashboard
(`/api/v1/digest`, `/api/v1/<sym>/summary`) — qui ne renvoie que des analyses
dérivées — et les relaie dans un salon Discord. On peut donc le partager avec
des amis sans qu'ils aient de compte courtier ni accès aux chaînes dxFeed.

Ce qu'il fait :
- poste l'état du gamma à heures fixes (8h30 / 15h25 / 17h30 Paris) ;
- poste aussi à chaque CHANGEMENT DE RÉGIME pendant la session US (un symbole
  qui bascule Gamma +/− ou Delta +/−) ;
- répond à `!gamma [SYMBOLE]` (valeurs calculées) et `!etat` (digest complet).

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
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import requests
from discord.ext import commands, tasks

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
SCHEDULE = {(8, 30), (15, 25), (17, 30)}
# Session US en heure de Paris (15h30 = 9h30 ET open ; ~22h = 16h ET close).
SESSION_START, SESSION_END = dt.time(15, 30), dt.time(22, 0)

_last_signature: tuple | None = None
_posted: dict[str, set] = {}          # jour ISO -> {(h, min) déjà postés}


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
bot = commands.Bot(command_prefix="!", intents=intents)


async def _post(d: dict) -> None:
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.warning("Salon %s introuvable — le bot est-il invité et l'ID correct ?",
                    CHANNEL_ID)
        return
    await channel.send(embed=_embed(d))


def _en_session(now: dt.datetime) -> bool:
    return now.weekday() < 5 and SESSION_START <= now.timetz().replace(tzinfo=None) <= SESSION_END


@tasks.loop(seconds=60)
async def tick() -> None:
    """Boucle minute : poste aux heures fixes, et sur changement de régime."""
    global _last_signature
    now = dt.datetime.now(PARIS)
    d = fetch("/api/v1/digest")
    if d is None:
        return
    signature = tuple(tuple(x) for x in d.get("signature", []))

    slot = (now.hour, now.minute)
    jour = now.date().isoformat()
    deja = _posted.setdefault(jour, set())
    if slot in SCHEDULE and slot not in deja:
        deja.add(slot)
        await _post(d)
        log.info("Post fixe %02dh%02d (%s)", slot[0], slot[1], d["color"])
        _last_signature = signature
        return

    # Changement de régime : uniquement en session, et seulement après un
    # premier relevé (sinon le tout premier tick posterait sans raison).
    if _en_session(now) and _last_signature is not None and signature != _last_signature:
        await _post(d)
        log.info("Changement de régime détecté -> post (%s)", d["color"])
    _last_signature = signature


@bot.command(name="etat")
async def etat(ctx: commands.Context) -> None:
    """`!etat` — le digest complet, à la demande."""
    d = fetch("/api/v1/digest")
    if d is None:
        await ctx.send("Dashboard injoignable pour l'instant.")
        return
    await ctx.send(embed=_embed(d))


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


async def _send_chart(ctx: commands.Context, symbole: str, chart: str, legende: str) -> None:
    """Récupère le PNG du dashboard et le poste en pièce jointe."""
    sym = symbole.upper()
    try:
        r = requests.get(f"{DASHBOARD}/api/v1/{sym}/chart/{chart}.png", timeout=45)
    except requests.RequestException:
        await ctx.send("Dashboard injoignable pour l'instant.")
        return
    if r.status_code != 200 or r.content[:4] != b"\x89PNG":
        await ctx.send(f"Graphique indisponible pour {sym} (pull pas encore fait ?).")
        return
    fichier = discord.File(io.BytesIO(r.content), filename=f"{sym}_{chart}.png")
    await ctx.send(f"**{sym}** — {legende}", file=fichier)


@bot.command(name="graph")
async def graph(ctx: commands.Context, symbole: str | None = None,
                nom: str | None = None) -> None:
    """`!graph NQ heatmap` — n'importe quel graphique en image."""
    if not symbole or not nom or nom.lower() not in CHARTS:
        dispo = ", ".join(sorted(CHARTS))
        await ctx.send(f"Usage : `!graph SYMBOLE NOM`. Graphiques : {dispo}.")
        return
    chart, legende = CHARTS[nom.lower()]
    await _send_chart(ctx, symbole, chart, legende)


def _make_chart_command(cmd_name: str, chart: str, legende: str):
    @bot.command(name=cmd_name)
    async def _cmd(ctx: commands.Context, symbole: str | None = None):
        if not symbole:
            await ctx.send(f"Usage : `!{cmd_name} SYMBOLE` (ex. `!{cmd_name} NQ`).")
            return
        await _send_chart(ctx, symbole, chart, legende)
    return _cmd


# Raccourcis directs : !heatmap NQ, !delta NQ, !flow NQ, !skew SPX, etc.
for _name, (_chart, _leg) in CHARTS.items():
    _make_chart_command(_name, _chart, _leg)


@bot.event
async def on_ready() -> None:
    log.info("Bot connecté : %s (salon cible %s)", bot.user, CHANNEL_ID)
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
