# Guide d'installation pas à pas

*[English version](INSTALL.en.md)*

Ce guide s'adresse à quelqu'un qui **n'a jamais installé de programme de ce
type**. Aucune connaissance préalable n'est supposée. Compte environ 15 minutes.

À la fin, tu auras le dashboard qui tourne dans ton navigateur. **Aucun compte,
aucune clé, aucun paiement** : les données viennent d'une source publique
gratuite.

> 💡 Lis chaque étape en entier avant de la faire. En cas de blocage, la
> section [En cas de problème](#en-cas-de-problème) en bas couvre les erreurs
> les plus courantes.

---

## Étape 1 — Installer Python

Python est le langage dans lequel le programme est écrit. Il faut l'installer
une fois.

### Sur Windows

1. Va sur **https://www.python.org/downloads/**
2. Clique sur le gros bouton jaune **« Download Python 3.x »**.
3. Ouvre le fichier téléchargé (en bas de ton navigateur, ou dans le dossier
   *Téléchargements*).
4. **⚠️ POINT LE PLUS IMPORTANT** : sur le premier écran de l'installateur,
   coche la case **« Add python.exe to PATH »** tout en bas de la fenêtre,
   **avant** de cliquer sur *Install Now*. Si tu oublies cette case, rien ne
   marchera ensuite.
5. Clique sur **Install Now** et laisse aller jusqu'au bout.

### Sur Mac

1. Va sur **https://www.python.org/downloads/**
2. Clique sur **« Download Python 3.x »**.
3. Ouvre le fichier `.pkg` téléchargé et suis l'installateur (clique
   *Continuer* / *Installer* jusqu'à la fin).

---

## Étape 2 — Télécharger le programme

Deux méthodes. **La méthode B (Git) est recommandée** si tu comptes garder
l'outil : la mise à jour se fera plus tard en **une seule commande**, sans rien
retélécharger. La méthode A (ZIP) est la plus simple pour juste essayer.

### Méthode A — Fichier ZIP (la plus simple)

1. Va sur **https://github.com/Darthreign/gex-dashboard**
2. Clique sur le bouton vert **« Code »** (en haut à droite de la liste des
   fichiers).
3. Dans le menu qui s'ouvre, clique sur **« Download ZIP »**.
4. Une fois téléchargé, **décompresse le ZIP** :
   - **Windows** : clic droit sur le fichier → *Extraire tout* → *Extraire*.
   - **Mac** : double-clique sur le fichier, il se décompresse tout seul.
5. Tu obtiens un dossier nommé **`gex-dashboard-main`**. Déplace-le où tu veux
   (par exemple sur ton *Bureau*).

### Méthode B — Git (recommandée, mises à jour faciles)

Git est un petit outil qui télécharge le programme **et** permet de le mettre à
jour ensuite d'une seule commande. Il s'installe une fois.

1. **Installe Git** :
   - **Windows** : va sur **https://git-scm.com/download/win**, le
     téléchargement démarre seul. Ouvre le fichier et clique **Next** à chaque
     écran (les réglages par défaut conviennent parfaitement) jusqu'à
     *Install*, puis *Finish*.
   - **Mac** : ouvre l'app **Terminal** et tape `git --version`. S'il n'est pas
     installé, une fenêtre te propose de l'installer : accepte. Sinon Git est
     déjà là.
2. **Télécharge le programme** : ouvre un terminal (Terminal sur Mac ;
   sur Windows, ouvre le dossier où tu veux le mettre — ton *Bureau* par
   exemple — clique dans la barre d'adresse, tape `powershell`, Entrée), puis
   tape :

   ```
   git clone https://github.com/Darthreign/gex-dashboard.git
   ```

3. Tu obtiens un dossier nommé **`gex-dashboard`**.

> La suite du guide parle du « dossier du programme » : ce sera
> `gex-dashboard-main` si tu as pris le ZIP, ou `gex-dashboard` si tu as pris
> Git. C'est le même contenu.

---

## Étape 3 — Ouvrir le terminal DANS le dossier

Le « terminal » est une fenêtre où on tape des commandes. Le point délicat est
de l'ouvrir **au bon endroit** : à l'intérieur du dossier du programme.

### Sur Windows

1. Ouvre le **dossier du programme** (`gex-dashboard-main` si tu as pris le
   ZIP, `gex-dashboard` si tu as pris Git). Tu dois voir à l'intérieur des
   fichiers comme `run.py`, `requirements.txt`, un dossier `gex`…
2. Clique une fois dans la **barre d'adresse** en haut de la fenêtre (là où
   est écrit le chemin du dossier). Le texte se surligne en bleu.
3. Tape **`powershell`** par-dessus et appuie sur **Entrée**.
4. Une fenêtre bleu foncé ou noire s'ouvre : c'est le terminal, déjà placé dans
   le bon dossier.

### Sur Mac

1. Ouvre l'application **Terminal** (cherche « Terminal » dans Spotlight avec
   *Cmd + Espace*).
