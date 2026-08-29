# Health Console — Document de conception

- **Date** : 2026-08-29
- **Cible** : Ubuntu 26.04 LTS « Resolute Raccoon », kernel 7.0, poste unique (portable HP)
- **Statut** : conception validée, prêt pour le plan d'implémentation

## 1. Objectif

Une console web locale qui présente la santé de la machine, de l'OS et du matériel
selon deux lectures assumées :

- **Mode Simple** — un verdict en français courant, compréhensible sans culture
  technique, avec les actions correctives à portée de clic.
- **Mode Expert** — la salle de contrôle complète : métriques brutes, courbes
  d'historique, tables, journal d'audit.

La valeur du projet est dans l'**interprétation** des mesures, pas dans leur
collecte. Afficher `77 °C` est trivial ; dire si c'est grave est le produit.

## 2. Décisions validées

| Sujet | Décision |
|---|---|
| Usage | Tableau de bord live **et** historique long terme |
| Périmètre | Noyau vital, santé matérielle, OS & maintenance, réseau & processus |
| Privilèges | Règle `sudoers.d` ciblée, service en simple utilisateur |
| Présentation | Deux modes explicites, commutateur Simple / Expert |
| Réseau | Écoute LAN, protégée par jeton |
| Stack | Python stdlib + `psutil` (apt) + SQLite ; front vanilla, sans build |
| Actions | Catalogue fermé : mises à jour, ménage, services & système, disque & diagnostic |

## 3. Contraintes de plateforme (mesurées le 2026-08-29)

Ces mesures ne sont pas décoratives : chacune contraint une décision.

- **PEP 668** — `/usr/lib/python3.14/EXTERNALLY-MANAGED` est présent. `pip install`
  sur le Python système est refusé. `psutil` 7.1.0 et `jinja2` sont disponibles en
  paquets apt ; ni Flask, ni FastAPI, ni uvicorn. → **Aucune dépendance hors apt,
  aucun venv.**
- **Ressources** — 4 cœurs, 5,2 Go de RAM dont ~1,5 Go réellement disponible.
  → **Budget : < 60 Mo de RSS pour le service, < 2 % de CPU en moyenne.** Un outil
  de santé qui dégrade la santé de la machine est un échec de conception.
- **Matériel** — SSD Crucial MX300 489 Go (`/dev/sda`), GPU AMD Radeon HD 6730M,
  batterie `BAT0`, souris Logitech avec batterie propre (`hidpp_battery_0`).
- **Capteurs** — `lm-sensors` absent, mais `/sys/class/hwmon` expose `coretemp`,
  `acpitz`, `radeon`, `hp`, `BAT0`, `AC`. → **Lecture directe de sysfs, pas de
  dépendance à `sensors`.**
- **Batterie** — `BAT0` n'expose **pas** `energy_full` mais la famille `charge_*`.
  → **Les sondes lisent les deux conventions**, sinon la batterie apparaîtrait
  « indisponible » sur cette machine précise.
- **Le pilote batterie de cette machine renvoie des valeurs incohérentes** :
  `charge_full = 1000`, `charge_full_design = 1000`, mais `charge_now = 467000`
  et `capacity = 46700` (là où `capacity` est un pourcentage 0-100). Un calcul
  naïf annoncerait « 0 % d'usure, chargée à 46 700 % » — un mensonge affirmé avec
  aplomb. → **Contrôle de plausibilité obligatoire, voir §7.7.**
- **Snaps** — ~39 montages `loop*`, `/var/lib/snapd` à 4,7 Go, 13 révisions
  désactivées. → **`df` brut est illisible pour un humain : les squashfs sont
  filtrés**, et le ménage des snaps est un gain réel.
- **`unattended-upgrades` est activé.** → La sonde « mises à jour » doit distinguer
  ce qui sera installé automatiquement de ce qui exige une intervention, sinon elle
  réclame une action déjà prise en charge.
- **`Linger=no`** — un service utilisateur s'arrête à la déconnexion. → L'installation
  propose `loginctl enable-linger` pour un historique continu.
- **sudo demande un mot de passe** — d'où la règle `sudoers.d` ciblée.

### État de référence au moment de la conception

Disque `/` à 9 % (40 Go / 481 Go) · 0 service en échec · 17 mises à jour en attente ·
59 erreurs au journal sur 24 h · ~5,3 Go récupérables (snaps 4,7 Go, cache APT
475 Mo, `/var/log` 177 Mo) · CPU package 77 °C · charge 0,62.

