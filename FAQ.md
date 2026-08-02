# FAQ

*[English version](FAQ.en.md)*

Tout ce qu'il faut savoir pour comprendre les données affichées et faire
tourner sa propre instance.

---

## Les données

### D'où viennent les données ?

De l'endpoint public **delayed de CBOE**, l'opérateur des marchés d'options
américains. C'est la source officielle des chaînes SPX et NDX : prix bid/ask,
volatilité implicite, open interest et volume, pour chaque strike et chaque
échéance.

**Aucun compte, aucune clé, aucun abonnement.** Le dashboard interroge
directement l'endpoint public, gratuitement.

### Pourquoi un délai de 15 minutes ?

C'est le délai de la source gratuite et publique de CBOE. *Rediffuser* du
temps réel exigerait une licence professionnelle coûteuse — mais pour un usage
personnel, un compte courtier suffit (voir
[Temps réel via un compte courtier](#temps-réel-via-un-compte-courtier-gratuit-avec-le-compte)).

**En pratique, ça compte beaucoup moins qu'on l'imagine** : la métrique
centrale de tout l'outil — l'open interest — n'est publiée **qu'une fois par
jour**, le matin, par l'OCC. Les murs de gamma, le Gamma Flip et les niveaux
clés reposent dessus et bougent donc très peu en séance. Le délai n'affecte
réellement que le prix spot de référence et le flux intraday.

### À quelle fréquence les données se rafraîchissent-elles ?

Le feed CBOE est régénéré environ toutes les **60 secondes**, et le dashboard
le sollicite au même rythme pendant les heures de marché (9h30–16h15 heure de
New York). En dehors, il se met en veille et affiche le dernier état connu.

### Pourquoi l'onglet « Positionnement » est-il souvent vide ?

Parce qu'il compare l'open interest d'une séance à l'autre, et que l'OI n'est
publié qu'une fois par jour. Tant que la publication du matin n'a pas eu lieu,
la comparaison n'a rien à montrer. Cet onglet devient exploitable après
quelques jours de collecte.

### Les niveaux sont-ils en points d'indice ou en futures ?

Les deux, au choix. Le sélecteur **Indice / ES** (ou NQ) bascule l'affichage.

C'est important : l'écart entre l'indice et son future n'est pas négligeable
(de l'ordre de +30 points sur ES, +150 sur NQ). Reporter un niveau SPX brut sur
un chart ES fausserait tout. Le basis est recalculé à chaque rafraîchissement à
partir de la parité call-put, et suit automatiquement le roll trimestriel.

---

## Faire tourner sa propre instance

### Pourquoi ne puis-je pas simplement utiliser ton dashboard ?

Deux raisons, et la première est la plus simple : **tu n'en as pas besoin**. La
source CBOE est gratuite et sans compte — ton instance affichera exactement les
mêmes données.

La seconde tient à la licence. Si une instance est enrichie de données
optionnelles (Databento, ou un flux courtier temps réel), celles-ci sont sous
licence *usage personnel, non redistribuable*. Les partager reviendrait à les
rediffuser, ce qui est interdit et ferait basculer l'exploitant dans la
catégorie « professionnel », avec les tarifs correspondants.

D'où le principe : **le code est partagé, pas les données.**

### Comment j'installe ?

Il faut Python 3.11 ou plus récent, et Git.

```
git clone https://github.com/Darthreign/gex-dashboard.git
cd gex-dashboard
python -m venv .venv
```

Puis, selon le système :

```
.venv\Scripts\pip install -r requirements.txt      # Windows
.venv/bin/pip install -r requirements.txt          # macOS / Linux
```

### Comment je lance ?

```
.venv\Scripts\python run.py       # Windows
.venv/bin/python run.py           # macOS / Linux
```

Puis ouvrir **http://127.0.0.1:8050** dans un navigateur.

Aucune configuration n'est nécessaire : le dashboard commence à collecter
immédiatement. L'interface est en français ou en anglais, détectée depuis la
langue du navigateur et modifiable par le sélecteur FR/EN.

### Dois-je le laisser tourner en permanence ?

Non — mais avec une nuance.

Les **niveaux** (GEX, murs, Gamma Flip, HVL) sont des photos de l'état actuel :
ils se reconstruisent intégralement au premier rafraîchissement, quel que soit
le temps d'arrêt. Rien à rattraper.

Le **flux delta intraday**, lui, se mesure entre deux relevés successifs : il ne
peut être capté que si le programme tourne pendant la séance. Idem pour
l'historique des niveaux, qui s'accumule au fil du temps.

En pratique : lance-le avant l'ouverture des marchés US les jours où tu
travailles. Hors séance, il se met en veille et ne consomme rien.

### Mes données restent-elles chez moi ?

Oui, entièrement. Tout est stocké en local dans le dossier `data/` (format
Parquet). Rien n'est envoyé nulle part — le dashboard n'écoute que sur
`127.0.0.1`, c'est-à-dire ta propre machine.

---

## Partager le verdict — le bot Discord

### Puis-je partager mes analyses avec des amis sans leur donner accès aux données ?

Oui, c'est exactement le rôle du **bot Discord** livré dans `discord_bot/`. Il
relaie dans un salon le **verdict** d'état du gamma — la conclusion, pas la
donnée. Tes amis voient « Gamma négatif sur le Nasdaq, contrarien risqué »
**sans compte courtier ni accès aux chaînes d'options**.

Techniquement, le bot n'interroge que l'API locale du dashboard
(`/api/v1/digest`), qui ne renvoie que des **analyses dérivées** : signes,
verdict, couleur, et graphiques d'agrégats. Jamais le flux brut par contrat.
C'est ce qui rend le partage compatible avec un flux sous licence personnelle —
tu partages une conclusion que *tu* produis, pas une rediffusion.

### Comment le bot décide-t-il la couleur du verdict ?

Il ne compte pas les symboles à égalité. SPX, SPY et ES sont trois vues du même
S&P 500 ; NDX, QQQ et NQ du même Nasdaq — les compter séparément reviendrait à
compter trois fois le même sous-jacent. Le verdict raisonne donc par **famille
indépendante** :

- Chaque famille (**S&P** : SPX/SPY/ES — **Nasdaq** : NDX/QQQ/NQ) agrège
  l'intensité de ses symboles avec des poids : **indice cash > ETF > future**.
  Un future négatif ne renverse pas le signal de l'indice cash.
- L'indice cash (SPX, NDX) est l'**indice principal** : s'il passe en *fort*
  gamma négatif, toute sa famille l'est.
- Couleur : 🔴 **rouge** si les 2 familles sont négatives ou une en fort
  négatif · 🟠 **orange** si 1 famille négative ou VIX au-dessus du seuil ·
  🟢 **vert** sinon.

Le digest affiche aussi une **confiance** (forte / moyenne / faible) selon la
couverture des données — un verdict appuyé sur les 3 symboles concordants d'une
famille vaut mieux qu'un verdict sur un seul.

### Quelles commandes le bot comprend-il ?

`!help` (la liste), `!etat`/`!gamma` (le digest complet), `!gamma NQ` (les
valeurs calculées d'un symbole), `!niveaux NQ` (les niveaux GEX en texte, avec
transposition d'échelle : `!niveaux NDX NQ` sort les niveaux NDX en prix NQ), et
n'importe quel graphique en image (`!heatmap NQ`, `!delta SPX`, `!vanna SPX`…).
Il poste aussi tout seul à heures fixes et à chaque changement de régime en
séance, en restant silencieux le week-end. Mise en place :
[`discord_bot/README.md`](discord_bot/README.md).

---

## Comprendre les indicateurs

### GEX (Gamma Exposure)

Estimation du gamma que les teneurs de marché doivent couvrir, exprimée en
**dollars par mouvement de 1 %** de l'indice. Calculée strike par strike à
partir de l'open interest et du gamma Black-Scholes.

- **GEX net positif** → régime *stabilisant*. Les teneurs de marché vendent
  dans la hausse et achètent dans la baisse : la volatilité est amortie.
- **GEX net négatif** → régime *déstabilisant*. Ils font l'inverse, ce qui
  amplifie les mouvements.

### Gamma Flip (ou Zero Gamma)

Le niveau de prix où le GEX net **change de signe** — la frontière entre les
deux régimes ci-dessus. C'est la métrique la plus suivie de toute l'analyse
gamma.

Il n'est pas simplement lu sur le graphique : le profil complet est recalculé
sur une grille de prix hypothétiques (visible dans l'onglet **Gamma Profile**),
puis le croisement est interpolé.

### HVL (High Volatility Level)

Même calcul que le Gamma Flip, mais pondéré par le **volume du jour** plutôt
que par l'open interest. Là où le Flip décrit la structure héritée, le HVL
reflète ce qui se traite — et donc se couvre — aujourd'hui.

Un écart marqué entre les deux est en soi une information sur l'orientation du
flux de la séance.

### Call Wall et Put Support

Les concentrations de gamma les plus fortes, **contraintes directionnellement** :

- **Call Wall** : le plus gros mur de calls **au-dessus** du prix — résistance.
- **Put Support** : le plus gros mur de puts **en dessous** — support.

Cette contrainte n'est pas cosmétique. Le plus gros mur de puts en valeur
absolue peut très bien se situer au-dessus du prix, auquel cas l'appeler
« support » n'aurait aucun sens.

### 1D Min et 1D Max

Les bornes du mouvement attendu sur l'échéance la plus proche, déduites du prix
du **straddle à la monnaie**. Le straddle *est* l'estimation de mouvement par le
marché lui-même — aucune hypothèse de modèle n'intervient.

### GEX1 à GEX5

Les cinq strikes au gamma le plus important en valeur absolue, sans contrainte
de direction. Ce sont les murs bruts, classés par poids. La case **Major Walls
seulement** filtre ceux qui pèsent moins de 25 % du plus fort.

### DEX (Delta Exposure)

L'équivalent du GEX pour le delta : l'exposition directionnelle que les teneurs
de marché portent à chaque strike.

### Vanna et Charm (onglet dédié)

Les grecques de second ordre, qui expliquent des flux de couverture que le
gamma seul ne capture pas :

- **Vanna** — sensibilité du delta à la volatilité implicite. Quand l'IV se
  détend, les teneurs de marché doivent racheter du delta : c'est la mécanique
  des hausses lentes sans catalyseur apparent.
- **Charm** — décroissance du delta avec le **temps qui passe**. Ce flux est
  purement mécanique et donc prévisible ; il explique une partie des dérives de
  fin de séance et des comportements de semaine d'expiration.

### Le flux delta

Une estimation du delta échangé, minute par minute, obtenue en multipliant la
variation de volume de chaque contrat par son delta.

**Sa limite doit être comprise** : ce feed ne dit pas si une transaction a été
initiée à l'achat ou à la vente. C'est donc une mesure de *pression pondérée
par le delta*, pas un véritable flux d'ordres signé. Elle indique l'intensité
et la concentration, pas la direction agressive.

---

## Options avancées (facultatives, payantes)

Le dashboard fonctionne intégralement sans rien de ce qui suit.

### Historique via Databento

Permet de pré-remplir plusieurs mois d'historique quotidien (GEX net, Gamma
Flip) et de récupérer le flux intraday de séances passées.

Facturation à la donnée téléchargée. Le script affiche **un devis avant tout
téléchargement** et refuse de dépasser un plafond que tu fixes
(`--max-cost`). Les fichiers bruts sont conservés localement : relancer ne
refacture jamais ce qui a déjà été récupéré.

Nécessite un compte Databento et la variable d'environnement
`DATABENTO_API_KEY` (voir `.env.example`).

À savoir : les données de la séance la plus récente restent sous licence
« temps réel » pendant environ un jour ouvré. Une erreur de licence sur la
veille est normale — il suffit d'attendre.

### Temps réel via un compte courtier (gratuit avec le compte)

Un compte courtier donnant accès à dxFeed — le dashboard est écrit pour
tastytrade, qui inclut ces données sans supplément — fait passer en direct :

- le **spot** des sous-jacents et des futures ;
- le **GEX net recalculé à ce spot**, donc la distance au Gamma Flip et la
  lecture du régime, qui se périment en quelques minutes ;
- l'enregistrement de **bougies à la minute**, et la récupération de plusieurs
  semaines d'**historique** en une passe.

**Les chaînes d'options restent délayées** : elles continuent de venir de CBOE.
Les murs de gamma et le Gamma Flip ne bougent pas davantage pour autant,
puisqu'ils reposent sur l'open interest publié une fois par jour.

Mise en place : créer une application OAuth depuis les paramètres du compte,
lancer `python -m gex.tt_auth` pour obtenir un jeton, puis renseigner
`TASTYTRADE_CLIENT_ID`, `TASTYTRADE_CLIENT_SECRET` et `TT_REFRESH` en variables
d'environnement — jamais dans un fichier du dépôt. Sans ces variables, le
module reste inerte et rien ne change.

Ouvrir un compte de courtage est une démarche personnelle et engageante ; le
dashboard fonctionne parfaitement sans, et ceci n'est pas une recommandation.

**Ces données ne sont jamais redistribuables** : elles restent sur l'instance
locale de leur titulaire. Le programme applique la règle par construction —
provenance marquée à l'écriture, et export limité aux seules données CBOE.

---

## Limites connues

- **Délai de 15 minutes** sur la source gratuite. Outil de lecture de
  structure, jamais d'exécution.
- **L'open interest est quotidien.** Aucun fournisseur, gratuit ou payant, n'y
  change quoi que ce soit : c'est l'OCC qui le publie.
- **Le sens des transactions n'est pas observable** dans le flux gratuit (voir
  la section sur le flux delta).
- **Hypothèse de positionnement des teneurs de marché.** Comme tous les outils
  de ce type, le calcul suppose que les dealers sont longs de calls et courts
  de puts. C'est une convention répandue et utile, pas une vérité mesurée.
- **L'endpoint CBOE n'est pas contractuel** : son format peut changer sans
  préavis. L'ingestion est isolée pour pouvoir brancher une autre source.

---

## Avertissement

Cet outil sert **exclusivement à l'analyse**. Il ne passe aucun ordre, ne se
connecte à aucun compte de trading, et ne constitue ni un conseil en
investissement ni une recommandation. Les calculs reposent sur des conventions
publiques et des hypothèses explicitées ci-dessus, susceptibles d'être fausses.

Distribué sous [licence MIT](LICENSE), sans aucune garantie.