2. Tape **`cd `** (avec un espace après `cd`), **sans appuyer sur Entrée**.
3. **Glisse le dossier du programme** depuis le Finder directement dans la
   fenêtre du Terminal : son chemin s'écrit tout seul.
4. Appuie sur **Entrée**.

---

## Étape 4 — Installer et lancer

Tape les commandes suivantes **une par une**, en appuyant sur **Entrée** après
chaque, et en attendant que chacune se termine avant de passer à la suivante.

### Sur Windows

Créer l'environnement (quelques secondes) :

```
python -m venv .venv
```

Installer les composants nécessaires (1 à 3 minutes ; beaucoup de texte défile,
c'est normal) :

```
.venv\Scripts\python -m pip install -r requirements.txt
```

Lancer le dashboard :

```
.venv\Scripts\python run.py
```

### Sur Mac

```
python3 -m venv .venv
```

```
.venv/bin/python -m pip install -r requirements.txt
```

```
.venv/bin/python run.py
```

### Ce que tu dois voir

Après la dernière commande, il reste écrit quelque chose comme :

```
Dash is running on http://127.0.0.1:8050/
```

C'est bon signe : **le programme tourne**. Laisse cette fenêtre ouverte — c'est
elle qui fait fonctionner le dashboard.

---

## Étape 5 — Ouvrir le dashboard

1. Ouvre ton navigateur habituel (Chrome, Edge, Firefox, Safari…).
2. Dans la barre d'adresse, tape exactement : **`127.0.0.1:8050`** puis Entrée.
3. Le dashboard s'affiche. 🎉

Il commence à collecter les données immédiatement. Les premières valeurs
apparaissent en quelques secondes ; les graphiques d'historique se remplissent
au fil des jours d'utilisation.

---

## Utilisation au quotidien

- **Arrêter le programme** : reviens dans la fenêtre du terminal et appuie sur
  **Ctrl + C** (Windows et Mac). Ou ferme simplement la fenêtre.
- **Le relancer plus tard** : rouvre le terminal dans le dossier (étape 3) et
  tape **une seule** commande — l'installation, elle, ne se refait pas :
  - Windows : `.venv\Scripts\python run.py`
  - Mac : `.venv/bin/python run.py`
- **Quand faut-il le laisser tourner ?** Lance-le pendant les heures de marché
  américain les jours où tu veux suivre les données. En dehors, il se met en
  veille et ne consomme rien. Tu peux le fermer la nuit et le week-end sans
  rien perdre d'important.

---

## En cas de problème

| Message / symptôme | Cause | Solution |
|---|---|---|
| `python n'est pas reconnu…` (Windows) | La case *Add to PATH* n'a pas été cochée à l'étape 1 | Réinstalle Python en **cochant bien la case**, puis ferme et rouvre le terminal |
| `command not found: python` (Mac) | Sur Mac la commande est `python3` | Utilise **`python3`** au lieu de `python` |
| La barre d'adresse ne veut pas de `powershell` | Fenêtre pas au premier plan | Reclique dans le dossier, puis dans la barre d'adresse, retape `powershell` |
| `pip install` s'arrête sur une erreur rouge | Coupure réseau pendant le téléchargement | Relance simplement la même commande `pip install …` |
| Le navigateur affiche « site inaccessible » | Le terminal a été fermé, ou tu as tapé la mauvaise adresse | Vérifie que la fenêtre du terminal est toujours ouverte et affiche *Dash is running*, et que tu as tapé `127.0.0.1:8050` |
| Une fenêtre de pare-feu demande une autorisation au premier lancement | Windows demande si le programme peut accéder au réseau | Autorise (c'est nécessaire pour récupérer les données) |

Si tu es vraiment bloqué, note le message d'erreur exact et demande de l'aide à
la personne qui t'a partagé le programme.

---

## Pour aller plus loin (facultatif)

- **Version en anglais / français** : bouton *FR / EN* en haut du dashboard.
- **Comprendre les indicateurs** : bouton *FAQ* en haut du dashboard, ou le
  fichier [FAQ.md](FAQ.md).
- **Mettre à jour le programme** plus tard :
  - **Si tu as utilisé Git** (méthode B) : ouvre le terminal dans le dossier
    (étape 3) et tape simplement `git pull`. C'est tout — le programme se met à
    jour, tes données dans `data/` sont conservées. Si la mise à jour touche
    les composants, relance ensuite la commande `pip install …` de l'étape 4.
  - **Si tu as utilisé le ZIP** (méthode A) : retélécharge le ZIP (étape 2) et
    refais l'installation dans le nouveau dossier. Récupère ton ancien dossier
    `data/` si tu veux garder ton historique.
- **Assistant Claude Code** : si tu utilises Claude Code, le
  [README](README.md) propose un prompt qui fait toute l'installation à ta
  place.

Bon usage — et souviens-toi que c'est un **outil d'analyse**, pas un conseil en
investissement (voir l'[avertissement](DISCLAIMER.md)).