## 4. Architecture

### 4.1 Principe structurant : deux cadences

Lire `/proc` coûte des microsecondes ; lancer `apt list --upgradable` ou `smartctl`
coûte des centaines de millisecondes et réveille le disque. Les confondre ruinerait
le budget de ressources.

- **Cadence rapide — 2 s** : CPU, mémoire, températures, débit réseau, charge,
  batterie. Lecture pure de `/proc` et `/sys`.
- **Cadence lente — 5 min** : SMART, mises à jour APT, services systemd, journal,
  remplissage des partitions, processus, usure batterie, infos OS.

### 4.2 Sondes indépendantes

Chaque domaine est un module exposant la même interface :

```python
NAME    = "thermal"
CADENCE = FAST
def collect() -> dict                    # mesure brute, aucune interprétation
def evaluate(sample, history) -> list[Finding]   # fonction pure
```

**Séparation stricte mesure / jugement.** `collect()` ne produit que des chiffres ;
`evaluate()` ne raisonne que sur des chiffres et ne touche ni au système ni à
l'horloge. Toute la logique de verdict est donc testable sans matériel, avec des
échantillons figés — c'est la propriété qui rend le mode Simple vérifiable.

**Isolation des pannes.** Une sonde qui échoue (SMART sans droits, pas de batterie,
`apt` verrouillé) renvoie `{"status": "unavailable", "reason": ...}` et n'affecte
aucune autre. Un outil de diagnostic qui plante quand quelque chose ne va pas est
pire qu'inutile.

### 4.3 Flux

```
  /proc /sys      ┌──────────────┐        ┌──────────┐
  smartctl   ───► │ ordonnanceur │ ─────► │  SQLite  │
  apt systemd     │  2 s / 5 min │        └────┬─────┘
                  └──────┬───────┘             │
                         │ état courant        │ historique
                         ▼                     ▼
                  ┌───────────────────────────────┐
                  │  serveur HTTP (ThreadingHTTP) │
                  │  /api/now /api/history        │
                  │  /api/stream (SSE) /api/actions│
                  └───────────────┬───────────────┘
                                  ▼
                     navigateur — Simple / Expert
```

Le temps réel passe par **SSE** et non WebSocket : le flux est unidirectionnel,
SSE tient en une trentaine de lignes sur `http.server` et se reconnecte tout seul.
Plafond de 8 flux simultanés (un thread chacun), au-delà le client bascule en
sondage à 5 s.

## 5. Catalogue des sondes

| Sonde | Cadence | Source | Si indisponible |
|---|---|---|---|
| `cpu` | rapide | `psutil`, `/proc/stat`, `/proc/cpuinfo` | — |
| `memory` | rapide | `psutil`, `/proc/meminfo` | — |
| `thermal` | rapide | `/sys/class/hwmon/*` (coretemp, acpitz, radeon, hp) | zone masquée |
| `network` | rapide | `psutil.net_io_counters`, `ip -j addr` | — |
| `battery` | rapide | `/sys/class/power_supply/*` — `energy_*` **et** `charge_*`, avec contrôle de plausibilité (§7.6) | carte masquée (poste fixe) ; valeurs aberrantes → « incohérent (pilote) » |
| `storage` | lente | `psutil.disk_partitions` filtré (squashfs/tmpfs exclus), `du` ciblé | — |
| `smart` | lente | `sudo smartctl -a /dev/sda` | « SMART verrouillé » + commande d'activation |
| `updates` | lente | `apt-get -s dist-upgrade`, `/var/run/reboot-required`, état d'`unattended-upgrades` | « verrou apt occupé, nouvelle tentative » |
| `services` | lente | `systemctl --failed --output=json` | — |
| `journal` | lente | `journalctl -p err -S -24h -o json` groupé | — |
| `processes` | lente | `psutil.process_iter` top 10 CPU / RSS | — |
| `osinfo` | lente | `/etc/os-release`, `uname`, uptime, fin de support | — |

## 6. Stockage et rétention

SQLite en mode WAL, `synchronous=NORMAL`, dans `~/.local/share/health-console/db.sqlite3`.

