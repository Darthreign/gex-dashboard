/* Replay de la heatmap (page principale) sur la séance du jour.
 *
 * Le serveur renvoie en une fois (/api/v1/heatmap-replay) la heatmap actuelle
 * (bougies de la séance) et une image par minute des niveaux et du profil de
 * gamma tels qu'ils étaient affichés (cf. gex/replay.py). Ici on ne fait que
 * les rejouer : à l'instant t, bougies jusqu'à t, niveaux et profil de la
 * dernière image <= t. Pendant le replay, le direct est suspendu sur ce
 * graphique (GexLW.hold) ; « Direct » le rend.
 *
 * Commandes (ids du layout, pane-heat) : heat-replay-play (lecture / pause),
 * heat-replay-speed (vitesse), heat-replay-pos (curseur 0-1000),
 * heat-replay-time (heure affichée), heat-replay-live (retour au direct).
 */
(function () {
  "use strict";
  const CHART = "heatmap";
  const TICK_MS = 200;
  const st = { data: null, t: 0, t0: 0, t1: 0, playing: false, timer: null,
               args: null, ctx: null, loading: false };

  function $(id) { return document.getElementById(id); }
  function label(txt) { const e = $("heat-replay-time"); if (e) e.textContent = txt; }
  function fr() { return !st.ctx || (st.ctx.lang || "fr") === "fr"; }
  function setPlayText() {
    const b = $("heat-replay-play");
    if (b) b.textContent = st.playing ? "⏸ Pause" : (st.data ? "▶ " + (fr() ? "Lecture" : "Play")
                                                             : "▶ Replay");
  }
  function speed() { const s = $("heat-replay-speed"); return Number(s && s.value) || 60; }

  function frameAt(t) {
    const f = st.data.frames;
    let lo = 0, hi = f.length - 1, best = 0;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (f[mid].t <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return f[best];
  }

  function show(t) {
    if (!st.data || !window.GexLW) return;
    st.t = Math.max(st.t0, Math.min(t, st.t1));
    const base = st.data.base;
    const fr0 = frameAt(st.t);
    const spec = Object.assign({}, base, {
      _replay: true, key: "replay-" + st.data.session,
      series: (base.series || []).map((s) => Object.assign({}, s, {
        data: (s.data || []).filter((d) => d.time <= st.t) })),
      lines: fr0.lines, profile: fr0.profile,
    });
    window.GexLW.render(CHART, spec);
    const d = new Date(st.t * 1000);
    label("Replay " + d.toLocaleDateString([], { day: "2-digit", month: "2-digit" })
          + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    const pos = $("heat-replay-pos");
    if (pos && st.t1 > st.t0 && document.activeElement !== pos) {
      pos.value = String(Math.round(1000 * (st.t - st.t0) / (st.t1 - st.t0)));
    }
  }

  function pause() {
    st.playing = false;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    setPlayText();
  }

  function play() {
    if (!st.data) return;
    if (st.t >= st.t1) st.t = st.t0;          // relancé depuis la fin : on repart du début
    st.playing = true;
    setPlayText();
    if (st.timer) clearInterval(st.timer);
    st.timer = setInterval(() => {
      const next = st.t + speed() * TICK_MS / 1000;
      show(next);
      if (next >= st.t1) pause();
    }, TICK_MS);
  }

  function stop() {
    pause();
    st.data = null; st.args = null;
    if (window.GexLW) window.GexLW.hold(CHART, false);
    label("");
    setPlayText();
  }

  function load() {
    if (!st.ctx || st.loading) return;
    st.loading = true;
    label(fr() ? "Chargement du replay…" : "Loading replay…");
    const q = new URLSearchParams(st.ctx).toString();
    const args = JSON.stringify(st.ctx);
    fetch("/api/v1/heatmap-replay?" + q).then((r) => r.json()).then((data) => {
      st.loading = false;
      if (!data || data.error || !data.frames || !data.frames.length) {
        label((data && (data.message || data.error)) || "—");
        return;
      }
      st.data = data; st.args = args;
      st.t0 = data.frames[0].t;
      st.t1 = data.frames[data.frames.length - 1].t;      // dernière image enregistrée
      window.GexLW.hold(CHART, true);
      show(st.t0);
      play();
    }).catch(() => { st.loading = false; label(fr() ? "Replay indisponible" : "Replay unavailable"); });
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("#heat-replay-play")) {
      if (!st.data) load(); else if (st.playing) pause(); else play();
    } else if (e.target.closest("#heat-replay-live")) {
      stop();
    }
  });
  document.addEventListener("input", (e) => {
    if (e.target && e.target.id === "heat-replay-pos" && st.data) {
      show(st.t0 + (st.t1 - st.t0) * Number(e.target.value) / 1000);
    }
  });

  // Réglages courants de la heatmap (symbole, échelle, niveaux…), transmis
  // par le callback « lw-streams » : un changement arrête le replay, qui
  // rejouerait sinon l'ancien contexte.
  function context(args) {
    st.ctx = args;
    if (st.data && st.args !== JSON.stringify(args)) stop();
    setPlayText();
  }

  window.GexReplay = { context, stop, _state: st };
})();
