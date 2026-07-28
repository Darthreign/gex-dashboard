# Onglet 3 — Vanna & Charm

*[← Retour au sommaire](README.md)*

Cet onglet va un cran plus loin que GEX/DEX : il montre deux effets plus subtils, souvent négligés, mais réels.

![Vanna & Charm](../screenshots/onglet-vanna-charm.png)

## Vanna Exposure

La **Vanna** répond à la question : *"si la volatilité implicite (l'incertitude perçue par le marché) bouge d'un point, de combien le delta des dealers change-t-il ?"*

Concrètement : la volatilité peut monter (panique, incertitude) ou descendre (calme) sans même que le prix bouge. Quand elle change, les dealers doivent re-couvrir leurs positions — c'est ce re-hedging que la Vanna mesure, par strike, en $M par point de volatilité.

## Charm Exposure

La **Charm** mesure un effet purement **mécanique du temps qui passe** : à mesure qu'une option se rapproche de son échéance, son delta change tout seul, même si le prix et la volatilité ne bougent pas d'un centime. C'est le moteur classique des dérives de fin de séance ("*pinning*" ou glissades en fin de journée) — exprimée en $M de delta par jour écoulé.

## Comment lire les deux graphiques

Même logique que les graphiques GEX/DEX de la Vue principale : une barre par strike, bleu si positif, rouge si négatif, avec le spot actuel indiqué par une ligne pointillée. Les deux tuiles au-dessus des graphiques donnent le total net (Vanna Exposure Nette, Charm Exposure Nette).

⚠️ Ce sont des effets réels mais **secondaires** par rapport au GEX/DEX — utiles pour affiner une lecture, pas pour la remplacer.

---

*[← Retour au sommaire](README.md) · [← Gamma Profile](2-gamma-profile.md) · [Onglet suivant : Heatmap →](4-heatmap.md)*