```sql
CREATE TABLE metric     (ts INTEGER, key TEXT, value REAL);           -- brut 2 s
CREATE TABLE metric_5m  (ts INTEGER, key TEXT, avg REAL, min REAL, max REAL);
CREATE TABLE snapshot   (ts INTEGER, probe TEXT, json TEXT);
CREATE TABLE event      (id INTEGER PRIMARY KEY, finding_id TEXT, severity TEXT,
                         opened_ts INTEGER, closed_ts INTEGER);
CREATE TABLE action_run (id TEXT PRIMARY KEY, ts INTEGER, action_id TEXT,
                         source TEXT, exit_code INTEGER, duration_ms INTEGER,
                         output TEXT);
CREATE INDEX metric_key_ts ON metric(key, ts);
```

**Rétention en cascade, bornée par construction** — une base qui grossit sans
limite est un problème de santé de plus, pas une fonctionnalité :

- `metric` brut : 48 h, puis agrégé en `metric_5m` et purgé
- `metric_5m` : 90 jours
- `snapshot` : le dernier de chaque sonde, plus un par heure sur 7 jours
- `event` : conservé 1 an (volume négligeable, c'est la mémoire des incidents)
- `action_run` : 1 an, sortie tronquée à 256 Kio

Purge et agrégation quotidiennes, plus `VACUUM` hebdomadaire. Taille attendue en
régime : **< 40 Mo**.

Nommage des métriques : `cpu.usage`, `cpu.freq`, `cpu.temp.pkg`, `mem.available`,
`mem.swap.used`, `load.1`, `disk.sda2.used_pct`, `net.enp0s25.rx_bps`,
`thermal.<zone>`, `battery.charge_pct`, `battery.wear_pct`.

## 7. Moteur de verdicts

### 7.1 Le constat, unité de base

```python
Finding(
  id="storage.reclaimable", severity=INFO,
  titre    = "5,3 Go peuvent être récupérés",
  pourquoi = "Ce sont d'anciennes versions de logiciels et des fichiers "
             "d'installation déjà utilisés. Les supprimer ne fait rien perdre.",
  action   = ActionRef("clean.all"),
  technique= "snapd 4,7 Go (13 rév. désactivées) · apt archives 475 Mo · "
             "/var/log 177 Mo",
)
```

`titre` et `pourquoi` alimentent le mode Simple ; `technique` alimente le mode
Expert. Aucune traduction à la volée, aucun jargon qui fuit vers le grand public.

Gravités : `OK`, `INFO`, `ATTENTION`, `URGENT`.

### 7.2 Score explicable

Départ à 100, chaque constat retire des points selon sa gravité
(`INFO` −2, `ATTENTION` −8, `URGENT` −25, plancher à 0). **Chaque point perdu est
traçable** : cliquer sur le score déplie la liste des constats qui l'ont fait
baisser. Aucun constat ⇒ 100, pas 94 « pour faire sérieux ». Le score d'antivirus,
chiffre magique inexplicable, est explicitement rejeté.

### 7.3 Anti-clignotement

Une console qui vire au rouge parce qu'une compilation a chargé le CPU 3 secondes
ne sera plus jamais crue. Donc :

- un seuil doit tenir sur une **fenêtre** avant d'ouvrir un constat (ex. CPU > 90 %
  pendant 5 min) ;
- il doit repasser sous un **seuil bas distinct** pour le refermer (hystérésis) ;
- l'ouverture et la fermeture sont écrites dans `event`, ce qui donne un historique
  des incidents et non seulement des courbes.

### 7.4 Tendances

Avec 90 jours en base, l'outil dit ce qu'un tableau de bord instantané ne peut pas
dire. Régression linéaire sur la fenêtre disponible, affichée seulement si la
corrélation est significative (r² > 0,7) et l'échéance sous 24 mois :

- « le disque gagne 1,8 Go par semaine, saturation estimée en mars 2027 » ;
- usure batterie extrapolée ;
- température CPU moyenne en hausse — signe habituel d'un ventilateur encrassé.

C'est le bénéfice concret du choix « live + historique ».

### 7.5 Seuils

Tous les seuils vivent dans **`rules.py`, un seul fichier**, pour qu'un ajustement
n'impose pas de relire le projet.

