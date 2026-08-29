# Health Console

Une console web locale qui présente la santé d'une machine Ubuntu selon deux
lectures assumées : un **mode Simple**, en français courant, compréhensible sans
culture technique, et un **mode Expert** qui expose tout.

> **État du projet : conception.** Le document de conception est complet et validé ;
> **il n'y a pas encore de code**. Ce dépôt n'est pas installable en l'état.

## L'idée

Afficher `77 °C` est trivial. Dire **si c'est grave** est le produit.

La plupart des outils de supervision s'adressent à des administrateurs système et
laissent l'utilisateur seul face à des chiffres. Health Console fait le travail
inverse : elle mesure comme un outil sérieux, puis elle **interprète**.

- « Le disque gagne 1,8 Go par semaine, saturation estimée en mars 2027 »
  plutôt que `9% /dev/sda2`
- Un score de santé dont **chaque point perdu est traçable** à un constat précis —
  pas le chiffre magique inexplicable des logiciels d'antivirus
- Des actions correctives à portée de clic, issues d'un **catalogue fermé**

## Principes de conception

**La mesure et le jugement sont séparés.** Les sondes ne produisent que des
chiffres ; les règles ne raisonnent que sur des chiffres. Toute la logique
d'interprétation est donc une fonction pure, testable sans matériel.

**L'outil n'a pas le droit de mentir.** Si le flux de données tombe, la page grise
les valeurs et affiche depuis quand elles sont figées. Si un capteur renvoie une
valeur invraisemblable — cas réel : un pilote de batterie qui annonce une charge de
46 700 % — la console affiche « incohérent » plutôt qu'un chiffre faux. Une console
de santé qui ment sur sa propre fraîcheur est un piège.

**Le bruit détruit la confiance.** 59 erreurs par jour dans le journal, c'est le
régime normal d'un Linux de bureau. Les remonter brutes ferait paniquer pour rien,
après quoi plus personne ne lit les vraies alertes. Les erreurs sont regroupées par
motif ; seul ce qui est nouveau ou en accélération est signalé.

**Un outil de santé ne doit pas nuire à la santé de la machine.** Budget cible :
moins de 60 Mo de mémoire résidente, moins de 2 % de CPU en moyenne, base de
données bornée à 40 Mo par une rétention en cascade.

## Architecture

Python de la bibliothèque standard, `psutil` et SQLite. **Aucune dépendance `pip`,
aucun environnement virtuel, aucune étape de compilation, aucun CDN** — un outil de
diagnostic doit fonctionner sans Internet, et survivre aux mises à jour de la
distribution.

Le collecteur travaille à deux cadences : 2 secondes pour ce qui est gratuit à lire
(`/proc`, `/sys`), 5 minutes pour ce qui coûte cher (SMART, APT, systemd). Le
serveur HTTP diffuse l'état en SSE. Le front est du HTML, du CSS et des modules ES
natifs, graphiques SVG dessinés à la main.

Conception détaillée :
[`docs/superpowers/specs/2026-08-29-health-console-design.md`](docs/superpowers/specs/2026-08-29-health-console-design.md)

## Avertissements de sécurité

Ce projet est conçu pour **un poste personnel sur un réseau de confiance**. Lis ceci
avant de le déployer ailleurs.

- **Il exécute des commandes privilégiées.** Mises à jour APT, redémarrage de
  services, extinction de la machine. L'installation pose une règle `sudoers.d`
  restreinte à une liste nommée de binaires avec leurs arguments figés — jamais
  `ALL`, jamais de joker.
- **Il n'existe aucune route « exécute cette commande ».** Le catalogue d'actions
  est déclaré en dur ; le navigateur envoie un identifiant, jamais un fragment de
  commande. `shell=False`, aucune interpolation de texte venu du réseau.
- **Il écoute sur le réseau local**, protégé par un jeton. Les actions sont
  **refusées hors de `127.0.0.1`** sauf activation explicite en configuration :
  consulter et agir n'ont pas le même coût quand on se trompe.
- **Il n'est pas conçu pour être exposé sur Internet.** Ne le fais pas.
- Chaque action est journalisée avec son heure, sa source, son code de retour et sa
  sortie complète.

## Cible

Ubuntu 26.04 LTS. Les principes valent pour toute distribution récente, mais les
sondes APT, snap et systemd sont spécifiques à Debian/Ubuntu.

## Licence

MIT — voir [LICENSE](LICENSE).
