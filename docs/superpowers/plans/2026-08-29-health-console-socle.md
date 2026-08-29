# Health Console — Plan 1 : Socle mesurable

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Une console web qui tourne, mesure les grandeurs vitales de la machine en direct, les interprète en français courant et les affiche en mode Simple.

**Architecture:** Un ordonnanceur échantillonne les sondes rapides toutes les 2 s vers un tampon mémoire, et écrit une valeur agrégée en SQLite toutes les 30 s. Les sondes ne produisent que des chiffres ; un moteur de règles pures les transforme en constats et en score. Un serveur HTTP de la bibliothèque standard sert la page et diffuse l'état en SSE.

**Tech Stack:** Python 3.14 (bibliothèque standard uniquement) · `psutil` 7.1 (paquet apt `python3-psutil`) · SQLite (`sqlite3`) · `tomllib` · tests avec `unittest` · front en HTML/CSS/modules ES natifs, sans build ni CDN.

**Spec:** `docs/superpowers/specs/2026-08-29-health-console-design.md`

## Global Constraints

Ces contraintes s'appliquent à **toutes** les tâches, sans être répétées dans chacune.

- **Aucune dépendance `pip`, aucun venv, aucune étape de build.** Le Python système est `EXTERNALLY-MANAGED` (PEP 668). Seuls sont autorisés : la bibliothèque standard et `psutil` (déjà installé via apt en 7.1.0).
- **Tests avec `unittest` de la bibliothèque standard.** Commande unique : `python3 -m unittest discover -s tests -t . -v`. Ne jamais introduire pytest.
- **Aucune valeur de capteur n'est affichée ni stockée sans contrôle de plausibilité** (spec §7.6). Une valeur hors domaine devient « incohérent », jamais un chiffre.
- **La console n'a pas le droit de mentir** (spec §10.4) : données périmées visiblement marquées, sonde indisponible affichée comme telle, jamais un zéro rassurant.
- **Tous les seuils vivent dans `healthconsole/rules.py`**, nulle part ailleurs.
- **`collect()` mesure, `evaluate()` juge.** `evaluate()` est une fonction pure : pas d'accès au système, pas d'appel à l'horloge, pas d'entrée/sortie. C'est ce qui la rend testable sans matériel.
- **Budget** : < 60 Mo de RSS, < 2 % de CPU en moyenne, base ≈ 32 Mo avec les réglages par défaut.
- **Langue** : toute l'interface, tous les libellés de constats et tous les messages de commit sont en français. Les identifiants, noms de fonctions et de variables restent en anglais.
- **Accessibilité** (spec §10.2) : la couleur ne porte jamais seule l'information ; chaque état a une icône et un mot.
- **Aucun accès réseau sortant, aucun CDN.** Tout est servi depuis le disque.
- **Commits fréquents**, un par tâche minimum, message en français à l'impératif.

## Structure des fichiers

| Fichier | Responsabilité unique |
|---|---|
| `healthconsole/config.py` | Charger, valider et estimer le coût de la configuration |
| `healthconsole/plausibility.py` | Décider si une valeur de capteur est crédible |
| `healthconsole/findings.py` | Le type `Finding` et les gravités |
| `healthconsole/rules.py` | **Tous** les seuils, et eux seuls |
| `healthconsole/verdict.py` | Score explicable et hystérésis |
| `healthconsole/ring.py` | Tampon circulaire mémoire des 60 dernières minutes |
| `healthconsole/store.py` | SQLite : schéma, écriture, lecture, agrégation, rétention |
| `healthconsole/probes/__init__.py` | Registre des sondes et contrat commun |
| `healthconsole/probes/{cpu,memory,thermal,network,battery}.py` | Une sonde chacun |
| `healthconsole/scheduler.py` | Les deux cadences et leur orchestration |
| `healthconsole/server.py` | Routage HTTP, jeton, SSE |
| `healthconsole/texts_fr.py` | Libellés grand public |
| `web/{index.html,style.css,app.js}` | Mode Simple |
| `bin/health-console` | CLI : `run`, `config`, `status`, `prune` |

---

### Task 1: Socle du projet, harnais de test et configuration

**Files:**
- Create: `healthconsole/__init__.py`, `healthconsole/config.py`
- Create: `tests/__init__.py`, `tests/test_config.py`
- Create: `run-tests`

**Interfaces:**
- Consumes: rien (première tâche)
- Produces: `Retention`, `Sampling`, `Config` (dataclasses gelées), `ConfigError(ValueError)`, `load_config(path: Path | None) -> Config`, `estimate_db_bytes(cfg: Config, n_metrics: int) -> int`, `DEFAULT_CONFIG_PATH: Path`

- [ ] **Step 1: Créer l'arborescence et le lanceur de tests**

```bash
mkdir -p healthconsole/probes tests web bin
touch healthconsole/__init__.py healthconsole/probes/__init__.py tests/__init__.py
printf '#!/bin/sh\nexec python3 -m unittest discover -s tests -t . "$@"\n' > run-tests
chmod +x run-tests
```

- [ ] **Step 2: Écrire les tests qui échouent**

Fichier `tests/test_config.py` :

```python
import unittest
from pathlib import Path
import tempfile

from healthconsole.config import (
    Config, ConfigError, load_config, estimate_db_bytes,
)


def write_toml(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


class TestDefaults(unittest.TestCase):
    def test_absent_file_yields_defaults(self):
        cfg = load_config(Path("/inexistant/config.toml"))
        self.assertEqual(cfg.retention.raw_days, 2)
        self.assertEqual(cfg.retention.aggregate_days, 90)
        self.assertEqual(cfg.sampling.live_seconds, 2)
        self.assertEqual(cfg.sampling.store_seconds, 30)
        self.assertEqual(cfg.port, 8787)
        self.assertFalse(cfg.allow_remote_actions)

    def test_partial_file_keeps_other_defaults(self):
        p = write_toml("[retention]\nraw_days = 5\n")
        cfg = load_config(p)
        self.assertEqual(cfg.retention.raw_days, 5)
        self.assertEqual(cfg.retention.aggregate_days, 90)


class TestValidation(unittest.TestCase):
    def test_zero_days_is_refused(self):
        p = write_toml("[retention]\nraw_days = 0\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(p)
        self.assertIn("raw_days", str(ctx.exception))

    def test_raw_longer_than_aggregate_is_refused(self):
        p = write_toml("[retention]\nraw_days = 100\naggregate_days = 30\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(p)
        self.assertIn("raw_days", str(ctx.exception))
        self.assertIn("aggregate_days", str(ctx.exception))

    def test_store_seconds_must_be_multiple_of_live(self):
        p = write_toml("[sampling]\nlive_seconds = 2\nstore_seconds = 25\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(p)
        self.assertIn("store_seconds", str(ctx.exception))

    def test_store_seconds_capped_at_300(self):
        p = write_toml("[sampling]\nlive_seconds = 2\nstore_seconds = 600\n")
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_non_integer_days_is_refused(self):
        p = write_toml('[retention]\nraw_days = "deux"\n')
        with self.assertRaises(ConfigError) as ctx:
            load_config(p)
        self.assertIn("raw_days", str(ctx.exception))

    def test_malformed_toml_names_the_file(self):
        p = write_toml("[retention\nraw_days = 2\n")
        with self.assertRaises(ConfigError) as ctx:
            load_config(p)
        self.assertIn(str(p), str(ctx.exception))


class TestEstimate(unittest.TestCase):
    def test_default_config_estimates_about_32_MB(self):
        size = estimate_db_bytes(Config(), n_metrics=25)
        self.assertGreater(size, 25_000_000)
        self.assertLess(size, 40_000_000)

    def test_estimate_grows_with_retention(self):
        small = estimate_db_bytes(Config(), n_metrics=25)
        from healthconsole.config import Retention
        big = estimate_db_bytes(
            Config(retention=Retention(raw_days=30, aggregate_days=365)),
            n_metrics=25,
        )
        self.assertGreater(big, small * 5)

    def test_slower_store_rate_shrinks_the_database(self):
        from healthconsole.config import Sampling
        fast = estimate_db_bytes(Config(), n_metrics=25)
        slow = estimate_db_bytes(
            Config(sampling=Sampling(live_seconds=2, store_seconds=300)),
            n_metrics=25,
        )
        self.assertLess(slow, fast)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.config'`

- [ ] **Step 4: Écrire l'implémentation minimale**

Fichier `healthconsole/config.py` :

```python
"""Chargement, validation et chiffrage de la configuration.

Une configuration invalide arrête le service en nommant le champ fautif.
Démarrer en ignorant silencieusement un réglage erroné est un piège.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "health-console" / "config.toml"

# Coût moyen d'une ligne de métrique, index compris, clé normalisée en entier.
BYTES_PER_METRIC_ROW = 40
# Poids des tables non métriques (snapshots, événements, audit) en régime.
OVERHEAD_BYTES = 3_000_000
AGGREGATE_PERIOD_SECONDS = 300
SECONDS_PER_DAY = 86_400


class ConfigError(ValueError):
    """Configuration invalide. Le message nomme toujours le champ fautif."""


@dataclass(frozen=True)
class Retention:
    raw_days: int = 2
    aggregate_days: int = 90
    snapshot_days: int = 7
    event_days: int = 365
    audit_days: int = 365


@dataclass(frozen=True)
class Sampling:
    live_seconds: int = 2
    store_seconds: int = 30


@dataclass(frozen=True)
class Config:
    retention: Retention = field(default_factory=Retention)
    sampling: Sampling = field(default_factory=Sampling)
    bind: str = "0.0.0.0"
    port: int = 8787
    allow_remote_actions: bool = False
    token: str = ""


def _int_field(table: dict, name: str, default: int, section: str) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"[{section}] {name} doit être un nombre entier de jours, "
            f"reçu : {value!r}"
        )
    return value


def load_config(path: Path | None = None) -> Config:
    path = DEFAULT_CONFIG_PATH if path is None else path
    data: dict = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} : fichier TOML illisible — {exc}") from exc

    r = data.get("retention", {})
    retention = Retention(
        raw_days=_int_field(r, "raw_days", 2, "retention"),
        aggregate_days=_int_field(r, "aggregate_days", 90, "retention"),
        snapshot_days=_int_field(r, "snapshot_days", 7, "retention"),
        event_days=_int_field(r, "event_days", 365, "retention"),
        audit_days=_int_field(r, "audit_days", 365, "retention"),
    )
    s = data.get("sampling", {})
    sampling = Sampling(
        live_seconds=_int_field(s, "live_seconds", 2, "sampling"),
        store_seconds=_int_field(s, "store_seconds", 30, "sampling"),
    )
    server = data.get("server", {})
    cfg = Config(
        retention=retention,
        sampling=sampling,
        bind=server.get("bind", "0.0.0.0"),
        port=_int_field(server, "port", 8787, "server"),
        allow_remote_actions=bool(server.get("allow_remote_actions", False)),
        token=str(server.get("token", "")),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    r, s = cfg.retention, cfg.sampling
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        if getattr(r, name) < 1:
            raise ConfigError(
                f"[retention] {name} doit valoir au moins 1 jour, "
                f"reçu : {getattr(r, name)}"
            )
    if r.raw_days > r.aggregate_days:
        raise ConfigError(
            f"[retention] raw_days ({r.raw_days}) ne peut pas dépasser "
            f"aggregate_days ({r.aggregate_days}) : conserver les mesures fines "
            f"plus longtemps que les moyennes n'a pas de sens."
        )
    if s.live_seconds < 1:
        raise ConfigError(
            f"[sampling] live_seconds doit valoir au moins 1, "
            f"reçu : {s.live_seconds}"
        )
    if s.store_seconds < s.live_seconds or s.store_seconds % s.live_seconds:
        raise ConfigError(
            f"[sampling] store_seconds ({s.store_seconds}) doit être un multiple "
            f"de live_seconds ({s.live_seconds})"
        )
    if s.store_seconds > AGGREGATE_PERIOD_SECONDS:
        raise ConfigError(
            f"[sampling] store_seconds ({s.store_seconds}) ne peut pas dépasser "
            f"{AGGREGATE_PERIOD_SECONDS} secondes"
        )
    if not 1 <= cfg.port <= 65535:
        raise ConfigError(f"[server] port hors plage : {cfg.port}")


def estimate_db_bytes(cfg: Config, n_metrics: int) -> int:
    """Taille projetée de la base, en octets, pour ce nombre de métriques."""
    raw_rows_per_day = SECONDS_PER_DAY / cfg.sampling.store_seconds * n_metrics
    agg_rows_per_day = SECONDS_PER_DAY / AGGREGATE_PERIOD_SECONDS * n_metrics
    return int(
        cfg.retention.raw_days * raw_rows_per_day * BYTES_PER_METRIC_ROW
        + cfg.retention.aggregate_days * agg_rows_per_day * BYTES_PER_METRIC_ROW
        + OVERHEAD_BYTES
    )
```

- [ ] **Step 5: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 6: Commit**

```bash
git add healthconsole tests run-tests
git commit -m "Ajouter le socle du projet et la configuration validée

Les durées de rétention sont exprimées en jours et validées au
démarrage : entier positif, cohérence raw/aggregate, cadence
d'écriture multiple de la cadence d'affichage. Une valeur invalide
arrête le service en nommant le champ fautif.

estimate_db_bytes chiffre la taille que les réglages impliquent, pour
que le coût soit annoncé et non subi."
```

---

### Task 2: Contrôle de plausibilité des capteurs

**Files:**
- Create: `healthconsole/plausibility.py`
- Create: `tests/test_plausibility.py`

**Interfaces:**
- Consumes: rien
- Produces: `RANGES: dict[str, tuple[float, float]]`, `is_plausible(kind: str, value: float | None) -> bool`, `sane(kind: str, value: float | None) -> float | None`, `battery_capacity_is_coherent(now: float, full: float, design: float) -> bool`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_plausibility.py` :

```python
import unittest

from healthconsole.plausibility import (
    is_plausible, sane, battery_capacity_is_coherent,
)


class TestRanges(unittest.TestCase):
    def test_percent_domain(self):
        self.assertTrue(is_plausible("percent", 0))
        self.assertTrue(is_plausible("percent", 100))
        self.assertFalse(is_plausible("percent", -1))
        self.assertFalse(is_plausible("percent", 46700))

    def test_temperature_domain(self):
        self.assertTrue(is_plausible("celsius", 77.0))
        self.assertFalse(is_plausible("celsius", -273.0))
        self.assertFalse(is_plausible("celsius", 200.0))

    def test_frequency_domain_in_hz(self):
        self.assertTrue(is_plausible("hertz", 3_400_000_000))
        self.assertFalse(is_plausible("hertz", 50_000_000))

    def test_none_is_never_plausible(self):
        self.assertFalse(is_plausible("percent", None))

    def test_unknown_kind_is_refused_rather_than_assumed_valid(self):
        self.assertFalse(is_plausible("inconnu", 42))


class TestSane(unittest.TestCase):
    def test_returns_value_when_plausible(self):
        self.assertEqual(sane("percent", 42.0), 42.0)

    def test_returns_none_when_implausible(self):
        self.assertIsNone(sane("percent", 46700))