| Domaine | Attention | Urgent | Note |
|---|---|---|---|
| Disque `/` | > 80 % | > 92 % ou < 3 Go libres | + projection de saturation |
| Temp CPU | > 85 °C soutenu 5 min | > 95 °C ou throttling | 77 °C actuel = normal |
| Mémoire | dispo < 15 % et swap actif | OOM killer au journal | marge déjà mince |
| SMART | réallocations > 0, usure > 80 % | `FAILING_NOW` | usure = vrai indicateur SSD |
| Batterie | usure > 30 % | usure > 50 % | `charge_full / charge_full_design`, **si plausible** — sinon aucun constat |
| Mises à jour | correctif sécurité en attente | sécurité > 14 j en attente | pondéré par `unattended-upgrades` |
| Services | 1 en échec | échec + redémarrages en boucle | 0 chez toi |
| Journal | motif nouveau ou en accélération | panique noyau, erreurs I/O | 59/24 h = bruit normal |
| Réseau | perte DNS | pas de route par défaut | — |

### 7.6 Contrôle de plausibilité des capteurs

Le matériel ment. Pas par malveillance : pilotes approximatifs, firmwares ACPI
bâclés, unités incohérentes selon le constructeur. La batterie de cette machine en
est la démonstration (§3). Afficher un chiffre faux avec assurance est pire que
d'admettre qu'on ne sait pas — c'est précisément ce qui détruit la confiance dans
un outil de diagnostic.

Chaque grandeur déclare donc son **domaine de validité**, appliqué dans `collect()`
avant toute écriture en base :

| Grandeur | Domaine admis | Cohérence croisée |
|---|---|---|
| Pourcentages | 0 – 100 | — |
| Températures | −20 – 125 °C | zone ignorée si hors bornes |
| Charge batterie | > 0 | `charge_now <= charge_full * 1,05` |
| Capacité batterie | > 0 | `charge_full <= charge_full_design * 1,05` |
| Fréquence CPU | 100 MHz – 10 GHz | — |
| Compteurs réseau | monotones | remise à zéro = redémarrage d'interface, pas un débit négatif |

Une valeur hors domaine n'est **ni affichée ni stockée**. La carte concernée
indique « valeur incohérente rapportée par le pilote » avec la valeur brute en mode
Expert, pour que le problème reste diagnosticable. Sur cette machine, l'usure
batterie sera donc marquée indisponible plutôt que faussement rassurante à 0 %.

### 7.7 Le piège du bruit

59 erreurs au journal en 24 h, c'est le bruit normal d'un Linux de bureau
(Bluetooth, ACPI, pilotes). Les remonter brutes en « 59 erreurs ! » ferait paniquer
pour rien et détruirait la confiance dans l'outil — après quoi plus personne ne lit
les vraies alertes. La sonde `journal` **regroupe par message récurrent**, applique
une liste de motifs de bruit connus, et ne signale qu'un motif **nouveau** ou dont
la fréquence **accélère**.

## 8. Catalogue d'actions

### 8.1 Principe

Il n'existe **aucune route « exécute cette commande »**. Chaque action est déclarée
en dur :

```python
Action(
  id="apt.upgrade",
  label="Installer les mises à jour",
  argv=["/usr/bin/apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
        "dist-upgrade"],
  root=True, risk=MEDIUM, duree="quelques minutes",
  confirm="Cette opération va installer 17 paquets. Ne coupez pas l'alimentation.",
)
```

Le navigateur envoie **un identifiant**, jamais un fragment de commande. `argv` est
figé, `shell=False`, aucune interpolation de texte venu du réseau. C'est ce qui
sépare un catalogue d'actions d'un shell à distance.

### 8.2 Le catalogue

| id | Libellé | Risque | Commande |
|---|---|---|---|
| `apt.refresh` | Rafraîchir la liste des paquets | sûr | `apt-get update` |
| `apt.upgrade` | Installer toutes les mises à jour | moyen | `apt-get -y -o Dpkg::Options::=--force-confold dist-upgrade` |
| `apt.security` | Installer les correctifs de sécurité seulement | moyen | `unattended-upgrade` |
| `clean.autoremove` | Supprimer les paquets orphelins | moyen | `apt-get -y autoremove --purge` |
| `clean.aptcache` | Vider le cache des paquets | sûr | `apt-get clean` |
| `clean.snaps` | Supprimer les anciennes versions de snaps | moyen | `snap remove --revision=<r> <nom>` par révision désactivée |
| `clean.journal` | Tronquer les journaux à 15 jours | moyen | `journalctl --vacuum-time=15d` |
| `svc.restart` | Redémarrer un service en échec | moyen | `systemctl restart <unité>` — unité **issue de la liste des services en échec**, jamais du client |
| `sys.reboot` | Redémarrer la machine | sensible | `shutdown -r +1` |
| `sys.poweroff` | Éteindre la machine | sensible | `shutdown -h +1` |
| `disk.selftest` | Autotest court du SSD | sûr | `smartctl -t short /dev/sda` |
| `disk.trim` | Entretien du SSD (TRIM) | sûr | `fstrim -av` |
| `report.export` | Exporter un rapport de diagnostic | sûr | interne, sans privilège |

