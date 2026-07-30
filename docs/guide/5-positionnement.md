# Onglet 5 — Positionnement

*[← Retour au sommaire](README.md)*

Le dernier onglet répond à une question que les autres ne posent pas : *"qu'est-ce qui a changé depuis hier ?"*

![Positionnement](../screenshots/onglet-positionnement.png)

## Pourquoi un onglet à part

Tous les autres graphiques du dashboard utilisent l'*open interest* (les positions ouvertes) tel qu'il est **aujourd'hui**. Mais l'open interest n'est publié qu'**une fois par jour**, le matin. Cet onglet compare deux photos — celle d'hier et celle d'aujourd'hui — pour isoler ce qui a été **réellement ouvert ou fermé** entre les deux, plutôt que le gamma résiduel de positions installées depuis longtemps.

<a id="positionnement"></a>
## Le graphique

Une barre par strike, séparée en deux couleurs :

- **Bleu** : variation des positions sur les **calls**.
- **Orange** : variation des positions sur les **puts**.

L'axe horizontal indique le nombre de contrats en plus (ouverture) ou en moins (fermeture) par rapport à la veille. La ligne pointillée marque le spot actuel, pour voir tout de suite si l'activité récente se concentre plutôt au-dessus ou en dessous du prix.

## Ce que ça révèle

Un strike avec beaucoup de gamma mais **aucune variation ici** est un niveau ancien, installé depuis un moment. Un strike avec une grosse variation, même si son gamma total reste modeste, est un niveau **qui vient de se former** — potentiellement plus révélateur de ce que le marché anticipe *maintenant*.

⚠️ Cet onglet reste vide ou peu fourni les premiers jours d'utilisation du dashboard : il lui faut au moins deux séances de collecte pour avoir quelque chose à comparer.

---

*[← Retour au sommaire](README.md) · [← Heatmap](4-heatmap.md)*
