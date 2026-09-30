# Bot Telegram — Cagnotte

Bot qui additionne les montants de chacun et affiche la cagnotte sous forme d'image (podium avec photos de profil + classement).

## Commandes

| Commande | Qui | Effet |
|---|---|---|
| `/top 50` | tout le monde | ajoute 50 € à ton total |
| `/cagnotte` | tout le monde | envoie l'image de la cagnotte |
| `/aide` | tout le monde | aide |
| `/solde @pseudo 120` | admin | fixe le solde d'un utilisateur |
| `/ajouter @pseudo -30` | admin | ajoute / retire un montant |
| `/supprimer @pseudo` | admin | retire l'utilisateur du top |
| `/reset confirmer` | admin | remet tout à zéro |

Les commandes admin acceptent aussi un ID numérique, ou s'utilisent **en réponse** au message de la personne (`/solde 120`).
Seul l'utilisateur `ADMIN_ID` peut les utiliser. Les commandes apparaissent dans les suggestions du bot (menu `/`).

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env   # puis mettre le token
python bot.py
```

Les données sont stockées dans `data.json`.

> Dans un groupe, le bot doit pouvoir lire les commandes (par défaut c'est le cas pour les messages commençant par `/`).
> Polices : place `fonts/bold.ttf` et `fonts/black.ttf` pour personnaliser, sinon les polices système sont utilisées (sous Linux : `apt install fonts-dejavu-core`).