Les actions de nettoyage **annoncent d'abord l'espace récupérable** (simulation
`apt-get -s`, `du`, `snap list --all`) avant de proposer le bouton.

`sys.reboot` et `sys.poweroff` ne sont **affichés que lorsqu'un redémarrage est
requis** : un bouton dangereux affiché en permanence finit par être cliqué par
accident. Délai d'une minute, annulable.

**Les deux seules actions paramétrées sont `svc.restart` et `clean.snaps`.** Dans
les deux cas, le paramètre n'est jamais accepté sur parole : il doit appartenir à
une liste que le serveur reconstruit lui-même au moment de l'exécution — les unités
actuellement en échec pour `svc.restart`, les révisions de snaps actuellement
désactivées pour `clean.snaps`. Une valeur absente de cette liste est rejetée en
`400`. Le client peut donc choisir parmi des possibilités, jamais en inventer.

### 8.3 Garde-fous

- **Verrou unique** : une seule action à la fois, jamais deux `apt` concurrents.
- **Sortie en direct** : la sortie défile en temps réel dans la page via SSE
  (`apt upgrade` prend des minutes ; un spinner aveugle est inacceptable).
- **Audit** : heure, action, source, code de retour, durée, sortie complète, écrits
  en base et consultables dans l'interface.
- **Confirmation** explicite côté navigateur, avec le texte de conséquence.
- **Refus hors loopback par défaut** : consulter et agir n'ont pas le même coût
  quand on se trompe. Déblocable par `allow_remote_actions = true`.
- **Délai maximal** : 30 min, au-delà le processus est terminé et l'échec journalisé.

### 8.4 Règle sudoers

`/etc/sudoers.d/health-console`, mode 0440, validée par `visudo -c` à l'installation.
Chemins absolus résolus et vérifiés sur cette machine :
`/usr/sbin/smartctl`, `/usr/bin/apt-get`, `/usr/bin/systemctl`, `/usr/bin/journalctl`,
`/usr/sbin/fstrim`, `/usr/bin/snap`, `/usr/sbin/shutdown`,
`/usr/bin/unattended-upgrade`.

Chaque entrée est nommée avec ses arguments figés. **Aucun joker, jamais `ALL`.**
Le fichier est fourni prêt à installer ; l'installateur affiche son contenu et
demande confirmation avant de l'écrire.

## 9. API HTTP

| Route | Méthode | Rôle |
|---|---|---|
| `/` | GET | la page (mode selon `localStorage`) |
| `/static/*` | GET | HTML, CSS, JS, icônes — servis depuis le disque |
| `/api/now` | GET | état complet : sondes, constats, score |
| `/api/history` | GET | `?metric=<clé>&range=1h\|24h\|7d\|90d` |
| `/api/stream` | GET | SSE : `tick` (rapide), `state` (lent), `action` (sortie) |
| `/api/actions` | GET | catalogue disponible dans l'état courant |
| `/api/actions/<id>` | POST | lance l'action, retourne `run_id` |
| `/api/actions/runs` | GET | journal d'audit |
| `/api/export` | GET | rapport HTML autonome, un seul fichier |

Toutes les réponses sont en JSON hors `/` et `/static/*`. Les erreurs portent un
code HTTP juste et un corps `{"error": "...", "detail": "..."}`.

## 10. Interface

### 10.1 Deux modes

Un seul document, deux compositions. Le commutateur mémorise le choix en
`localStorage` ; **Simple est le mode par défaut**, l'inverse trahirait la commande.

**Simple** — une colonne large et aérée. Verdict en haut, puis une carte par
constat, chacune avec son bouton d'action quand il y en a une. Ni graphique ni
unité exotique : « il reste 441 Go », pas « 9 % de 481 Go ».

