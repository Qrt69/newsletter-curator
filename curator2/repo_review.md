# Wekelijkse repo-review (opdracht voor Claude)

Elke zondagavond, na de gewone run. Kurts laptop staat aan en de Claude-app is open.

## Wat je doet

1. Open de map `Curator/Items` in Kurts vault (F:\Obsidian). Neem alle notities met
   `soort: "repo"` waarvan `repo_oordeel` leeg is.
2. Bekijk per repo de GitHub-pagina: README, sterren, recente activiteit, licentie,
   aantal bijdragers, en of het iets is dat Kurt echt kan gebruiken. Kurt is een
   functionele ERP-consultant die kleine technische experimenten doet met Python en
   AI (zie het profiel in `curator2/config/beoordeling.toml`). Zware tools voor grote
   ontwikkelteams zijn voor hem niet relevant.
3. Vul in elke notitie twee eigenschappen in:
   - `repo_oordeel`: `topper`, `houden` of `weg`
   - `repo_reden`: één Nederlandse zin waarom
4. Bij `topper`: zet ook `topper: true`. Het item verschijnt dan in de weergave
   Om te gebruiken.
5. Bij `weg`: verplaats niets. Zet `weg_vanaf` op de datum over 7 dagen. Notities
   met een `weg_vanaf` die voorbij is en waarvan Kurt het oordeel niet veranderde,
   verwijder je in een volgende review uit `Items` en zet je als regel in de
   Verworpen-lijst van die dag (reden: "repo-review").
6. Schrijf in `Curator/Runs` een korte notitie `Repo-review <datum>.md`: hoeveel
   repo's bekeken, hoeveel toppers, houden en weg, met de toppers bij naam.

## Wat je niet doet

- Geen andere notities aanpassen dan de repo-notities en de review-notitie.
- Geen oordeel overschrijven dat Kurt zelf heeft aangepast.
- Bij twijfel tussen houden en weg: houden.