class TestBatteryCoherence(unittest.TestCase):
    def test_healthy_battery_is_coherent(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=3_000_000, full=4_000_000, design=5_000_000))

    def test_full_slightly_above_is_tolerated(self):
        self.assertTrue(battery_capacity_is_coherent(
            now=4_100_000, full=4_000_000, design=5_000_000))

    def test_this_machine_reports_nonsense(self):
        # Valeurs réelles du portable HP cible : charge_now vaut 467 fois
        # charge_full. Le pilote ment ; on doit le détecter.
        self.assertFalse(battery_capacity_is_coherent(
            now=467_000, full=1_000, design=1_000))

    def test_full_above_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=6_000_000, design=5_000_000))

    def test_zero_design_is_incoherent(self):
        self.assertFalse(battery_capacity_is_coherent(
            now=1_000, full=1_000, design=0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.plausibility'`

- [ ] **Step 3: Écrire l'implémentation minimale**

Fichier `healthconsole/plausibility.py` :

```python
"""Le matériel ment. Pas par malveillance : pilotes approximatifs, firmwares
ACPI bâclés, unités incohérentes selon le constructeur.

Afficher un chiffre faux avec assurance est pire que d'admettre qu'on ne sait
pas — c'est ce qui détruit la confiance dans un outil de diagnostic. Toute
valeur passe donc par ici avant d'être affichée ou stockée.
"""

from __future__ import annotations

# Domaines de validité. Un type inconnu est refusé plutôt que supposé valide :
# oublier de déclarer un domaine ne doit pas ouvrir une porte silencieuse.
RANGES: dict[str, tuple[float, float]] = {
    "percent": (0.0, 100.0),
    "celsius": (-20.0, 125.0),
    "hertz": (100_000_000.0, 10_000_000_000.0),
    "bytes": (0.0, 1e15),
    "bytes_per_second": (0.0, 1e12),
    "seconds": (0.0, 1e10),
    "count": (0.0, 1e9),
    "load": (0.0, 1024.0),
}

# Un pilote peut annoncer une charge légèrement supérieure à la capacité
# pleine juste après une charge complète. 5 % de tolérance, pas davantage.
CAPACITY_TOLERANCE = 1.05


def is_plausible(kind: str, value: float | None) -> bool:
    if value is None:
        return False
    bounds = RANGES.get(kind)
    if bounds is None:
        return False
    low, high = bounds
    return low <= value <= high


def sane(kind: str, value: float | None) -> float | None:
    """La valeur si elle est crédible, sinon None. Jamais de repli à zéro :
    un zéro se lit comme une mesure, alors que None se lit comme une absence."""
    return value if is_plausible(kind, value) else None


def battery_capacity_is_coherent(now: float, full: float, design: float) -> bool:
    """Cohérence croisée des trois capacités batterie.

    Sur la machine cible, le pilote annonce charge_now = 467 000 pour
    charge_full = 1 000. Sans ce contrôle, la console afficherait
    « 0 % d'usure, chargée à 46 700 % ».
    """
    if design <= 0 or full <= 0 or now < 0:
        return False
    if full > design * CAPACITY_TOLERANCE:
        return False
    if now > full * CAPACITY_TOLERANCE:
        return False
    return True
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/plausibility.py tests/test_plausibility.py
git commit -m "Ajouter le contrôle de plausibilité des capteurs

Toute valeur passe par un domaine de validité avant affichage ou
stockage. Un type de grandeur non déclaré est refusé plutôt que
supposé valide.

La cohérence croisée des capacités batterie détecte le cas réel de la
machine cible, dont le pilote annonce une charge 467 fois supérieure à
la capacité pleine."
```

---

### Task 3: Constats, seuils et score explicable

**Files:**
- Create: `healthconsole/findings.py`, `healthconsole/rules.py`, `healthconsole/verdict.py`
- Create: `tests/test_verdict.py`

**Interfaces:**
- Consumes: rien
- Produces: `Severity` (énumération `OK`/`INFO`/`ATTENTION`/`URGENT`), `Finding` (dataclass gelée : `id`, `severity`, `titre`, `pourquoi`, `technique`, `action` optionnel), `PENALTIES: dict[Severity, int]`, `score(findings: list[Finding]) -> int`, `score_breakdown(findings) -> list[tuple[str, int]]`, `worst(findings) -> Severity`, et dans `rules.py` les constantes de seuils `DISK_*`, `CPU_TEMP_*`, `MEM_*`, `BATTERY_WEAR_*`, `LOAD_*`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_verdict.py` :

```python
import unittest

from healthconsole.findings import Finding, Severity
from healthconsole.verdict import score, score_breakdown, worst


def f(fid, severity):
    return Finding(
        id=fid, severity=severity, titre="t", pourquoi="p", technique="tech",
    )


class TestScore(unittest.TestCase):
    def test_no_finding_is_exactly_100(self):
        # Pas 94 « pour faire sérieux » : un score inexpliqué est un mensonge.
        self.assertEqual(score([]), 100)

    def test_ok_findings_cost_nothing(self):
        self.assertEqual(score([f("a", Severity.OK)]), 100)

    def test_info_costs_two(self):
        self.assertEqual(score([f("a", Severity.INFO)]), 98)

    def test_attention_costs_eight(self):
        self.assertEqual(score([f("a", Severity.ATTENTION)]), 92)

    def test_urgent_costs_twentyfive(self):
        self.assertEqual(score([f("a", Severity.URGENT)]), 75)

    def test_penalties_accumulate(self):
        self.assertEqual(
            score([f("a", Severity.URGENT), f("b", Severity.ATTENTION),
                   f("c", Severity.INFO)]),
            100 - 25 - 8 - 2,
        )

    def test_score_never_goes_below_zero(self):
        self.assertEqual(score([f(str(i), Severity.URGENT) for i in range(10)]), 0)


class TestBreakdown(unittest.TestCase):
    def test_every_lost_point_is_traceable(self):
        findings = [f("disque", Severity.ATTENTION), f("maj", Severity.INFO)]
        detail = score_breakdown(findings)
        self.assertEqual(detail, [("disque", 8), ("maj", 2)])
        self.assertEqual(100 - sum(p for _, p in detail), score(findings))

    def test_breakdown_is_empty_when_perfect(self):
        self.assertEqual(score_breakdown([]), [])

    def test_breakdown_is_sorted_by_cost(self):
        findings = [f("petit", Severity.INFO), f("gros", Severity.URGENT)]
        self.assertEqual([fid for fid, _ in score_breakdown(findings)],
                         ["gros", "petit"])


class TestWorst(unittest.TestCase):
    def test_worst_of_nothing_is_ok(self):
        self.assertIs(worst([]), Severity.OK)

    def test_worst_wins_over_milder(self):
        self.assertIs(worst([f("a", Severity.INFO), f("b", Severity.URGENT)]),
                      Severity.URGENT)


class TestFindingShape(unittest.TestCase):
    def test_all_public_texts_are_required(self):
        finding = f("a", Severity.INFO)
        self.assertTrue(finding.titre)
        self.assertTrue(finding.pourquoi)
        self.assertTrue(finding.technique)

    def test_finding_is_immutable(self):
        finding = f("a", Severity.INFO)
        with self.assertRaises(Exception):
            finding.titre = "modifié"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.findings'`

- [ ] **Step 3: Écrire `healthconsole/findings.py`**

```python
"""Le constat : l'unité de base de l'interprétation.

Quatre facettes, parce que les deux modes ont besoin de mots différents.
`titre` et `pourquoi` s'adressent au grand public ; `technique` à l'expert.
Aucune traduction à la volée, aucun jargon qui fuit vers le mode Simple.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    OK = 0
    INFO = 1
    ATTENTION = 2
    URGENT = 3


@dataclass(frozen=True, slots=True)
class Finding:
    id: str
    severity: Severity
    titre: str          # mode Simple — le constat en une phrase
    pourquoi: str       # mode Simple — pourquoi ça compte, sans jargon
    technique: str      # mode Expert — les chiffres bruts
    action: str | None = None   # identifiant d'action, renseigné au plan 3
```

- [ ] **Step 4: Écrire `healthconsole/rules.py`**

```python
"""Tous les seuils du projet, et eux seuls.

Ils vivent ici pour qu'en ajuster un n'impose pas de relire le projet.
Chaque seuil « haut » ouvre un constat, chaque seuil « bas » le referme :
l'écart entre les deux est l'hystérésis qui empêche le clignotement.
"""

# Disque — spec §7.5
DISK_ATTENTION_PCT = 80.0
DISK_ATTENTION_CLEAR_PCT = 75.0
DISK_URGENT_PCT = 92.0
DISK_URGENT_FREE_BYTES = 3 * 1024**3

# Température CPU
CPU_TEMP_ATTENTION_C = 85.0
CPU_TEMP_ATTENTION_CLEAR_C = 80.0
CPU_TEMP_URGENT_C = 95.0
CPU_TEMP_SUSTAIN_SECONDS = 300

# Charge processeur
CPU_USAGE_ATTENTION_PCT = 90.0
CPU_USAGE_ATTENTION_CLEAR_PCT = 70.0
CPU_USAGE_SUSTAIN_SECONDS = 300

# Mémoire
MEM_ATTENTION_AVAILABLE_PCT = 15.0
MEM_ATTENTION_CLEAR_PCT = 25.0
MEM_SWAP_ACTIVE_BYTES = 64 * 1024**2

# Batterie
BATTERY_WEAR_ATTENTION_PCT = 30.0
BATTERY_WEAR_URGENT_PCT = 50.0
BATTERY_LOW_PCT = 10.0

# Charge système, rapportée au nombre de cœurs
LOAD_ATTENTION_RATIO = 1.5
LOAD_ATTENTION_CLEAR_RATIO = 1.0

# Pénalités de score — spec §7.2
PENALTY_INFO = 2
PENALTY_ATTENTION = 8
PENALTY_URGENT = 25
```

- [ ] **Step 5: Écrire `healthconsole/verdict.py`**

```python
"""Score explicable.

Le score est une conséquence des constats, jamais un oracle. Chaque point
perdu remonte à un constat nommé : c'est le refus explicite du chiffre magique
des logiciels d'antivirus, que personne ne sait expliquer.
"""

from __future__ import annotations

from healthconsole import rules
from healthconsole.findings import Finding, Severity

PENALTIES: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: rules.PENALTY_INFO,
    Severity.ATTENTION: rules.PENALTY_ATTENTION,
    Severity.URGENT: rules.PENALTY_URGENT,
}


def score_breakdown(findings: list[Finding]) -> list[tuple[str, int]]:
    """(identifiant du constat, points retirés), du plus coûteux au moins."""
    detail = [(f.id, PENALTIES[f.severity]) for f in findings
              if PENALTIES[f.severity] > 0]
    detail.sort(key=lambda pair: pair[1], reverse=True)
    return detail


def score(findings: list[Finding]) -> int:
    lost = sum(points for _, points in score_breakdown(findings))
    return max(0, 100 - lost)


def worst(findings: list[Finding]) -> Severity:
    return max((f.severity for f in findings), default=Severity.OK)
```

- [ ] **Step 6: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 7: Commit**

```bash
git add healthconsole/findings.py healthconsole/rules.py healthconsole/verdict.py tests/test_verdict.py
git commit -m "Ajouter les constats, les seuils centralisés et le score

Le score part de 100 et chaque point perdu remonte à un constat nommé
via score_breakdown. Aucun constat donne exactement 100.

Tous les seuils sont réunis dans rules.py, avec pour chacun un seuil
d'ouverture et un seuil de fermeture distinct qui portera l'hystérésis."
```

---

### Task 4: Hystérésis et suivi d'événements

**Files:**
- Modify: `healthconsole/verdict.py` (ajout en fin de fichier)
- Create: `tests/test_hysteresis.py`

**Interfaces:**
- Consumes: `healthconsole.rules`
- Produces: `HysteresisTracker` avec `update(key: str, value: float | None, high: float, low: float, now: float, sustain_seconds: float = 0.0) -> bool`, `is_open(key: str) -> bool`, `opened_at(key: str) -> float | None`, `snapshot() -> dict[str, float]`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_hysteresis.py` :

```python
import unittest

from healthconsole.verdict import HysteresisTracker


class TestFlapping(unittest.TestCase):
    def setUp(self):
        self.t = HysteresisTracker()

    def test_brief_spike_does_not_open(self):
        # Une compilation qui charge le CPU 3 secondes ne doit pas faire
        # virer la console au rouge : elle ne serait plus jamais crue.
        self.assertFalse(self.t.update("cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.t.update("cpu", 95, 90, 70, now=3, sustain_seconds=300))
        self.assertFalse(self.t.update("cpu", 10, 90, 70, now=4, sustain_seconds=300))

    def test_sustained_breach_opens(self):
        self.assertFalse(self.t.update("cpu", 95, 90, 70, now=0, sustain_seconds=300))
        self.assertFalse(self.t.update("cpu", 95, 90, 70, now=299, sustain_seconds=300))
        self.assertTrue(self.t.update("cpu", 95, 90, 70, now=300, sustain_seconds=300))

    def test_without_sustain_opens_immediately(self):
        self.assertTrue(self.t.update("disk", 85, 80, 75, now=0))

    def test_stays_open_between_low_and_high(self):
        self.t.update("disk", 85, 80, 75, now=0)
        self.assertTrue(self.t.update("disk", 78, 80, 75, now=10))

    def test_closes_only_below_low(self):
        self.t.update("disk", 85, 80, 75, now=0)
        self.assertFalse(self.t.update("disk", 74, 80, 75, now=10))

    def test_stays_closed_between_low_and_high(self):
        self.assertFalse(self.t.update("disk", 78, 80, 75, now=0))

    def test_sustain_timer_resets_when_value_drops(self):
        self.t.update("cpu", 95, 90, 70, now=0, sustain_seconds=300)
        self.t.update("cpu", 50, 90, 70, now=100, sustain_seconds=300)
        self.t.update("cpu", 95, 90, 70, now=200, sustain_seconds=300)
        self.assertFalse(self.t.update("cpu", 95, 90, 70, now=450,
                                       sustain_seconds=300))
        self.assertTrue(self.t.update("cpu", 95, 90, 70, now=500,
                                      sustain_seconds=300))


class TestUnavailableValues(unittest.TestCase):
    def test_none_never_opens(self):
        t = HysteresisTracker()
        self.assertFalse(t.update("bat", None, 30, 25, now=0))

    def test_none_does_not_close_an_open_state(self):
        # Perdre la mesure n'est pas la preuve que le problème a disparu.
        t = HysteresisTracker()
        t.update("bat", 40, 30, 25, now=0)
        self.assertTrue(t.update("bat", None, 30, 25, now=10))


class TestIntrospection(unittest.TestCase):
    def test_opened_at_records_the_moment(self):
        t = HysteresisTracker()
        t.update("disk", 85, 80, 75, now=1234)
        self.assertEqual(t.opened_at("disk"), 1234)

    def test_opened_at_is_none_when_closed(self):
        t = HysteresisTracker()
        self.assertIsNone(t.opened_at("disk"))

    def test_snapshot_lists_open_keys_with_their_start(self):
        t = HysteresisTracker()
        t.update("disk", 85, 80, 75, now=100)
        t.update("cpu", 10, 90, 70, now=100)
        self.assertEqual(t.snapshot(), {"disk": 100})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'HysteresisTracker'`

- [ ] **Step 3: Ajouter l'implémentation à la fin de `healthconsole/verdict.py`**

```python
class HysteresisTracker:
    """Empêche le clignotement des constats.

    Un seuil doit être franchi *et tenu* pour ouvrir un constat ; il faut
    repasser sous un seuil bas distinct pour le refermer. Sans cela, une
    pointe de charge de trois secondes ferait virer la console au rouge et
    plus personne ne la croirait.
    """

    def __init__(self) -> None:
        self._open: dict[str, float] = {}      # clé -> instant d'ouverture
        self._breach_since: dict[str, float] = {}  # clé -> début du dépassement

    def update(self, key: str, value: float | None, high: float, low: float,
               now: float, sustain_seconds: float = 0.0) -> bool:
        if value is None:
            # Perdre la mesure n'est pas la preuve que le problème a disparu :
            # on conserve l'état sans jamais l'ouvrir sur une absence.
            return key in self._open

        if key in self._open:
            if value < low:
                del self._open[key]
                self._breach_since.pop(key, None)
            return key in self._open

        if value >= high:
            since = self._breach_since.setdefault(key, now)
            if now - since >= sustain_seconds:
                self._open[key] = now
                self._breach_since.pop(key, None)
        else:
            self._breach_since.pop(key, None)
        return key in self._open

    def is_open(self, key: str) -> bool:
        return key in self._open

    def opened_at(self, key: str) -> float | None:
        return self._open.get(key)

    def snapshot(self) -> dict[str, float]:
        return dict(self._open)
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/verdict.py tests/test_hysteresis.py
git commit -m "Ajouter l'hystérésis pour empêcher le clignotement des constats

Un seuil doit être franchi et tenu pour ouvrir un constat, et il faut
repasser sous un seuil bas distinct pour le refermer.

Une mesure absente ne peut ni ouvrir un constat ni en fermer un :
perdre la mesure n'est pas la preuve que le problème a disparu."
```

---

### Task 5: Tampon circulaire mémoire

**Files:**
- Create: `healthconsole/ring.py`
- Create: `tests/test_ring.py`

**Interfaces:**
- Consumes: rien
- Produces: `Ring(window_seconds: int = 3600, live_seconds: int = 2)` avec `push(key: str, value: float, ts: float) -> None`, `series(key: str) -> list[tuple[float, float]]`, `aggregate(key: str, since: float) -> tuple[float, float, float] | None`, `keys() -> list[str]`, `capacity: int`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_ring.py` :

```python
import unittest

from healthconsole.ring import Ring


class TestCapacity(unittest.TestCase):
    def test_capacity_matches_window_over_resolution(self):
        self.assertEqual(Ring(window_seconds=3600, live_seconds=2).capacity, 1800)

    def test_oldest_points_are_dropped(self):
        r = Ring(window_seconds=10, live_seconds=2)  # 5 points
        for i in range(8):
            r.push("cpu", float(i), ts=float(i))
        self.assertEqual([v for _, v in r.series("cpu")], [3, 4, 5, 6, 7])


class TestSeries(unittest.TestCase):
    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(Ring().series("inexistant"), [])

    def test_keys_lists_what_was_pushed(self):
        r = Ring()
        r.push("cpu", 1.0, ts=0)
        r.push("mem", 2.0, ts=0)
        self.assertEqual(sorted(r.keys()), ["cpu", "mem"])


class TestAggregate(unittest.TestCase):
    def test_returns_avg_min_max(self):
        r = Ring()
        for i, v in enumerate([10.0, 20.0, 30.0]):
            r.push("cpu", v, ts=float(i))
        self.assertEqual(r.aggregate("cpu", since=0.0), (20.0, 10.0, 30.0))

    def test_only_considers_points_since(self):
        r = Ring()
        for i, v in enumerate([10.0, 20.0, 30.0]):
            r.push("cpu", v, ts=float(i))
        self.assertEqual(r.aggregate("cpu", since=1.0), (25.0, 20.0, 30.0))

    def test_empty_window_returns_none(self):
        # None, pas zéro : un zéro se lirait comme une mesure.
        r = Ring()
        r.push("cpu", 10.0, ts=0.0)
        self.assertIsNone(r.aggregate("cpu", since=100.0))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(Ring().aggregate("inexistant", since=0.0))


class TestMemoryFootprint(unittest.TestCase):
    def test_stays_bounded_under_sustained_push(self):
        r = Ring(window_seconds=3600, live_seconds=2)
        for i in range(50_000):
            r.push("cpu", float(i), ts=float(i))
        self.assertEqual(len(r.series("cpu")), 1800)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.ring'`

- [ ] **Step 3: Écrire `healthconsole/ring.py`**

```python
"""Tampon circulaire des 60 dernières minutes, en mémoire.

Afficher finement et conserver longtemps sont deux besoins distincts, et les
confondre fait exploser la base. Le direct à 2 secondes vit ici — environ
360 Kio pour 25 métriques — et seule une valeur agrégée toutes les 30 secondes
part sur le disque.
"""

from __future__ import annotations

from collections import deque


class Ring:
    def __init__(self, window_seconds: int = 3600, live_seconds: int = 2) -> None:
        if live_seconds < 1:
            raise ValueError("live_seconds doit valoir au moins 1")
        self.window_seconds = window_seconds
        self.live_seconds = live_seconds
        self.capacity = max(1, window_seconds // live_seconds)
        self._data: dict[str, deque[tuple[float, float]]] = {}

    def push(self, key: str, value: float, ts: float) -> None:
        buf = self._data.get(key)
        if buf is None:
            buf = self._data[key] = deque(maxlen=self.capacity)
        buf.append((ts, value))

    def series(self, key: str) -> list[tuple[float, float]]:
        return list(self._data.get(key, ()))

    def keys(self) -> list[str]:
        return list(self._data)

    def aggregate(self, key: str, since: float
                  ) -> tuple[float, float, float] | None:
        """(moyenne, minimum, maximum) depuis `since`, ou None si aucun point.

        None et non zéro : un zéro se lirait comme une mesure réelle.
        """
        buf = self._data.get(key)
        if not buf:
            return None
        values = [v for ts, v in buf if ts >= since]
        if not values:
            return None
        return (sum(values) / len(values), min(values), max(values))
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/ring.py tests/test_ring.py
git commit -m "Ajouter le tampon circulaire mémoire du direct

Le direct à 2 secondes vit en mémoire sur une fenêtre de 60 minutes et
n'atteint jamais le disque. C'est ce qui permet de servir les
sparklines sans écrire 80 Mo par jour en base.

L'agrégation renvoie None sur une fenêtre vide plutôt que zéro : un
zéro se lirait comme une mesure."
```

---

### Task 6: Base de données — schéma, écriture, lecture

**Files:**
- Create: `healthconsole/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: rien
- Produces: `Store(path: Path | str)` avec `close() -> None`, `key_id(key: str) -> int`, `write_metrics(ts: int, rows: list[tuple[str, float, float, float]]) -> None`, `read_series(key: str, since: int, until: int, table: str = "metric") -> list[tuple[int, float]]`, `count_rows(table: str) -> int`, `distinct_metric_count() -> int`, `db_bytes() -> int`, `oldest_ts(table: str) -> int | None`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_store.py` :

```python
import unittest

from healthconsole.store import Store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()


class TestSchema(StoreCase):
    def test_tables_exist(self):
        names = {row[0] for row in self.s.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"metric_key", "metric", "metric_5m", "snapshot", "event",
             "action_run"},
            names,
        )

    def test_reopening_is_idempotent(self):
        self.s._create_schema()
        self.assertEqual(self.s.count_rows("metric"), 0)


class TestKeyNormalisation(StoreCase):
    def test_same_key_yields_same_id(self):
        self.assertEqual(self.s.key_id("cpu.usage"), self.s.key_id("cpu.usage"))

    def test_different_keys_yield_different_ids(self):
        self.assertNotEqual(self.s.key_id("cpu.usage"), self.s.key_id("mem.used"))

    def test_ids_are_cached_without_extra_rows(self):
        for _ in range(10):
            self.s.key_id("cpu.usage")
        self.assertEqual(self.s.count_rows("metric_key"), 1)


class TestWriteRead(StoreCase):
    def test_written_points_come_back(self):
        self.s.write_metrics(1000, [("cpu.usage", 20.0, 10.0, 30.0)])
        self.assertEqual(self.s.read_series("cpu.usage", 0, 2000),
                         [(1000, 20.0)])

    def test_range_is_inclusive_and_bounded(self):
        for ts in (100, 200, 300):
            self.s.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual([ts for ts, _ in self.s.read_series("cpu.usage", 200, 300)],
                         [200, 300])

    def test_results_are_ordered_by_time(self):
        for ts in (300, 100, 200):
            self.s.write_metrics(ts, [("cpu.usage", float(ts), 0.0, 0.0)])
        self.assertEqual([ts for ts, _ in self.s.read_series("cpu.usage", 0, 999)],
                         [100, 200, 300])

    def test_unknown_key_yields_empty_series(self):
        self.assertEqual(self.s.read_series("inexistant", 0, 999), [])

    def test_batch_write_is_one_transaction(self):
        self.s.write_metrics(1000, [
            ("cpu.usage", 1.0, 1.0, 1.0),
            ("mem.available", 2.0, 2.0, 2.0),
        ])
        self.assertEqual(self.s.count_rows("metric"), 2)

    def test_empty_batch_writes_nothing(self):
        self.s.write_metrics(1000, [])
        self.assertEqual(self.s.count_rows("metric"), 0)


class TestIntrospection(StoreCase):
    def test_distinct_metric_count(self):
        self.s.write_metrics(1, [("a", 1.0, 1.0, 1.0), ("b", 1.0, 1.0, 1.0)])
        self.s.write_metrics(2, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.s.distinct_metric_count(), 2)

    def test_oldest_ts_is_none_when_empty(self):
        self.assertIsNone(self.s.oldest_ts("metric"))

    def test_oldest_ts_reports_the_first_point(self):
        self.s.write_metrics(500, [("a", 1.0, 1.0, 1.0)])
        self.s.write_metrics(100, [("a", 1.0, 1.0, 1.0)])
        self.assertEqual(self.s.oldest_ts("metric"), 100)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.store'`

- [ ] **Step 3: Écrire `healthconsole/store.py`**

```python
"""Persistance SQLite.

Les clés de métriques sont normalisées en entiers : stocker la chaîne
« net.enp0s25.rx_bps » sur chaque ligne coûterait plus cher que la mesure
elle-même.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_key (
    id  INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS metric (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS metric_5m (
    ts INTEGER NOT NULL, key_id INTEGER NOT NULL,
    avg REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
    ts INTEGER NOT NULL, probe TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL, severity TEXT NOT NULL,
    opened_ts INTEGER NOT NULL, closed_ts INTEGER
);
CREATE TABLE IF NOT EXISTS action_run (
    id TEXT PRIMARY KEY, ts INTEGER NOT NULL, action_id TEXT NOT NULL,
    source TEXT NOT NULL, exit_code INTEGER, duration_ms INTEGER, output TEXT
);
CREATE INDEX IF NOT EXISTS metric_key_ts ON metric(key_id, ts);
CREATE INDEX IF NOT EXISTS metric_5m_key_ts ON metric_5m(key_id, ts);
CREATE INDEX IF NOT EXISTS snapshot_ts ON snapshot(ts);
"""

METRIC_TABLES = ("metric", "metric_5m")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._key_cache: dict[str, int] = {}
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def key_id(self, key: str) -> int:
        cached = self._key_cache.get(key)
        if cached is not None:
            return cached
        cur = self.conn.execute("SELECT id FROM metric_key WHERE key = ?", (key,))
        row = cur.fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO metric_key(key) VALUES (?)", (key,))
            self.conn.commit()
            new_id = int(cur.lastrowid)
        else:
            new_id = int(row[0])
        self._key_cache[key] = new_id
        return new_id

    def write_metrics(self, ts: int,
                      rows: list[tuple[str, float, float, float]]) -> None:
        if not rows:
            return
        payload = [(ts, self.key_id(k), a, mn, mx) for k, a, mn, mx in rows]
        self.conn.executemany(
            "INSERT INTO metric(ts, key_id, avg, min, max) VALUES (?,?,?,?,?)",
            payload,
        )
        self.conn.commit()

    def read_series(self, key: str, since: int, until: int,
                    table: str = "metric") -> list[tuple[int, float]]:
        if table not in METRIC_TABLES:
            raise ValueError(f"table inconnue : {table}")
        cur = self.conn.execute(
            f"SELECT ts, avg FROM {table} "
            "WHERE key_id = (SELECT id FROM metric_key WHERE key = ?) "
            "AND ts BETWEEN ? AND ? ORDER BY ts",
            (key, since, until),
        )
        return [(int(ts), float(v)) for ts, v in cur.fetchall()]

    def count_rows(self, table: str) -> int:
        if table not in METRIC_TABLES + ("metric_key", "snapshot", "event",
                                         "action_run"):
            raise ValueError(f"table inconnue : {table}")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def distinct_metric_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(DISTINCT key_id) FROM metric").fetchone()[0])

    def oldest_ts(self, table: str) -> int | None:
        if table not in METRIC_TABLES:
            raise ValueError(f"table inconnue : {table}")
        row = self.conn.execute(f"SELECT MIN(ts) FROM {table}").fetchone()
        return None if row[0] is None else int(row[0])

    def db_bytes(self) -> int:
        if self.path == ":memory:":
            page = self.conn.execute("PRAGMA page_count").fetchone()[0]
            size = self.conn.execute("PRAGMA page_size").fetchone()[0]
            return int(page) * int(size)
        return sum(
            Path(self.path + suffix).stat().st_size
            for suffix in ("", "-wal", "-shm")
            if Path(self.path + suffix).exists()
        )
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/store.py tests/test_store.py
git commit -m "Ajouter le schéma SQLite et les écritures de métriques

Les clés de métriques sont normalisées en entiers via metric_key :
stocker la chaîne sur chaque ligne coûterait plus cher que la mesure.

Base en WAL, synchronous NORMAL, schéma idempotent."
```

---

### Task 7: Base de données — agrégation, rétention configurable, purge

**Files:**
- Modify: `healthconsole/store.py` (ajout de méthodes)
- Create: `tests/test_retention.py`

**Interfaces:**
- Consumes: `healthconsole.config.Config`, `Store` de la tâche 6
- Produces: sur `Store` — `aggregate_5m(now: int, period: int = 300) -> int`, `prune(cfg: Config, now: int) -> dict[str, int]`, `available_depth_seconds(table: str, now: int) -> int`, `vacuum() -> None`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_retention.py` :

```python
import unittest

from healthconsole.config import Config, Retention
from healthconsole.store import Store

DAY = 86_400


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def fill_days(self, days, now, key="cpu.usage", step=3600):
        """Un point par heure sur `days` jours, jusqu'à `now`."""
        ts = now - days * DAY
        while ts <= now:
            self.s.write_metrics(ts, [(key, 50.0, 40.0, 60.0)])
            ts += step


class TestAggregation(RetentionCase):
    def test_raw_points_become_five_minute_buckets(self):
        now = 10 * DAY
        for i in range(10):                       # 10 points sur 300 s
            self.s.write_metrics(now + i * 30, [("cpu.usage", float(i), 0.0, 9.0)])
        written = self.s.aggregate_5m(now=now + 600)
        self.assertGreater(written, 0)
        rows = self.s.read_series("cpu.usage", 0, now + 600, table="metric_5m")
        self.assertEqual(len(rows), 1)

    def test_bucket_average_is_correct(self):
        now = 10 * DAY
        for i, v in enumerate([10.0, 20.0, 30.0]):
            self.s.write_metrics(now + i * 30, [("cpu.usage", v, v, v)])
        self.s.aggregate_5m(now=now + 600)
        rows = self.s.read_series("cpu.usage", 0, now + 600, table="metric_5m")
        self.assertAlmostEqual(rows[0][1], 20.0)

    def test_aggregation_is_idempotent(self):
        now = 10 * DAY
        for i in range(6):
            self.s.write_metrics(now + i * 30, [("cpu.usage", 1.0, 1.0, 1.0)])
        self.s.aggregate_5m(now=now + 600)
        before = self.s.count_rows("metric_5m")
        self.s.aggregate_5m(now=now + 600)
        self.assertEqual(self.s.count_rows("metric_5m"), before)


class TestPrune(RetentionCase):
    def test_raw_older_than_raw_days_is_removed(self):
        now = 100 * DAY
        self.fill_days(10, now)
        cfg = Config(retention=Retention(raw_days=2, aggregate_days=90))
        self.s.prune(cfg, now=now)
        oldest = self.s.oldest_ts("metric")
        self.assertGreaterEqual(oldest, now - 2 * DAY)

    def test_prune_reports_what_it_deleted(self):
        now = 100 * DAY
        self.fill_days(10, now)
        cfg = Config(retention=Retention(raw_days=2, aggregate_days=90))
        deleted = self.s.prune(cfg, now=now)
        self.assertIn("metric", deleted)
        self.assertGreater(deleted["metric"], 0)

    def test_reducing_retention_purges_more(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.s.prune(Config(retention=Retention(raw_days=20)), now=now)
        after_20 = self.s.count_rows("metric")
        self.s.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLess(self.s.count_rows("metric"), after_20)

    def test_increasing_retention_resurrects_nothing(self):
        # L'historique repart de la date du changement. La console doit
        # afficher la profondeur réellement disponible, jamais celle demandée.
        now = 100 * DAY
        self.fill_days(30, now)
        self.s.prune(Config(retention=Retention(raw_days=2)), now=now)
        remaining = self.s.count_rows("metric")
        self.s.prune(Config(retention=Retention(raw_days=90)), now=now)
        self.assertEqual(self.s.count_rows("metric"), remaining)

    def test_nothing_is_deleted_when_everything_is_recent(self):
        now = 100 * DAY
        self.fill_days(1, now)
        deleted = self.s.prune(Config(retention=Retention(raw_days=30)), now=now)
        self.assertEqual(deleted["metric"], 0)


class TestAvailableDepth(RetentionCase):
    def test_depth_is_zero_when_empty(self):
        self.assertEqual(self.s.available_depth_seconds("metric", now=DAY), 0)

    def test_depth_reflects_what_is_actually_stored(self):
        now = 100 * DAY
        self.fill_days(3, now)
        depth = self.s.available_depth_seconds("metric", now=now)
        self.assertAlmostEqual(depth / DAY, 3.0, places=1)

    def test_depth_shrinks_after_prune(self):
        now = 100 * DAY
        self.fill_days(30, now)
        self.s.prune(Config(retention=Retention(raw_days=2)), now=now)
        self.assertLessEqual(self.s.available_depth_seconds("metric", now=now),
                             2 * DAY + 3600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'aggregate_5m'`

- [ ] **Step 3: Ajouter les méthodes à la fin de la classe `Store`**

```python
    # --- Agrégation et rétention -------------------------------------

    def aggregate_5m(self, now: int, period: int = 300) -> int:
        """Replie les points bruts en seaux de `period` secondes.

        Idempotent : un seau déjà écrit n'est pas réécrit. Seuls les seaux
        entièrement révolus sont traités, pour ne pas figer une moyenne
        partielle.
        """
        boundary = (now // period) * period
        rows = self.conn.execute(
            "SELECT (ts / ?) * ? AS bucket, key_id, AVG(avg), MIN(min), MAX(max) "
            "FROM metric WHERE ts < ? GROUP BY bucket, key_id",
            (period, period, boundary),
        ).fetchall()
        written = 0
        for bucket, key_id, avg, mn, mx in rows:
            exists = self.conn.execute(
                "SELECT 1 FROM metric_5m WHERE ts = ? AND key_id = ?",
                (bucket, key_id),
            ).fetchone()
            if exists:
                continue
            self.conn.execute(
                "INSERT INTO metric_5m(ts, key_id, avg, min, max) "
                "VALUES (?,?,?,?,?)",
                (bucket, key_id, avg, mn, mx),
            )
            written += 1
        self.conn.commit()
        return written

    def prune(self, cfg, now: int) -> dict[str, int]:
        """Applique les durées de rétention. Renvoie le nombre de lignes
        supprimées par table.

        Augmenter une durée ne ressuscite rien : la suppression est
        définitive, et la console affichera la profondeur réellement
        disponible plutôt que celle demandée.
        """
        day = 86_400
        cutoffs = {
            "metric": now - cfg.retention.raw_days * day,
            "metric_5m": now - cfg.retention.aggregate_days * day,
            "snapshot": now - cfg.retention.snapshot_days * day,
            "action_run": now - cfg.retention.audit_days * day,
        }
        deleted: dict[str, int] = {}
        for table, cutoff in cutoffs.items():
            cur = self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = cur.rowcount if cur.rowcount > 0 else 0
        event_cutoff = now - cfg.retention.event_days * day
        cur = self.conn.execute(
            "DELETE FROM event WHERE closed_ts IS NOT NULL AND closed_ts < ?",
            (event_cutoff,),
        )
        deleted["event"] = cur.rowcount if cur.rowcount > 0 else 0
        self.conn.commit()
        return deleted

    def available_depth_seconds(self, table: str, now: int) -> int:
        oldest = self.oldest_ts(table)
        return 0 if oldest is None else max(0, now - oldest)

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")
        self.conn.commit()
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/store.py tests/test_retention.py
git commit -m "Ajouter l'agrégation 5 min et la purge de rétention

L'horloge est injectée dans aggregate_5m et prune : aucun test n'attend
48 heures.

Les tests vérifient qu'augmenter une durée de rétention ne ressuscite
rien, et que available_depth_seconds reflète ce qui est réellement
stocké et non ce qui est demandé en configuration."
```

---

### Task 8: Contrat des sondes, sondes CPU et mémoire

**Files:**
- Modify: `healthconsole/probes/__init__.py`
- Create: `healthconsole/probes/cpu.py`, `healthconsole/probes/memory.py`
- Create: `tests/test_probes_cpu.py`, `tests/test_probes_memory.py`

**Interfaces:**
- Consumes: `healthconsole.findings`, `healthconsole.rules`, `healthconsole.plausibility`
- Produces: dans `probes/__init__.py` — `FAST: str = "fast"`, `SLOW: str = "slow"`, `EvalContext(sustained: dict[str, bool], cores: int)`, `load_probes(cadence: str | None = None) -> list`, `PROBE_MODULES: tuple[str, ...]`. Chaque sonde expose `NAME: str`, `CADENCE: str`, `collect() -> dict`, `metrics(sample: dict) -> dict[str, float]`, `evaluate(sample: dict, ctx: EvalContext) -> list[Finding]`

**Contrat commun, valable pour toutes les sondes du projet :**
- `collect()` mesure et ne juge jamais. Elle renvoie toujours un dict contenant `status`, valant `"ok"`, `"unavailable"` (avec `reason`) ou `"incoherent"`.
- `metrics()` extrait du relevé les seules valeurs numériques à historiser.
- `evaluate()` est **pure** : ni horloge, ni système de fichiers, ni sous-processus. Tout ce dont elle a besoin arrive par `sample` et `ctx`.

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_probes_cpu.py` :

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext, FAST, load_probes
from healthconsole.probes import cpu


def ctx(sustained=None, cores=4):
    return EvalContext(sustained=sustained or {}, cores=cores)


class TestRegistry(unittest.TestCase):
    def test_cpu_is_registered_as_fast(self):
        names = [m.NAME for m in load_probes(FAST)]
        self.assertIn("cpu", names)

    def test_every_probe_honours_the_contract(self):
        for module in load_probes():
            for attr in ("NAME", "CADENCE", "collect", "metrics", "evaluate"):
                self.assertTrue(hasattr(module, attr),
                                f"{module.__name__} n'expose pas {attr}")


class TestMetrics(unittest.TestCase):
    def test_extracts_numeric_series(self):
        sample = {"status": "ok", "usage_pct": 23.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertEqual(cpu.metrics(sample),
                         {"cpu.usage": 23.0, "cpu.freq": 3.4e9, "load.1": 0.62})

    def test_implausible_values_are_not_historised(self):
        sample = {"status": "ok", "usage_pct": 4700.0, "freq_hz": 3.4e9,
                  "load1": 0.62, "cores": 4}
        self.assertNotIn("cpu.usage", cpu.metrics(sample))

    def test_unavailable_sample_yields_no_metric(self):
        self.assertEqual(cpu.metrics({"status": "unavailable",
                                      "reason": "x"}), {})


class TestEvaluate(unittest.TestCase):
    def test_idle_machine_produces_no_finding(self):
        sample = {"status": "ok", "usage_pct": 12.0, "freq_hz": 1.6e9,
                  "load1": 0.3, "cores": 4}
        self.assertEqual(cpu.evaluate(sample, ctx()), [])

    def test_spike_alone_produces_no_finding(self):
        # Sans confirmation dans la durée par l'ordonnanceur, une pointe
        # ne doit rien déclencher.
        sample = {"status": "ok", "usage_pct": 99.0, "freq_hz": 3.4e9,
                  "load1": 4.0, "cores": 4}
        self.assertEqual(cpu.evaluate(sample, ctx()), [])

    def test_sustained_load_produces_attention(self):
        sample = {"status": "ok", "usage_pct": 99.0, "freq_hz": 3.4e9,
                  "load1": 4.0, "cores": 4}
        findings = cpu.evaluate(sample, ctx({"cpu.usage_high": True}))
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)

    def test_finding_speaks_plainly(self):
        sample = {"status": "ok", "usage_pct": 99.0, "freq_hz": 3.4e9,
                  "load1": 4.0, "cores": 4}
        f = cpu.evaluate(sample, ctx({"cpu.usage_high": True}))[0]
        self.assertTrue(f.titre and f.pourquoi and f.technique)
        for jargon in ("CPU", "load average", "%"):
            self.assertNotIn(jargon, f.pourquoi)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(
            cpu.evaluate({"status": "unavailable", "reason": "x"}, ctx()), [])


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        sample = cpu.collect()
        self.assertEqual(sample["status"], "ok")
        self.assertGreaterEqual(sample["cores"], 1)
        self.assertIsInstance(sample["usage_pct"], float)


if __name__ == "__main__":
    unittest.main()
```

Fichier `tests/test_probes_memory.py` :

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import memory

GIB = 1024 ** 3


def ctx(sustained=None):
    return EvalContext(sustained=sustained or {}, cores=4)


def sample(available_pct=50.0, swap_used=0):
    return {"status": "ok", "total": 5 * GIB,
            "available": int(5 * GIB * available_pct / 100),
            "available_pct": available_pct, "swap_used": swap_used,
            "swap_total": 2 * GIB}


class TestMetrics(unittest.TestCase):
    def test_extracts_series(self):
        m = memory.metrics(sample())
        self.assertIn("mem.available", m)
        self.assertIn("mem.available_pct", m)
        self.assertIn("mem.swap.used", m)


class TestEvaluate(unittest.TestCase):
    def test_comfortable_memory_is_silent(self):
        self.assertEqual(memory.evaluate(sample(50.0), ctx()), [])

    def test_low_memory_alone_is_silent(self):
        # Peu de mémoire disponible sans échange actif est le régime normal
        # de Linux : le cache occupe la mémoire libre. Ce n'est pas un défaut.
        self.assertEqual(memory.evaluate(sample(10.0, swap_used=0), ctx()), [])

    def test_low_memory_with_active_swap_warns(self):
        findings = memory.evaluate(sample(10.0, swap_used=512 * 1024 ** 2), ctx())
        self.assertEqual(len(findings), 1)
        self.assertIs(findings[0].severity, Severity.ATTENTION)

    def test_finding_gives_figures_a_human_can_use(self):
        f = memory.evaluate(sample(10.0, swap_used=512 * 1024 ** 2), ctx())[0]
        self.assertIn("Go", f.titre)

    def test_unavailable_sample_produces_no_finding(self):
        self.assertEqual(
            memory.evaluate({"status": "unavailable", "reason": "x"}, ctx()), [])


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_a_usable_sample(self):
        s = memory.collect()
        self.assertEqual(s["status"], "ok")
        self.assertGreater(s["total"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'EvalContext'`

- [ ] **Step 3: Écrire `healthconsole/probes/__init__.py`**

```python
"""Registre des sondes et contrat commun.

Une sonde qui échoue renvoie un relevé `unavailable` avec sa raison ; elle ne
fait jamais tomber les autres. Un outil de diagnostic qui plante quand quelque
chose ne va pas est pire qu'inutile.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType

FAST = "fast"
SLOW = "slow"

PROBE_MODULES: tuple[str, ...] = (
    "cpu", "memory", "thermal", "network", "battery",
)


@dataclass(frozen=True)
class EvalContext:
    """Tout ce dont `evaluate()` a besoin en plus du relevé.

    Les dépassements confirmés dans la durée sont calculés par
    l'ordonnanceur et transmis ici, ce qui laisse `evaluate()` purement
    fonctionnelle : sans horloge, elle reste testable avec des relevés figés.
    """

    sustained: dict[str, bool] = field(default_factory=dict)
    cores: int = 1


def load_probes(cadence: str | None = None) -> list[ModuleType]:
    modules = []
    for name in PROBE_MODULES:
        module = importlib.import_module(f"healthconsole.probes.{name}")
        if cadence is None or module.CADENCE == cadence:
            modules.append(module)
    return modules


def unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}
```

- [ ] **Step 4: Écrire `healthconsole/probes/cpu.py`**

```python
"""Charge, fréquence et charge système du processeur."""

from __future__ import annotations

import os

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "cpu"
CADENCE = FAST


def collect() -> dict:
    try:
        freq = psutil.cpu_freq()
        load1, load5, load15 = os.getloadavg()
        return {
            "status": "ok",
            "usage_pct": float(psutil.cpu_percent(interval=None)),
            "freq_hz": float(freq.current * 1e6) if freq else None,
            "load1": float(load1), "load5": float(load5), "load15": float(load15),
            "cores": psutil.cpu_count(logical=True) or 1,
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"lecture du processeur impossible : {exc}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    out: dict[str, float] = {}
    for key, kind, field_name in (
        ("cpu.usage", "percent", "usage_pct"),
        ("cpu.freq", "hertz", "freq_hz"),
        ("load.1", "load", "load1"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            out[key] = value
    return out


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    if not ctx.sustained.get("cpu.usage_high"):
        return []
    usage = sample.get("usage_pct")
    return [Finding(
        id="cpu.usage_high",
        severity=Severity.ATTENTION,
        titre="Votre ordinateur travaille beaucoup depuis un moment",
        pourquoi=(
            "Un programme sollicite fortement la machine depuis plusieurs "
            "minutes. C'est normal pendant un traitement lourd, mais si cela "
            "dure sans raison, l'ordinateur risque de chauffer et de ralentir."
        ),
        technique=(
            f"usage={usage:.0f}% soutenu ≥ {rules.CPU_USAGE_SUSTAIN_SECONDS}s · "
            f"load1={sample.get('load1')} sur {sample.get('cores')} cœurs"
        ),
    )]
```

- [ ] **Step 5: Écrire `healthconsole/probes/memory.py`**

```python
"""Mémoire vive et espace d'échange."""

from __future__ import annotations

import psutil

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "memory"
CADENCE = FAST
GIB = 1024 ** 3


def collect() -> dict:
    try:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "status": "ok",
            "total": int(vm.total), "available": int(vm.available),
            "available_pct": float(vm.available) / float(vm.total) * 100.0,
            "swap_used": int(sw.used), "swap_total": int(sw.total),
        }
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"lecture de la mémoire impossible : {exc}")


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    out: dict[str, float] = {}
    for key, kind, field_name in (
        ("mem.available", "bytes", "available"),
        ("mem.available_pct", "percent", "available_pct"),
        ("mem.swap.used", "bytes", "swap_used"),
    ):
        value = sane(kind, sample.get(field_name))
        if value is not None:
            out[key] = float(value)
    return out


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    pct = sample.get("available_pct")
    swap_used = sample.get("swap_used", 0)
    # Peu de mémoire « libre » est le régime normal de Linux : le cache
    # occupe ce qui n'est pas utilisé. Seul l'échange actif signale une gêne.
    if pct is None or pct >= rules.MEM_ATTENTION_AVAILABLE_PCT:
        return []
    if swap_used < rules.MEM_SWAP_ACTIVE_BYTES:
        return []
    available_gb = sample["available"] / GIB
    return [Finding(
        id="memory.pressure",
        severity=Severity.ATTENTION,
        titre=f"La mémoire est presque pleine ({available_gb:.1f} Go libres)",
        pourquoi=(
            "L'ordinateur a commencé à déplacer des données vers le disque, "
            "beaucoup plus lent que la mémoire. Fermer quelques applications "
            "devrait le rendre plus réactif."
        ),
        technique=(
            f"available={available_gb:.2f} Gio ({pct:.1f}%) · "
            f"swap_used={swap_used / GIB:.2f} Gio"
        ),
    )]
```

- [ ] **Step 6: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 7: Commit**

```bash
git add healthconsole/probes tests/test_probes_cpu.py tests/test_probes_memory.py
git commit -m "Ajouter le contrat des sondes et les sondes processeur et mémoire

collect() mesure, evaluate() juge, metrics() historise. evaluate() est
pure : les dépassements confirmés dans la durée lui arrivent par
EvalContext, calculés par l'ordonnanceur.

La sonde mémoire n'alerte que si l'échange est actif : peu de mémoire
libre est le régime normal de Linux, le cache occupant le reste."
```

---

### Task 9: Sondes températures, réseau et batterie

**Files:**
- Create: `healthconsole/probes/thermal.py`, `healthconsole/probes/network.py`, `healthconsole/probes/battery.py`
- Create: `tests/test_probes_thermal.py`, `tests/test_probes_network.py`, `tests/test_probes_battery.py`

**Interfaces:**
- Consumes: `probes.EvalContext`, `plausibility.sane`, `plausibility.battery_capacity_is_coherent`
- Produces: trois modules au contrat de la tâche 8, plus `network.rates(previous: dict, current: dict, dt: float) -> dict[str, float]` (fonction pure) et `battery.wear_pct(full: float, design: float) -> float | None`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_probes_battery.py` :

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import battery


def ctx():
    return EvalContext(sustained={}, cores=4)


class TestWear(unittest.TestCase):
    def test_new_battery_has_no_wear(self):
        self.assertEqual(battery.wear_pct(full=5_000_000, design=5_000_000), 0.0)

    def test_worn_battery(self):
        self.assertAlmostEqual(
            battery.wear_pct(full=3_500_000, design=5_000_000), 30.0)

    def test_zero_design_is_unknown(self):
        self.assertIsNone(battery.wear_pct(full=1000, design=0))


class TestIncoherentDriver(unittest.TestCase):
    """Valeurs réelles de la machine cible : le pilote ment."""

    SAMPLE = {"status": "incoherent",
              "raw": {"charge_now": 467000, "charge_full": 1000,
                      "charge_full_design": 1000}}

    def test_incoherent_sample_produces_no_finding(self):
        self.assertEqual(battery.evaluate(self.SAMPLE, ctx()), [])

    def test_incoherent_sample_historises_nothing(self):
        self.assertEqual(battery.metrics(self.SAMPLE), {})


class TestEvaluate(unittest.TestCase):
    def ok(self, wear=0.0, charge=80.0):
        return {"status": "ok", "wear_pct": wear, "charge_pct": charge,
                "present": True, "state": "Discharging"}

    def test_healthy_battery_is_silent(self):
        self.assertEqual(battery.evaluate(self.ok(), ctx()), [])

    def test_worn_battery_warns(self):
        f = battery.evaluate(self.ok(wear=35.0), ctx())
        self.assertEqual(len(f), 1)
        self.assertIs(f[0].severity, Severity.ATTENTION)

    def test_very_worn_battery_is_urgent(self):
        f = battery.evaluate(self.ok(wear=60.0), ctx())
        self.assertIs(f[0].severity, Severity.URGENT)

    def test_absent_battery_produces_no_finding(self):
        self.assertEqual(
            battery.evaluate({"status": "unavailable",
                              "reason": "aucune batterie"}, ctx()), [])

    def test_wear_finding_avoids_jargon(self):
        f = battery.evaluate(self.ok(wear=35.0), ctx())[0]
        self.assertNotIn("charge_full", f.pourquoi)


class TestCollectSmoke(unittest.TestCase):
    def test_collect_never_raises(self):
        s = battery.collect()
        self.assertIn(s["status"], {"ok", "unavailable", "incoherent"})


if __name__ == "__main__":
    unittest.main()
```

Fichier `tests/test_probes_network.py` :

```python
import unittest

from healthconsole.probes import network


class TestRates(unittest.TestCase):
    def test_computes_bytes_per_second(self):
        prev = {"enp0s25": {"rx": 1000, "tx": 500}}
        cur = {"enp0s25": {"rx": 3000, "tx": 1500}}
        r = network.rates(prev, cur, dt=2.0)
        self.assertEqual(r["net.enp0s25.rx_bps"], 1000.0)
        self.assertEqual(r["net.enp0s25.tx_bps"], 500.0)

    def test_counter_reset_yields_no_negative_rate(self):
        # Une interface qui redémarre remet ses compteurs à zéro. Ce n'est
        # pas un débit négatif : on saute l'échantillon.
        prev = {"wlo1": {"rx": 10_000, "tx": 10_000}}
        cur = {"wlo1": {"rx": 5, "tx": 5}}
        self.assertEqual(network.rates(prev, cur, dt=2.0), {})

    def test_new_interface_is_ignored_until_second_sample(self):
        self.assertEqual(network.rates({}, {"eth0": {"rx": 1, "tx": 1}}, 2.0), {})

    def test_zero_dt_is_refused(self):
        prev = {"eth0": {"rx": 1, "tx": 1}}
        cur = {"eth0": {"rx": 2, "tx": 2}}
        self.assertEqual(network.rates(prev, cur, dt=0.0), {})


class TestCollectSmoke(unittest.TestCase):
    def test_collect_returns_interfaces(self):
        s = network.collect()
        self.assertEqual(s["status"], "ok")
        self.assertIn("lo", s["counters"])


if __name__ == "__main__":
    unittest.main()
```

Fichier `tests/test_probes_thermal.py` :

```python
import unittest

from healthconsole.findings import Severity
from healthconsole.probes import EvalContext
from healthconsole.probes import thermal


def ctx(sustained=None):
    return EvalContext(sustained=sustained or {}, cores=4)


def sample(pkg=60.0, zones=None):
    return {"status": "ok", "package_c": pkg,
            "zones": zones or {"coretemp": 60.0, "acpitz": 45.0}}


class TestMetrics(unittest.TestCase):
    def test_each_zone_becomes_a_series(self):
        m = thermal.metrics(sample())
        self.assertEqual(m["thermal.coretemp"], 60.0)
        self.assertEqual(m["cpu.temp.pkg"], 60.0)

    def test_impossible_temperature_is_dropped(self):
        m = thermal.metrics(sample(zones={"broken": 5000.0, "coretemp": 60.0}))
        self.assertNotIn("thermal.broken", m)
        self.assertIn("thermal.coretemp", m)


class TestEvaluate(unittest.TestCase):
    def test_normal_temperature_is_silent(self):
        # 77 °C est le régime normal de la machine cible sous charge.
        self.assertEqual(thermal.evaluate(sample(77.0), ctx()), [])

    def test_hot_but_brief_is_silent(self):
        self.assertEqual(thermal.evaluate(sample(88.0), ctx()), [])

    def test_sustained_heat_warns(self):
        f = thermal.evaluate(sample(88.0), ctx({"cpu.temp_high": True}))
        self.assertEqual(len(f), 1)
        self.assertIs(f[0].severity, Severity.ATTENTION)

    def test_critical_temperature_is_urgent_without_waiting(self):
        f = thermal.evaluate(sample(97.0), ctx())
        self.assertIs(f[0].severity, Severity.URGENT)

    def test_missing_package_temperature_is_silent(self):
        self.assertEqual(thermal.evaluate(
            {"status": "ok", "package_c": None, "zones": {}}, ctx()), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.probes.thermal'`

- [ ] **Step 3: Écrire `healthconsole/probes/thermal.py`**

```python
"""Températures, lues directement dans sysfs.

`lm-sensors` n'est pas installé sur la machine cible et n'est pas requis :
/sys/class/hwmon expose tout ce dont on a besoin.
"""

from __future__ import annotations

from pathlib import Path

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "thermal"
CADENCE = FAST
HWMON = Path("/sys/class/hwmon")
PACKAGE_HINTS = ("coretemp", "x86_pkg_temp", "k10temp")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def collect() -> dict:
    zones: dict[str, float] = {}
    try:
        for hwmon in sorted(HWMON.glob("hwmon*")):
            name = _read(hwmon / "name") or hwmon.name
            temps = sorted(hwmon.glob("temp*_input"))
            if not temps:
                continue
            values = [int(v) / 1000.0 for v in
                      (_read(t) for t in temps) if v is not None]
            if values:
                zones[name] = max(values)
    except OSError as exc:
        return unavailable(f"capteurs thermiques illisibles : {exc}")
    if not zones:
        return unavailable("aucun capteur thermique exposé par le noyau")
    package = next((zones[h] for h in PACKAGE_HINTS if h in zones), None)
    return {"status": "ok", "package_c": package, "zones": zones}


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    out: dict[str, float] = {}
    for zone, value in sample.get("zones", {}).items():
        checked = sane("celsius", value)
        if checked is not None:
            out[f"thermal.{zone}"] = checked
    package = sane("celsius", sample.get("package_c"))
    if package is not None:
        out["cpu.temp.pkg"] = package
    return out


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    pkg = sane("celsius", sample.get("package_c"))
    if pkg is None:
        return []
    if pkg >= rules.CPU_TEMP_URGENT_C:
        return [Finding(
            id="thermal.critical", severity=Severity.URGENT,
            titre=f"Votre ordinateur est très chaud ({pkg:.0f} °C)",
            pourquoi=(
                "À cette température, la machine se bride pour se protéger et "
                "peut s'éteindre seule. Vérifiez que les grilles d'aération ne "
                "sont pas obstruées et posez l'appareil sur une surface dure."
            ),
            technique=f"package={pkg:.1f} °C ≥ {rules.CPU_TEMP_URGENT_C} °C",
        )]
    if ctx.sustained.get("cpu.temp_high"):
        return [Finding(
            id="thermal.high", severity=Severity.ATTENTION,
            titre=f"Votre ordinateur chauffe depuis un moment ({pkg:.0f} °C)",
            pourquoi=(
                "La chaleur dure depuis plusieurs minutes. Si cela se répète "
                "souvent, le ventilateur est probablement encrassé : un "
                "dépoussiérage rallonge la vie de la machine."
            ),
            technique=(f"package={pkg:.1f} °C soutenu ≥ "
                       f"{rules.CPU_TEMP_SUSTAIN_SECONDS}s"),
        )]
    return []
```

- [ ] **Step 4: Écrire `healthconsole/probes/network.py`**

```python
"""Interfaces réseau et débits.

`collect()` renvoie des compteurs absolus ; le calcul des débits est une
fonction pure à part, pour être testable sans machine et sans état caché.
"""

from __future__ import annotations

import psutil

from healthconsole.findings import Finding
from healthconsole.plausibility import sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "network"
CADENCE = FAST


def collect() -> dict:
    try:
        counters = {
            name: {"rx": int(c.bytes_recv), "tx": int(c.bytes_sent)}
            for name, c in psutil.net_io_counters(pernic=True).items()
        }
        addrs = {
            name: [a.address for a in entries if a.family.name == "AF_INET"]
            for name, entries in psutil.net_if_addrs().items()
        }
        stats = {name: s.isup for name, s in psutil.net_if_stats().items()}
        return {"status": "ok", "counters": counters, "addresses": addrs,
                "up": stats}
    except Exception as exc:                      # noqa: BLE001
        return unavailable(f"lecture du réseau impossible : {exc}")


def rates(previous: dict, current: dict, dt: float) -> dict[str, float]:
    """Débits en octets par seconde entre deux relevés de compteurs.

    Une interface qui redémarre remet ses compteurs à zéro : ce n'est pas un
    débit négatif, l'échantillon est simplement sauté.
    """
    if dt <= 0:
        return {}
    out: dict[str, float] = {}
    for name, cur in current.items():
        prev = previous.get(name)
        if prev is None:
            continue
        for direction in ("rx", "tx"):
            delta = cur[direction] - prev[direction]
            if delta < 0:
                continue
            value = sane("bytes_per_second", delta / dt)
            if value is not None:
                out[f"net.{name}.{direction}_bps"] = value
    return out


def metrics(sample: dict) -> dict[str, float]:
    # Les débits demandent deux relevés : l'ordonnanceur les calcule avec
    # rates() et les pousse lui-même dans le tampon.
    return {}


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    return []
```

- [ ] **Step 5: Écrire `healthconsole/probes/battery.py`**

```python
"""Batterie : charge, état et usure réelle.

Deux pièges traités ici. D'une part les noyaux exposent soit `energy_*` soit
`charge_*` selon le matériel — la machine cible n'a que `charge_*`. D'autre
part son pilote annonce `charge_now` 467 fois supérieur à `charge_full` :
sans contrôle, la console afficherait « 0 % d'usure, chargée à 46 700 % ».
"""

from __future__ import annotations

from pathlib import Path

from healthconsole import rules
from healthconsole.findings import Finding, Severity
from healthconsole.plausibility import battery_capacity_is_coherent, sane
from healthconsole.probes import FAST, EvalContext, unavailable

NAME = "battery"
CADENCE = FAST
POWER_SUPPLY = Path("/sys/class/power_supply")


def _read_int(directory: Path, name: str) -> int | None:
    try:
        return int((directory / name).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _first_battery() -> Path | None:
    if not POWER_SUPPLY.exists():
        return None
    for entry in sorted(POWER_SUPPLY.iterdir()):
        kind = entry / "type"
        try:
            if kind.read_text(encoding="utf-8").strip() == "Battery":
                if (entry / "charge_full").exists() or \
                        (entry / "energy_full").exists():
                    return entry
        except OSError:
            continue
    return None


def wear_pct(full: float, design: float) -> float | None:
    if not design:
        return None
    return max(0.0, (1.0 - full / design) * 100.0)


def collect() -> dict:
    directory = _first_battery()
    if directory is None:
        return unavailable("aucune batterie détectée (poste fixe ?)")

    prefix = "charge" if (directory / "charge_full").exists() else "energy"
    now = _read_int(directory, f"{prefix}_now")
    full = _read_int(directory, f"{prefix}_full")
    design = _read_int(directory, f"{prefix}_full_design")
    state = None
    try:
        state = (directory / "status").read_text(encoding="utf-8").strip()
    except OSError:
        pass

    raw = {f"{prefix}_now": now, f"{prefix}_full": full,
           f"{prefix}_full_design": design}
    if now is None or full is None or design is None:
        return {"status": "incoherent", "raw": raw,
                "reason": "capacités batterie incomplètes"}
    if not battery_capacity_is_coherent(now, full, design):
        return {"status": "incoherent", "raw": raw,
                "reason": "valeurs incohérentes rapportées par le pilote"}

    return {
        "status": "ok", "present": True, "state": state,
        "charge_pct": now / full * 100.0,
        "wear_pct": wear_pct(full, design),
        "raw": raw,
    }


def metrics(sample: dict) -> dict[str, float]:
    if sample.get("status") != "ok":
        return {}
    out: dict[str, float] = {}
    for key, field_name in (("battery.charge_pct", "charge_pct"),
                            ("battery.wear_pct", "wear_pct")):
        value = sane("percent", sample.get(field_name))
        if value is not None:
            out[key] = value
    return out


def evaluate(sample: dict, ctx: EvalContext) -> list[Finding]:
    if sample.get("status") != "ok":
        return []
    wear = sane("percent", sample.get("wear_pct"))
    if wear is None or wear < rules.BATTERY_WEAR_ATTENTION_PCT:
        return []
    urgent = wear >= rules.BATTERY_WEAR_URGENT_PCT
    return [Finding(
        id="battery.wear",
        severity=Severity.URGENT if urgent else Severity.ATTENTION,
        titre=f"La batterie a perdu {wear:.0f} % de sa capacité d'origine",
        pourquoi=(
            "Elle tient donc moins longtemps qu'à l'achat. C'est l'usure "
            "normale d'une batterie, mais au-delà de la moitié il devient "
            "raisonnable d'envisager son remplacement."
        ),
        technique=f"wear={wear:.1f}% · {sample.get('raw')}",
    )]
```

- [ ] **Step 6: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 7: Commit**

```bash
git add healthconsole/probes tests/test_probes_thermal.py tests/test_probes_network.py tests/test_probes_battery.py
git commit -m "Ajouter les sondes températures, réseau et batterie

La sonde batterie lit indifféremment energy_* et charge_*, et refuse
les relevés incohérents : sur la machine cible le pilote annonce une
charge 467 fois supérieure à la capacité pleine, ce qui donnerait
« 0 % d'usure, chargée à 46 700 % ».

Les débits réseau sont calculés par une fonction pure qui saute les
remises à zéro de compteurs plutôt que d'inventer un débit négatif."
```

---

### Task 10: Ordonnanceur

**Files:**
- Create: `healthconsole/scheduler.py`
- Create: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Config`, `Store`, `Ring`, `HysteresisTracker`, `load_probes`, `EvalContext`, `network.rates`
- Produces: `Scheduler(cfg, store, ring, probes=None, clock=time.time)` avec `tick(now: float | None = None) -> dict`, `flush(now: float | None = None) -> int`, `maintain(now: float | None = None) -> dict[str, int]`, `state() -> dict`, `SUSTAIN_RULES: dict[str, tuple[str, float, float, float]]`

**Le contrat de `state()`** — c'est le corps de la réponse `/api/now`, consommé par le serveur (tâche 11) et par le front (tâche 13) :

```python
{
  "ts": 1772400000.0,          # instant du dernier relevé
  "score": 92,                 # 0-100
  "breakdown": [["disque", 8]],# chaque point perdu, tracé
  "severity": "ATTENTION",     # pire gravité présente
  "findings": [ {...} ],       # constats sérialisés
  "probes": {"cpu": {...}},    # relevés bruts par sonde
  "depth_days": 3.2,           # profondeur d'historique RÉELLEMENT disponible
}
```

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_scheduler.py` :

```python
import unittest

from healthconsole.config import Config
from healthconsole.findings import Finding, Severity
from healthconsole.probes import FAST
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.store import Store


class FakeProbe:
    """Sonde contrôlée, pour piloter l'ordonnanceur sans matériel."""

    NAME = "fake"
    CADENCE = FAST

    def __init__(self):
        self.value = 10.0
        self.status = "ok"
        self.collect_calls = 0

    def collect(self):
        self.collect_calls += 1
        return {"status": self.status, "usage_pct": self.value}

    def metrics(self, sample):
        if sample.get("status") != "ok":
            return {}
        return {"cpu.usage": sample["usage_pct"]}

    def evaluate(self, sample, ctx):
        if ctx.sustained.get("cpu.usage_high"):
            return [Finding(id="cpu.usage_high", severity=Severity.ATTENTION,
                            titre="t", pourquoi="p", technique="tech")]
        return []


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.ring = Ring(window_seconds=3600, live_seconds=2)
        self.probe = FakeProbe()
        self.sched = Scheduler(Config(), self.store, self.ring,
                               probes=[self.probe])

    def tearDown(self):
        self.store.close()


class TestTick(SchedulerCase):
    def test_tick_fills_the_ring_not_the_database(self):
        self.sched.tick(now=1000.0)
        self.assertEqual(len(self.ring.series("cpu.usage")), 1)
        self.assertEqual(self.store.count_rows("metric"), 0)

    def test_tick_returns_the_state(self):
        state = self.sched.tick(now=1000.0)
        self.assertEqual(state["score"], 100)
        self.assertEqual(state["findings"], [])
        self.assertIn("fake", state["probes"])

    def test_a_failing_probe_does_not_stop_the_others(self):
        class Exploding(FakeProbe):
            NAME = "boom"
            def collect(self):
                raise RuntimeError("capteur cassé")

        sched = Scheduler(Config(), self.store, self.ring,
                          probes=[Exploding(), self.probe])
        state = sched.tick(now=1000.0)
        self.assertEqual(state["probes"]["boom"]["status"], "unavailable")
        self.assertEqual(state["probes"]["fake"]["status"], "ok")


class TestSustained(SchedulerCase):
    def test_brief_spike_produces_no_finding(self):
        self.probe.value = 99.0
        state = self.sched.tick(now=1000.0)
        self.assertEqual(state["findings"], [])

    def test_sustained_load_produces_a_finding(self):
        self.probe.value = 99.0
        self.sched.tick(now=1000.0)
        state = self.sched.tick(now=1000.0 + 301)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(state["score"], 92)
        self.assertEqual(state["breakdown"], [["cpu.usage_high", 8]])


class TestFlush(SchedulerCase):
    def test_flush_writes_the_aggregate_to_the_database(self):
        for i in range(15):
            self.sched.tick(now=1000.0 + i * 2)
        written = self.sched.flush(now=1030.0)
        self.assertEqual(written, 1)
        rows = self.store.read_series("cpu.usage", 0, 2000)
        self.assertEqual(len(rows), 1)

    def test_flush_with_no_new_point_writes_nothing(self):
        self.assertEqual(self.sched.flush(now=1000.0), 0)

    def test_flush_averages_the_window(self):
        for i, v in enumerate([10.0, 20.0, 30.0]):
            self.probe.value = v
            self.sched.tick(now=1000.0 + i * 2)
        self.sched.flush(now=1030.0)
        self.assertAlmostEqual(
            self.store.read_series("cpu.usage", 0, 2000)[0][1], 20.0)


class TestMaintain(SchedulerCase):
    def test_maintain_aggregates_and_prunes(self):
        report = self.sched.maintain(now=100 * 86400)
        self.assertIn("aggregated", report)
        self.assertIn("deleted", report)


class TestDepth(SchedulerCase):
    def test_depth_reports_what_exists_not_what_is_configured(self):
        # raw_days vaut 2 par défaut, mais rien n'est encore stocké.
        self.assertEqual(self.sched.state()["depth_days"], 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.scheduler'`

- [ ] **Step 3: Écrire `healthconsole/scheduler.py`**

```python
"""Les deux cadences et leur orchestration.

Le direct remplit le tampon mémoire ; seule une valeur agrégée part en base.
L'ordonnanceur est aussi le seul endroit qui connaît l'horloge : c'est ce qui
laisse les sondes et les règles purement fonctionnelles.
"""

from __future__ import annotations

import time
from dataclasses import asdict

from healthconsole import rules
from healthconsole.config import Config
from healthconsole.findings import Severity
from healthconsole.probes import FAST, EvalContext, load_probes
from healthconsole.probes import network as network_probe
from healthconsole.ring import Ring
from healthconsole.store import Store
from healthconsole.verdict import HysteresisTracker, score, score_breakdown, worst

# clé de dépassement -> (métrique, seuil haut, seuil bas, durée à tenir)
SUSTAIN_RULES: dict[str, tuple[str, float, float, float]] = {
    "cpu.usage_high": ("cpu.usage", rules.CPU_USAGE_ATTENTION_PCT,
                       rules.CPU_USAGE_ATTENTION_CLEAR_PCT,
                       rules.CPU_USAGE_SUSTAIN_SECONDS),
    "cpu.temp_high": ("cpu.temp.pkg", rules.CPU_TEMP_ATTENTION_C,
                      rules.CPU_TEMP_ATTENTION_CLEAR_C,
                      rules.CPU_TEMP_SUSTAIN_SECONDS),
}


class Scheduler:
    def __init__(self, cfg: Config, store: Store, ring: Ring,
                 probes=None, clock=time.time) -> None:
        self.cfg = cfg
        self.store = store
        self.ring = ring
        self.clock = clock
        self.probes = load_probes(FAST) if probes is None else probes
        self.tracker = HysteresisTracker()
        self._last_flush: float | None = None
        self._net_previous: dict = {}
        self._net_previous_ts: float | None = None
        self._state: dict = {"ts": 0.0, "score": 100, "breakdown": [],
                             "severity": "OK", "findings": [], "probes": {},
                             "depth_days": 0.0}

    # --- cadence rapide ----------------------------------------------

    def tick(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        samples: dict[str, dict] = {}
        measurements: dict[str, float] = {}

        for probe in self.probes:
            try:
                sample = probe.collect()
                samples[probe.NAME] = sample
                measurements.update(probe.metrics(sample))
            except Exception as exc:              # noqa: BLE001
                # Une sonde qui échoue ne fait jamais tomber les autres.
                samples[probe.NAME] = {
                    "status": "unavailable",
                    "reason": f"sonde en échec : {exc}",
                }

        measurements.update(self._network_rates(samples, now))
        for key, value in measurements.items():
            self.ring.push(key, value, ts=now)

        sustained = {
            flag: self.tracker.update(flag, measurements.get(metric),
                                      high, low, now, sustain)
            for flag, (metric, high, low, sustain) in SUSTAIN_RULES.items()
        }
        ctx = EvalContext(sustained=sustained,
                          cores=samples.get("cpu", {}).get("cores", 1))

        findings = []
        for probe in self.probes:
            sample = samples.get(probe.NAME, {})
            try:
                findings.extend(probe.evaluate(sample, ctx))
            except Exception:                     # noqa: BLE001
                continue

        self._state = {
            "ts": now,
            "score": score(findings),
            "breakdown": [list(pair) for pair in score_breakdown(findings)],
            "severity": worst(findings).name,
            "findings": [self._serialise(f) for f in findings],
            "probes": samples,
            "depth_days": self._depth_days(now),
        }
        return self._state

    def _network_rates(self, samples: dict, now: float) -> dict[str, float]:
        sample = samples.get("network")
        if not sample or sample.get("status") != "ok":
            return {}
        current = sample["counters"]
        out: dict[str, float] = {}
        if self._net_previous and self._net_previous_ts is not None:
            out = network_probe.rates(self._net_previous, current,
                                      dt=now - self._net_previous_ts)
        self._net_previous = current
        self._net_previous_ts = now
        return out

    @staticmethod
    def _serialise(finding) -> dict:
        data = asdict(finding)
        data["severity"] = Severity(finding.severity).name
        return data

    def _depth_days(self, now: float) -> float:
        seconds = self.store.available_depth_seconds("metric", int(now))
        return round(seconds / 86_400, 2)

    # --- cadence d'écriture -------------------------------------------

    def flush(self, now: float | None = None) -> int:
        """Écrit en base la moyenne de la fenêtre écoulée. Renvoie le nombre
        d'instants écrits (0 ou 1)."""
        now = self.clock() if now is None else now
        since = self._last_flush if self._last_flush is not None else 0.0
        rows = []
        for key in self.ring.keys():
            aggregated = self.ring.aggregate(key, since=since)
            if aggregated is None:
                continue
            avg, mn, mx = aggregated
            rows.append((key, avg, mn, mx))
        if not rows:
            return 0
        self.store.write_metrics(int(now), rows)
        self._last_flush = now
        return 1

    # --- entretien quotidien ------------------------------------------

    def maintain(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        aggregated = self.store.aggregate_5m(int(now))
        deleted = self.store.prune(self.cfg, int(now))
        return {"aggregated": aggregated, "deleted": deleted}

    def state(self) -> dict:
        return self._state
```

- [ ] **Step 4: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 5: Commit**

```bash
git add healthconsole/scheduler.py tests/test_scheduler.py
git commit -m "Ajouter l'ordonnanceur des deux cadences

tick() remplit le tampon mémoire et produit l'état ; flush() écrit la
moyenne de la fenêtre en base ; maintain() agrège et purge.

L'ordonnanceur est le seul composant qui connaît l'horloge, ce qui
laisse les sondes et les règles purement fonctionnelles. Une sonde qui
lève une exception est marquée indisponible sans arrêter les autres."
```

---

### Task 11: Serveur HTTP, jeton et `/api/now`

**Files:**
- Create: `healthconsole/server.py`
- Create: `tests/test_server.py`

**Interfaces:**
- Consumes: `Config`, `Scheduler`
- Produces: `is_loopback(addr: str) -> bool`, `authorise(client_ip: str, presented: str | None, cfg: Config) -> bool`, `generate_token() -> str`, `make_server(cfg, scheduler, web_dir: Path) -> ThreadingHTTPServer`, `WEB_DIR: Path`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_server.py` :

```python
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import (
    authorise, generate_token, is_loopback, make_server,
)
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class TestLoopback(unittest.TestCase):
    def test_ipv4_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))

    def test_ipv6_loopback(self):
        self.assertTrue(is_loopback("::1"))

    def test_lan_address_is_not_loopback(self):
        self.assertFalse(is_loopback("192.168.0.3"))

    def test_garbage_is_not_loopback(self):
        self.assertFalse(is_loopback("pas-une-adresse"))


class TestAuthorise(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(token="s3cr3t")

    def test_loopback_needs_no_token(self):
        self.assertTrue(authorise("127.0.0.1", None, self.cfg))

    def test_lan_without_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", None, self.cfg))

    def test_lan_with_wrong_token_is_refused(self):
        self.assertFalse(authorise("192.168.0.3", "faux", self.cfg))

    def test_lan_with_right_token_is_allowed(self):
        self.assertTrue(authorise("192.168.0.3", "s3cr3t", self.cfg))

    def test_empty_configured_token_never_authorises_the_lan(self):
        # Un jeton vide ne doit pas ouvrir l'accès à tout le réseau.
        self.assertFalse(authorise("192.168.0.3", "", Config(token="")))


class TestToken(unittest.TestCase):
    def test_token_is_long_and_random(self):
        a, b = generate_token(), generate_token()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 43)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.sched = Scheduler(Config(bind="127.0.0.1", port=0),
                              cls.store, Ring())
        cls.sched.tick(now=1000.0)
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.sched, WEB_DIR)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def get(self, path):
        return urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_api_now_returns_the_state(self):
        payload = json.loads(self.get("/api/now").read())
        self.assertIn("score", payload)
        self.assertIn("probes", payload)
        self.assertIn("depth_days", payload)

    def test_index_is_served(self):
        response = self.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/inexistant")
        self.assertEqual(ctx.exception.code, 404)

    def test_security_headers_are_present(self):
        headers = self.get("/").headers
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_path_traversal_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../../etc/passwd")
        self.assertIn(ctx.exception.code, (403, 404))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.server'`

- [ ] **Step 3: Écrire `healthconsole/server.py`**

```python
"""Serveur HTTP.

La console écoute sur le réseau local : tout accès non loopback exige le
jeton, comparé en temps constant. La consultation est ouverte, l'action ne
l'est pas — mais les actions arrivent au plan 3.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from healthconsole.config import Config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def authorise(client_ip: str, presented: str | None, cfg: Config) -> bool:
    if is_loopback(client_ip):
        return True
    if not cfg.token or not presented:
        return False
    return hmac.compare_digest(cfg.token, presented)


def make_server(cfg: Config, scheduler, web_dir: Path = WEB_DIR
                ) -> ThreadingHTTPServer:

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):     # silence : journalisé ailleurs
            pass

        # --- utilitaires ------------------------------------------
        def _send(self, code, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, payload: dict):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _error(self, code, message, detail=""):
            self._json(code, {"error": message, "detail": detail})

        def _authorised(self, query) -> bool:
            presented = self.headers.get("X-Health-Token") or \
                (query.get("k", [None])[0])
            return authorise(self.client_address[0], presented, cfg)

        def _serve_file(self, relative: str):
            target = (web_dir / relative).resolve()
            try:
                target.relative_to(web_dir.resolve())
            except ValueError:
                # Tentative de remontée de chemin.
                return self._error(403, "chemin refusé")
            if not target.is_file():
                return self._error(404, "fichier introuvable", relative)
            content_type = CONTENT_TYPES.get(target.suffix,
                                             "application/octet-stream")
            self._send(200, target.read_bytes(), content_type)

        # --- routage ----------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path

            if not self._authorised(query):
                return self._error(
                    401, "jeton requis",
                    "Cette console est accessible depuis le réseau local ; "
                    "un jeton est nécessaire.")

            if path == "/":
                return self._serve_file("index.html")
            if path.startswith("/static/"):
                return self._serve_file(path[len("/static/"):])
            if path == "/api/now":
                return self._json(200, scheduler.state())
            return self._error(404, "route inconnue", path)

    server = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
    server.daemon_threads = True
    return server
```

- [ ] **Step 4: Créer un `web/index.html` minimal pour que les tests de service de fichier passent**

```bash
mkdir -p web
cat > web/index.html <<'HTML'
<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>Console de santé</title></head>
<body><p>Console de santé — interface au chargement.</p></body>
</html>
HTML
```

- [ ] **Step 5: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 6: Commit**

```bash
git add healthconsole/server.py tests/test_server.py web/index.html
git commit -m "Ajouter le serveur HTTP, le jeton et la route /api/now

L'accès loopback est libre, tout accès réseau exige le jeton comparé
en temps constant. Un jeton vide n'ouvre jamais l'accès au réseau.

Les fichiers statiques sont résolus puis vérifiés comme descendants du
répertoire web, ce qui ferme les remontées de chemin. En-têtes CSP,
nosniff et no-referrer sur toutes les réponses."
```

---

### Task 12: Diffusion en direct (SSE) et historique

**Files:**
- Modify: `healthconsole/server.py` (routes `/api/stream` et `/api/history`)
- Create: `tests/test_stream.py`

**Interfaces:**
- Consumes: `Scheduler.state()`, `Store.read_series`
- Produces: `RANGES: dict[str, int]` (`{"1h": 3600, "24h": 86400, "7d": 604800, "90d": 7776000}`), `MAX_STREAMS: int = 8`, routes `/api/stream` (SSE) et `/api/history?metric=<clé>&range=<plage>`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_stream.py` :

```python
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from healthconsole.config import Config
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import MAX_STREAMS, RANGES, make_server
from healthconsole.store import Store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.sched = Scheduler(Config(), cls.store, Ring())
        cls.sched.tick(now=1000.0)
        cls.store.write_metrics(1000, [("cpu.usage", 20.0, 10.0, 30.0)])
        cls.store.write_metrics(2000, [("cpu.usage", 40.0, 30.0, 50.0)])
        cls.server = make_server(Config(bind="127.0.0.1", port=0),
                                 cls.sched, WEB_DIR)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


class TestHistory(HttpCase):
    def test_known_range_returns_points(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"), timeout=5).read())
        self.assertEqual(body["metric"], "cpu.usage")
        self.assertGreaterEqual(len(body["points"]), 1)

    def test_unknown_range_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                self.url("/api/history?metric=cpu.usage&range=42x"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_missing_metric_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/history?range=24h"), timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_metric_returns_empty_not_an_error(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=inexistant&range=24h"),
            timeout=5).read())
        self.assertEqual(body["points"], [])

    def test_response_states_the_available_depth(self):
        body = json.loads(urllib.request.urlopen(
            self.url("/api/history?metric=cpu.usage&range=90d"), timeout=5).read())
        # La profondeur réellement disponible, jamais celle demandée :
        # un graphique « 90 jours » à moitié vide ferait croire à une panne.
        self.assertIn("depth_days", body)


class TestStream(HttpCase):
    def test_stream_announces_the_right_content_type(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        self.assertEqual(response.headers["Content-Type"],
                         "text/event-stream; charset=utf-8")
        response.close()

    def test_first_event_carries_the_state(self):
        response = urllib.request.urlopen(self.url("/api/stream"), timeout=5)
        lines = []
        for _ in range(3):
            lines.append(response.readline().decode("utf-8"))
        response.close()
        joined = "".join(lines)
        self.assertIn("event: state", joined)
        self.assertIn('"score"', joined)

    def test_stream_cap_is_declared(self):
        self.assertEqual(MAX_STREAMS, 8)

    def test_known_ranges(self):
        self.assertEqual(set(RANGES), {"1h", "24h", "7d", "90d"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_STREAMS'`

- [ ] **Step 3: Ajouter les constantes en tête de `healthconsole/server.py`**

```python
import time

RANGES: dict[str, int] = {"1h": 3600, "24h": 86_400,
                          "7d": 604_800, "90d": 7_776_000}
# Chaque flux occupe un thread. Au-delà, le client bascule en sondage.
MAX_STREAMS = 8
```

- [ ] **Step 4: Ajouter les deux routes dans `do_GET`, avant le `404` final**

```python
            if path == "/api/history":
                return self._history(query)
            if path == "/api/stream":
                return self._stream()
```

- [ ] **Step 5: Ajouter les méthodes correspondantes à la classe `Handler`**

```python
        def _history(self, query):
            metric = query.get("metric", [None])[0]
            window = query.get("range", ["24h"])[0]
            if not metric:
                return self._error(400, "paramètre manquant",
                                   "metric est obligatoire")
            if window not in RANGES:
                return self._error(
                    400, "plage inconnue",
                    f"valeurs acceptées : {', '.join(RANGES)}")
            now = int(time.time())
            since = now - RANGES[window]
            table = "metric" if RANGES[window] <= 172_800 else "metric_5m"
            points = scheduler.store.read_series(metric, since, now, table=table)
            depth = scheduler.store.available_depth_seconds(table, now)
            return self._json(200, {
                "metric": metric, "range": window, "table": table,
                "points": [[ts, value] for ts, value in points],
                # Toujours la profondeur réellement disponible : un graphique
                # « 90 jours » à moitié vide ferait croire à une panne de
                # collecte alors que l'historique vient simplement de commencer.
                "depth_days": round(depth / 86_400, 2),
            })

        def _stream(self):
            with Handler._stream_lock:
                if Handler._stream_count >= MAX_STREAMS:
                    return self._error(
                        503, "trop de flux simultanés",
                        f"maximum {MAX_STREAMS} ; la page bascule en sondage")
                Handler._stream_count += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                # Sans Content-Length, HTTP/1.1 doit fermer la connexion en
                # fin de flux : sinon le client ne sait pas où s'arrête le
                # corps et la réponse suivante se décale.
                self.send_header("Connection", "close")
                self.close_connection = True
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                self.end_headers()
                while True:
                    payload = json.dumps(scheduler.state())
                    self.wfile.write(b"event: state\n")
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(cfg.sampling.live_seconds)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with Handler._stream_lock:
                    Handler._stream_count -= 1
```

- [ ] **Step 6: Déclarer les attributs de classe partagés en tête de `Handler`**

```python
        _stream_lock = threading.Lock()
        _stream_count = 0
```

Ajouter `import threading` en tête du module.

- [ ] **Step 7: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 8: Commit**

```bash
git add healthconsole/server.py tests/test_stream.py
git commit -m "Ajouter la diffusion SSE et la route d'historique

Le flux SSE est plafonné à huit clients simultanés, chacun occupant un
thread ; au-delà la page bascule en sondage plutôt que d'épuiser le
serveur.

La réponse d'historique annonce toujours la profondeur réellement
disponible : un graphique 90 jours à moitié vide ferait croire à une
panne de collecte alors que l'historique vient de commencer."
```

---

### Task 13: Interface — mode Simple

**Files:**
- Modify: `web/index.html`
- Create: `web/style.css`, `web/app.js`
- Create: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: `/api/now`, `/api/stream`
- Produces: une page autonome. `app.js` expose `render(state)` et `formatBytes(n)` sur `globalThis.healthConsole` pour être vérifiable.

**Pourquoi tester des fichiers front en Python :** le projet n'a pas de chaîne de compilation et n'en aura pas. Les propriétés qui comptent ici — aucune ressource externe, la couleur ne porte jamais seule l'information, budget de poids respecté — se vérifient très bien par inspection du fichier, et cette vérification tourne dans la même commande que le reste.

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_web_assets.py` :

```python
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


class TestNoExternalResources(unittest.TestCase):
    """Un outil de diagnostic doit fonctionner sans Internet."""

    def test_no_external_urls(self):
        for name in ("index.html", "style.css", "app.js"):
            text = (WEB / name).read_text(encoding="utf-8")
            for match in re.findall(r"https?://[^\s\"')]+", text):
                self.fail(f"{name} référence une ressource externe : {match}")

    def test_no_build_step_artefacts(self):
        self.assertFalse((WEB.parent / "package.json").exists())


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.html = (WEB / "index.html").read_text(encoding="utf-8")

    def test_language_is_french(self):
        self.assertIn('lang="fr"', self.html)

    def test_has_a_viewport_for_phones(self):
        self.assertIn("viewport", self.html)

    def test_mode_switch_is_present(self):
        self.assertIn('id="mode-simple"', self.html)
        self.assertIn('id="mode-expert"', self.html)

    def test_live_region_announces_changes(self):
        self.assertIn('aria-live="polite"', self.html)

    def test_scripts_and_styles_are_local(self):
        self.assertIn('href="/static/style.css"', self.html)
        self.assertIn('src="/static/app.js"', self.html)


class TestStyle(unittest.TestCase):
    def setUp(self):
        self.css = (WEB / "style.css").read_text(encoding="utf-8")

    def test_dark_theme_is_supported(self):
        self.assertIn("prefers-color-scheme", self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", self.css)

    def test_touch_targets_are_large_enough(self):
        self.assertIn("min-height: 44px", self.css)


class TestApp(unittest.TestCase):
    def setUp(self):
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_stays_within_budget(self):
        size = (WEB / "app.js").stat().st_size
        self.assertLess(size, 60 * 1024, "budget de 60 Kio dépassé")

    def test_every_severity_has_an_icon_and_a_word(self):
        # La couleur ne porte jamais seule l'information : sinon 8 % des
        # hommes ne lisent pas le tableau de bord.
        for word in ("Tout va bien", "Information", "Attention", "Urgent"):
            self.assertIn(word, self.js)

    def test_stale_data_is_announced(self):
        self.assertIn("figées", self.js)

    def test_unavailable_probes_are_shown_as_such(self):
        self.assertIn("indisponible", self.js)

    def test_numbers_are_formatted_in_french(self):
        self.assertIn("fr-FR", self.js)

    def test_default_mode_is_simple(self):
        self.assertIn('"simple"', self.js)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `FileNotFoundError: web/style.css`

- [ ] **Step 3: Écrire `web/index.html`**

```html
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Console de santé</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="barre">
    <h1>Console de santé</h1>
    <div class="modes" role="tablist" aria-label="Niveau de détail">
      <button id="mode-simple" role="tab" aria-selected="true">Simple</button>
      <button id="mode-expert" role="tab" aria-selected="false">Expert</button>
    </div>
  </header>

  <p id="fraicheur" class="fraicheur" aria-live="polite"></p>

  <main id="simple" class="vue">
    <section class="verdict" id="verdict" aria-live="polite">
      <p class="verdict-etat"><span id="verdict-icone" aria-hidden="true"></span>
        <span id="verdict-mot"></span></p>
      <p class="verdict-phrase" id="verdict-phrase"></p>
      <p class="verdict-score">Santé globale
        <strong id="score">—</strong><span aria-hidden="true">/100</span></p>
    </section>
    <div id="constats"></div>
  </main>

  <main id="expert" class="vue" hidden>
    <p>Le mode Expert arrive au plan 2.</p>
    <pre id="brut"></pre>
  </main>

  <script type="module" src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Écrire `web/style.css`**

```css
:root {
  color-scheme: light dark;
  --fond: #f7f7f5;
  --carte: #ffffff;
  --texte: #1a1a1a;
  --discret: #5a5a5a;
  --bord: #d8d8d4;
  --ok: #1c6b3a;
  --info: #2c5a8a;
  --attention: #8a5a00;
  --urgent: #a02020;
}

@media (prefers-color-scheme: dark) {
  :root {
    --fond: #16181a; --carte: #202325; --texte: #ececec;
    --discret: #a8a8a8; --bord: #34383b;
    --ok: #6cc48d; --info: #8bb8e8; --attention: #e0b060; --urgent: #f08080;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font: 16px/1.5 system-ui, sans-serif;
  background: var(--fond);
  color: var(--texte);
}

.barre {
  display: flex; flex-wrap: wrap; gap: 1rem;
  align-items: center; justify-content: space-between;
  padding: 1rem 1.25rem; border-bottom: 1px solid var(--bord);
}

.barre h1 { font-size: 1.1rem; margin: 0; }

.modes button {
  min-height: 44px; min-width: 88px;
  border: 1px solid var(--bord); background: var(--carte);
  color: var(--texte); border-radius: 8px; cursor: pointer; font: inherit;
}

.modes button[aria-selected="true"] {
  background: var(--texte); color: var(--fond);
}

.fraicheur { margin: 0; padding: 0.5rem 1.25rem; color: var(--discret); }
.fraicheur.perimee { color: var(--attention); font-weight: 600; }

.vue { max-width: 46rem; margin: 0 auto; padding: 1.25rem; }

.verdict {
  background: var(--carte); border: 1px solid var(--bord);
  border-radius: 14px; padding: 1.75rem; text-align: center;
}

.verdict-etat { font-size: 1.6rem; font-weight: 700; margin: 0 0 0.5rem; }
.verdict-phrase { margin: 0 0 1rem; color: var(--discret); }
.verdict-score { margin: 0; }

.constat {
  background: var(--carte); border: 1px solid var(--bord);
  border-left: 5px solid var(--bord);
  border-radius: 12px; padding: 1rem 1.25rem; margin-top: 1rem;
}

.constat h2 { font-size: 1.05rem; margin: 0 0 0.35rem; }
.constat p { margin: 0; color: var(--discret); }
.constat .etiquette { font-weight: 700; font-size: 0.85rem; }

.gravite-OK        { border-left-color: var(--ok); }
.gravite-OK .etiquette { color: var(--ok); }
.gravite-INFO      { border-left-color: var(--info); }
.gravite-INFO .etiquette { color: var(--info); }
.gravite-ATTENTION { border-left-color: var(--attention); }
.gravite-ATTENTION .etiquette { color: var(--attention); }
.gravite-URGENT    { border-left-color: var(--urgent); }
.gravite-URGENT .etiquette { color: var(--urgent); }

.perimees { opacity: 0.45; }

@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
```

- [ ] **Step 5: Écrire `web/app.js`**

```javascript
// Mode Simple. Aucune dépendance, aucun réseau sortant.

const MODE_DEFAUT = "simple";

// La couleur ne porte jamais seule l'information : chaque gravité a une
// icône ET un mot. Sinon 8 % des hommes ne lisent pas ce tableau de bord.
const GRAVITES = {
  OK:        { icone: "●", mot: "Tout va bien" },
  INFO:      { icone: "ℹ", mot: "Information" },
  ATTENTION: { icone: "⚠", mot: "Attention" },
  URGENT:    { icone: "✖", mot: "Urgent" },
};

const PHRASES = {
  OK: "Votre ordinateur se porte bien.",
  INFO: "Tout fonctionne, avec un point à connaître.",
  ATTENTION: "Quelque chose mérite votre attention.",
  URGENT: "Une action est nécessaire rapidement.",
};

const nf = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const hf = new Intl.DateTimeFormat("fr-FR",
  { hour: "2-digit", minute: "2-digit" });

export function formatBytes(n) {
  const unites = ["o", "ko", "Mo", "Go", "To"];
  let i = 0;
  while (n >= 1024 && i < unites.length - 1) { n /= 1024; i += 1; }
  return `${nf.format(n)} ${unites[i]}`;
}

function el(id) { return document.getElementById(id); }

export function render(state) {
  const gravite = GRAVITES[state.severity] || GRAVITES.OK;
  el("verdict-icone").textContent = gravite.icone;
  el("verdict-mot").textContent = gravite.mot;
  el("verdict-phrase").textContent = PHRASES[state.severity] || PHRASES.OK;
  el("score").textContent = state.score;

  const hote = el("constats");
  hote.textContent = "";
  for (const f of state.findings) {
    const g = GRAVITES[f.severity] || GRAVITES.INFO;
    const carte = document.createElement("article");
    carte.className = `constat gravite-${f.severity}`;
    const etiquette = document.createElement("p");
    etiquette.className = "etiquette";
    etiquette.textContent = `${g.icone} ${g.mot}`;
    const titre = document.createElement("h2");
    titre.textContent = f.titre;
    const pourquoi = document.createElement("p");
    pourquoi.textContent = f.pourquoi;
    carte.append(etiquette, titre, pourquoi);
    hote.append(carte);
  }

  // Une sonde absente est affichée comme absente, jamais comme un zéro
  // rassurant.
  for (const [nom, sonde] of Object.entries(state.probes || {})) {
    if (sonde.status === "ok") continue;
    const carte = document.createElement("article");
    carte.className = "constat gravite-INFO";
    const titre = document.createElement("h2");
    titre.textContent = `Mesure indisponible : ${nom}`;
    const raison = document.createElement("p");
    raison.textContent = sonde.reason || "cause inconnue";
    carte.append(titre, raison);
    hote.append(carte);
  }

  el("brut").textContent = JSON.stringify(state, null, 2);
  marquerFraicheur(state.ts * 1000, false);
}

function marquerFraicheur(ms, perimee) {
  const zone = el("fraicheur");
  const heure = hf.format(new Date(ms));
  zone.classList.toggle("perimee", perimee);
  document.body.classList.toggle("perimees", perimee);
  zone.textContent = perimee
    ? `Données figées depuis ${heure} · reconnexion…`
    : `À jour · dernière mesure à ${heure}`;
}

function basculer(mode) {
  const simple = mode === "simple";
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function connecter() {
  let dernier = Date.now();
  const source = new EventSource("/api/stream");
  source.addEventListener("state", (evenement) => {
    dernier = Date.now();
    render(JSON.parse(evenement.data));
  });
  source.addEventListener("error", () => marquerFraicheur(dernier, true));
  setInterval(() => {
    if (Date.now() - dernier > 15000) marquerFraicheur(dernier, true);
  }, 5000);
}

el("mode-simple").addEventListener("click", () => basculer("simple"));
el("mode-expert").addEventListener("click", () => basculer("expert"));
basculer(localStorage.getItem("mode") || MODE_DEFAUT);
fetch("/api/now").then((r) => r.json()).then(render).catch(() => {});
connecter();

globalThis.healthConsole = { render, formatBytes };
```

- [ ] **Step 6: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 7: Commit**

```bash
git add web tests/test_web_assets.py
git commit -m "Ajouter l'interface du mode Simple

Verdict en langage courant, une carte par constat, commutateur
Simple/Expert mémorisé, Simple par défaut.

Chaque gravité porte une icône et un mot en plus de la couleur. Les
données figées sont grisées et annoncées avec leur heure, et une sonde
indisponible est montrée comme telle plutôt que par un zéro rassurant.

Les tests vérifient l'absence de toute ressource externe : la console
doit fonctionner sans Internet."
```

---

### Task 14: Ligne de commande et bouclage

**Files:**
- Create: `bin/health-console`
- Create: `healthconsole/cli.py`
- Create: `tests/test_cli.py`, `tests/test_smoke.py`

**Interfaces:**
- Consumes: tout ce qui précède
- Produits: `main(argv: list[str] | None = None) -> int`, `cmd_run(cfg) -> int`, `cmd_config(cfg) -> int`, `cmd_status(cfg) -> int`, `cmd_prune(cfg) -> int`, `default_db_path() -> Path`, `human_bytes(n: int) -> str`

- [ ] **Step 1: Écrire les tests qui échouent**

Fichier `tests/test_cli.py` :

```python
import io
import unittest
from contextlib import redirect_stdout

from healthconsole.cli import human_bytes, main


class TestHumanBytes(unittest.TestCase):
    def test_formats_in_french(self):
        self.assertEqual(human_bytes(1536), "1,5 ko")

    def test_bytes_stay_bytes(self):
        self.assertEqual(human_bytes(512), "512 o")


class TestCommands(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_no_command_shows_usage(self):
        code, output = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", output.lower())

    def test_config_prints_effective_settings(self):
        code, output = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("raw_days", output)
        self.assertIn("aggregate_days", output)

    def test_config_announces_projected_size(self):
        # Le coût doit être annoncé, pas subi.
        _, output = self.run_cli("config")
        self.assertIn("Taille projetée", output)

    def test_unknown_command_is_refused(self):
        code, _ = self.run_cli("teleporter")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
```

Fichier `tests/test_smoke.py` :

```python
"""Test de fumée : lance les vraies sondes sur cette machine.

Dépend du matériel, contrairement à tous les autres tests.
"""

import unittest

from healthconsole.probes import EvalContext, load_probes


class TestRealProbes(unittest.TestCase):
    def test_every_probe_collects_without_raising(self):
        for module in load_probes():
            with self.subTest(sonde=module.NAME):
                sample = module.collect()
                self.assertIn(sample.get("status"),
                              {"ok", "unavailable", "incoherent"})

    def test_every_probe_evaluates_its_own_sample(self):
        ctx = EvalContext(sustained={}, cores=1)
        for module in load_probes():
            with self.subTest(sonde=module.NAME):
                findings = module.evaluate(module.collect(), ctx)
                self.assertIsInstance(findings, list)

    def test_metrics_are_all_finite_numbers(self):
        import math
        for module in load_probes():
            for key, value in module.metrics(module.collect()).items():
                with self.subTest(metrique=key):
                    self.assertTrue(math.isfinite(value))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Lancer les tests pour vérifier qu'ils échouent**

Run: `./run-tests -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'healthconsole.cli'`

- [ ] **Step 3: Écrire `healthconsole/cli.py`**

```python
"""Ligne de commande."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from healthconsole.config import (
    DEFAULT_CONFIG_PATH, ConfigError, estimate_db_bytes, load_config,
)
from healthconsole.ring import Ring
from healthconsole.scheduler import Scheduler
from healthconsole.server import WEB_DIR, make_server
from healthconsole.store import Store

DATA_DIR = Path.home() / ".local" / "share" / "health-console"
WARN_DB_BYTES = 500 * 1024 ** 2


def default_db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "db.sqlite3"


def human_bytes(n: float) -> str:
    unites = ["o", "ko", "Mo", "Go", "To"]
    i = 0
    while n >= 1024 and i < len(unites) - 1:
        n /= 1024
        i += 1
    texte = f"{n:.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{texte} {unites[i]}"


def _open(cfg):
    store = Store(default_db_path())
    ring = Ring(window_seconds=3600, live_seconds=cfg.sampling.live_seconds)
    return store, Scheduler(cfg, store, ring)


def cmd_config(cfg) -> int:
    r, s = cfg.retention, cfg.sampling
    print(f"Fichier          : {DEFAULT_CONFIG_PATH}")
    print(f"Écoute           : {cfg.bind}:{cfg.port}")
    print(f"Jeton            : {'défini' if cfg.token else 'absent'}")
    print("Rétention (jours)")
    for name in ("raw_days", "aggregate_days", "snapshot_days",
                 "event_days", "audit_days"):
        print(f"  {name:<15} {getattr(r, name)}")
    print(f"Cadences         : affichage {s.live_seconds}s · "
          f"écriture {s.store_seconds}s")
    projected = estimate_db_bytes(cfg, n_metrics=25)
    print(f"Taille projetée  : {human_bytes(projected)} (pour 25 métriques)")
    if projected > WARN_DB_BYTES:
        print("  Avertissement : ces réglages produiront une base "
              "volumineuse. Réduisez aggregate_days ou augmentez "
              "store_seconds si ce n'est pas voulu.")
    return 0


def cmd_status(cfg) -> int:
    store, scheduler = _open(cfg)
    try:
        now = int(time.time())
        print(f"Base             : {store.path}")
        print(f"Taille réelle    : {human_bytes(store.db_bytes())}")
        print(f"Métriques suivies: {store.distinct_metric_count()}")
        for table in ("metric", "metric_5m"):
            depth = store.available_depth_seconds(table, now) / 86_400
            print(f"Profondeur {table:<9}: {depth:.2f} jours "
                  f"({store.count_rows(table)} lignes)")
    finally:
        store.close()
    return 0


def cmd_prune(cfg) -> int:
    store, scheduler = _open(cfg)
    try:
        report = scheduler.maintain()
        print(f"Agrégés : {report['aggregated']} seaux")
        for table, count in report["deleted"].items():
            print(f"Purgé {table:<12}: {count} lignes")
        store.vacuum()
        print(f"Taille après     : {human_bytes(store.db_bytes())}")
    finally:
        store.close()
    return 0


def cmd_run(cfg) -> int:
    store, scheduler = _open(cfg)
    server = make_server(cfg, scheduler, WEB_DIR)

    def boucle():
        last_flush = time.time()
        last_maintain = time.time()
        while True:
            now = time.time()
            scheduler.tick(now)
            if now - last_flush >= cfg.sampling.store_seconds:
                scheduler.flush(now)
                last_flush = now
            if now - last_maintain >= 86_400:
                scheduler.maintain(now)
                last_maintain = now
            time.sleep(cfg.sampling.live_seconds)

    threading.Thread(target=boucle, daemon=True).start()
    print(f"Console de santé sur http://{cfg.bind}:{cfg.port}")
    print("Ctrl+C pour arrêter.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
        store.close()
    return 0


COMMANDS = {"run": cmd_run, "config": cmd_config,
            "status": cmd_status, "prune": cmd_prune}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="health-console",
        description="Console de santé système.")
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="run, config, status ou prune")
    parser.add_argument("--config", type=Path, default=None,
                        help="chemin d'un fichier de configuration")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        parser.print_usage()
        return 2
    if args.command is None:
        parser.print_usage()
        return 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration invalide : {exc}", file=sys.stderr)
        return 1
    return COMMANDS[args.command](cfg)
```

- [ ] **Step 4: Écrire `bin/health-console`**

```bash
cat > bin/health-console <<'SH'
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from healthconsole.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
SH
chmod +x bin/health-console
```

- [ ] **Step 5: Lancer les tests pour vérifier qu'ils passent**

Run: `./run-tests -v`
Expected: PASS — toute la suite est verte, y compris les tests des tâches précédentes

- [ ] **Step 6: Vérifier le socle sur la vraie machine**

```bash
./bin/health-console config
./bin/health-console status
./bin/health-console run   # puis ouvrir http://127.0.0.1:8787, Ctrl+C
```

Attendu : `config` annonce une taille projetée d'environ 32 Mo ; la page affiche un verdict, un score et, sur cette machine précise, une carte « Mesure indisponible : battery » avec la mention des valeurs incohérentes du pilote.

- [ ] **Step 7: Commit**

```bash
git add bin healthconsole/cli.py tests/test_cli.py tests/test_smoke.py
git commit -m "Ajouter la ligne de commande et le test de fumée

run, config, status et prune. config annonce la taille de base que les
réglages impliquent, status affiche la profondeur d'historique
réellement disponible.

Le test de fumée lance les vraies sondes de cette machine et vérifie
qu'aucune ne lève d'exception et que toutes les métriques produites
sont des nombres finis."
```

---

## Ce que le plan 1 ne fait pas

Volontairement remis aux plans suivants, pour que celui-ci reste exécutable et livre un logiciel qui marche :

- **Plan 2** — sondes lentes (stockage, SMART, mises à jour, services, journal, processus, infos OS), tendances par régression, mode Expert complet avec courbes d'historique, export du rapport.
- **Plan 3** — catalogue d'actions, règle sudoers, verrou d'exécution, journal d'audit, sortie en direct, unité systemd et commande `install`.
- **Repéré à la relecture** — le spec §12 prévoit qu'une base corrompue soit
  recréée et l'incident journalisé. Le plan 1 ne le traite pas : `Store` laisse
  remonter l'erreur SQLite. À reprendre en tête du plan 2, avant d'ajouter des
  sondes qui écriront davantage.

À l'issue du plan 1, la console tourne, mesure, interprète et s'affiche. Elle ne sait encore rien faire d'autre qu'observer.