**Expert** — grille dense : jauges, sparklines des 60 dernières minutes, tables des
processus et partitions, courbes 24 h / 7 j / 90 j, journal d'audit, et le JSON brut
de chaque sonde en dernier recours.

### 10.2 Accessibilité

Ce n'est pas un supplément d'âme quand on vise le grand public.

- La couleur ne porte **jamais** seule l'information : chaque état a une icône et un
  mot (« Attention », pas seulement de l'orange) — sinon 8 % des hommes ne lisent
  pas le tableau de bord.
- Contrastes AA, navigation clavier complète, cibles tactiles ≥ 44 px.
- `prefers-reduced-motion` et `prefers-color-scheme` respectés.
- Régions live ARIA pour les valeurs qui changent, sans bavardage du lecteur d'écran.
- Formats français via `Intl` : « 4,7 Go », « 21:11 ».

### 10.3 Responsive

L'accès LAN ayant été retenu, consulter depuis un téléphone est un usage réel : la
grille Expert se replie en une colonne, le mode Simple est pensé mobile d'abord.

### 10.4 Honnêteté de l'affichage

Si le flux SSE tombe, la page ne fige pas des valeurs périmées en faisant semblant :
elle grise les chiffres et affiche « données figées depuis 14:32 · reconnexion… »,
avec retente à délai croissant. **Une console de santé qui ment sur sa propre
fraîcheur est un piège.** Même règle pour une sonde indisponible : « SMART
indisponible » et la commande pour l'activer, jamais un zéro rassurant.

### 10.5 Technique front

Aucun build, aucun CDN — la console doit fonctionner sans Internet, ce qui est la
moindre des choses pour un outil de diagnostic. Modules ES natifs, CSS moderne
(grid, container queries, `oklch`), graphiques en SVG dessinés à la main
(sparklines et courbes d'historique). Budget : < 60 Kio de JS non compressé.

## 11. Sécurité

- Écoute sur `0.0.0.0:8787`, port configurable.
- **Jeton** aléatoire de 32 octets généré à l'installation, stocké dans
  `~/.config/health-console/config.toml` en mode 0600, exigé pour tout accès non
  loopback, comparé en **temps constant** (`hmac.compare_digest`).
- Le jeton vit dans le `localStorage` du navigateur, **jamais dans une URL** qu'on
  colle par mégarde dans un chat ou qui atterrit dans un historique.
- Actions refusées hors loopback sauf `allow_remote_actions = true`.
- Catalogue fermé, `argv` figés, `shell=False`, aucune interpolation.
- `sudoers.d` limité aux binaires nommés avec leurs arguments.
- En-têtes : `Content-Security-Policy: default-src 'self'`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`.
- Durcissement systemd : `NoNewPrivileges=no` (obligatoire, `sudo` en dépend),
  `ProtectSystem=strict` avec `ReadWritePaths=` limité à
  `~/.local/share/health-console` et `~/.config/health-console`, plus `PrivateTmp`
  et `RestrictNamespaces`. **`ProtectHome` n'est pas utilisé** : il empêcherait le
  service d'écrire sa propre base.
- Le rapport exporté indique en tête qu'il contient des informations système
  (noms d'hôte, interfaces, IP locales) et **ne contient jamais le jeton**.

## 12. Robustesse et modes dégradés

| Panne | Comportement |
|---|---|
| Sonde en échec | `unavailable` + raison affichée + commande pour débloquer |
| Verrou `apt` occupé | constat « mise à jour en cours ailleurs », nouvelle tentative au cycle suivant |
| Collecteur tué | `Restart=on-failure`, `RestartSec=5` |
| Base corrompue | recréée, incident journalisé — l'historique est précieux, pas vital |
| SSE coupé | reconnexion à délai croissant, données visiblement marquées périmées |
| Action en échec | code de retour et sortie complète affichés — jamais d'échec silencieux |
| Disque plein | l'écriture en base s'arrête proprement, la console continue à afficher |

## 13. Tests

La séparation mesure / jugement paie ici : `evaluate()` étant pure, toute la logique
se teste sans matériel et en millisecondes.

- **Fixtures** — échantillons réels capturés sur cette machine, plus des cas
  synthétiques impossibles à provoquer : disque à 99 %, SMART `FAILING_NOW`,
  batterie à 60 % d'usure, panique noyau, service en boucle de redémarrage,
  batterie absente (poste fixe), `charge_*` **et** `energy_*`, et **les valeurs
  aberrantes réelles de cette machine** (`charge_now` = 467 × `charge_full`), qui
  doivent produire « incohérent » et non un chiffre.
- **Verdicts** — un test par règle de `rules.py`, aux deux bords du seuil, plus
  l'hystérésis (une pointe brève n'ouvre pas de constat ; un dépassement soutenu
  l'ouvre ; le retour sous le seuil bas le ferme).
- **Score** — la somme est exacte et chaque point perdu remonte à un constat.
- **Stockage** — rétention, agrégation 5 min et purge, avec **horloge injectée**
  (pas de test qui attend 48 h).
- **Serveur** — routage, jeton absent / faux / valide, refus d'action depuis une IP
  non loopback, comportement au-delà de 8 flux SSE.
- **Actions** — construction des `argv` et validation du paramètre de `svc.restart`,
  **vérifiées sans jamais exécuter** les commandes.
- **Textes** — tout constat possède `titre`, `pourquoi` et `technique` non vides ;
  le mode Simple ne contient aucun terme d'une liste noire de jargon.
- **Fumée** — un test à part lance les vraies sondes sur cette machine, marqué comme
  dépendant du matériel.

## 14. Installation et exploitation

CLI `health-console` :

- `run` — lance en premier plan (développement)
- `install` — écrit l'unité systemd utilisateur, génère le jeton, affiche la règle
  sudoers **et demande confirmation** avant de l'installer, propose
  `loginctl enable-linger`
- `status` — état du service, du jeton, des privilèges, de la base
- `export [fichier]` — rapport HTML autonome
- `token [--rotate]` — affiche ou régénère le jeton

Unité systemd utilisateur : `WantedBy=default.target`, `Restart=on-failure`,
`MemoryMax=128M` (garde-fou dur contre une fuite sur une machine à 5 Go).

## 15. Arborescence

```
health-console/
├── bin/health-console
├── healthconsole/
│   ├── config.py       server.py     scheduler.py   store.py
│   ├── rules.py        verdict.py    actions.py     texts_fr.py
│   └── probes/  cpu memory storage thermal battery smart
│                network processes updates services journal osinfo
├── web/  index.html  app.js  style.css  charts.js  icons.svg
├── systemd/health-console.service
├── packaging/sudoers.d/health-console
├── tests/  fixtures/  test_verdict.py  test_store.py
│           test_server.py  test_actions.py  test_texts.py  test_smoke.py
└── docs/superpowers/specs/
```

## 16. Hors périmètre (YAGNI assumé)

Écarté volontairement, à rouvrir seulement sur besoin réel :

- multi-machines, agrégation de flotte ;
- alertes par courriel, Telegram ou notification de bureau ;
- comptes utilisateurs, rôles, authentification multi-utilisateur ;
- conteneurs, machines virtuelles (`docker` absent, `virbr0` inactif) ;
- GPU NVIDIA (`nvidia-smi` absent, GPU AMD ici) ;
- exécution de commandes arbitraires — exclu par conception, pas par manque de temps.

## 17. Risques connus

1. **Une action `apt` échoue à mi-parcours** et laisse dpkg en état incohérent. Le
   risque existe déjà en ligne de commande ; l'atténuation est la sortie complète
   affichée et journalisée, plus un constat « dpkg interrompu, exécutez
   `sudo dpkg --configure -a` » détecté au cycle suivant.
2. **L'exposition LAN élargit la surface.** Atténuée par le jeton, le refus d'action
   à distance par défaut et le catalogue fermé — mais le choix reste conscient et
   documenté ici.
3. **D'autres capteurs peuvent mentir comme la batterie.** Le contrôle de
   plausibilité (§7.6) couvre les grandeurs connues, mais un pilote peut inventer
   une valeur *dans* le domaine admis. Aucune parade générale ; la comparaison à
   l'historique signale au moins les sauts impossibles.
4. **Le format de sortie de `smartctl` varie** selon les modèles. La sonde utilise
   `--json` et dégrade proprement si un champ manque.
5. **`unattended-upgrades` peut agir pendant qu'un `apt.upgrade` manuel tourne.** Le
   verrou apt le protège ; l'action affiche alors « verrou occupé » plutôt que
   d'échouer sans explication.
