# Wekelijkse repo-review (opdracht voor Claude)

Elke zondagavond om 21:40, na de gewone run van 20:00. Kurts laptop staat aan, de
Claude-app is open en de externe schijf F: hangt eraan. Deze tekst is ook de opdracht
van de geplande taak "Curator repo-review".

## Wat je doet

1. Lees de notities in `F:\Obsidian\QRT-Obsidian\Curator\Items` via de koppeling met
   Kurts laptop (bestanden ophalen en terugschrijven). Neem alle notities met
   `soort: repo` (met of zonder aanhalingstekens; Obsidian haalt ze soms weg) waarvan
   `repo_oordeel` leeg is.
2. Bekijk per repo de GitHub-pagina: README, sterren, recente activiteit, licentie,
   aantal bijdragers, en of het iets is dat Kurt echt kan gebruiken. Kurt is een
   functionele ERP-consultant die kleine technische experimenten doet met Python en
   AI (zie het profiel in `curator2/config/beoordeling.toml`). Zware tools voor grote
   ontwikkelteams zijn voor hem niet relevant.
3. Vul in elke notitie twee eigenschappen in:
   - `repo_oordeel`: `topper`, `houden` of `weg`
   - `repo_reden`: één Nederlandse zin waarom
4. Bij `topper`: zet ook `topper: true`. Het item verschijnt dan in de weergave
   Om te gebruiken. De lat ligt hoog: alleen iets dat Kurt meteen kan installeren en
   gebruiken voor zijn eigen werk. Geen toppers is een normale uitkomst.
5. Bij `weg`: verwijder niets. Zet `weg_vanaf` op de datum over 7 dagen.
6. Notities met een `weg_vanaf` die voorbij is en waarvan Kurt `repo_oordeel` niet
   veranderde: verwijder ze niet zelf, maar zet ze in de review-notitie onder
   "Mag weg" met een link, zodat Kurt ze zelf wist.
7. Schrijf in `Curator/Runs` een korte notitie `Repo-review <datum>.md`: hoeveel
   repo's bekeken, hoeveel toppers, houden en weg, met de toppers bij naam, en de
   lijst "Mag weg".

## Wat je niet doet

- Geen andere notities aanpassen dan de repo-notities en de review-notitie.
- Alleen de eigenschappen hierboven wijzigen; de rest van een notitie blijft exact
  zoals ze is (Kurt kan er zelf in geschreven hebben).
- Geen oordeel overschrijven dat Kurt zelf heeft aangepast.
- Bij twijfel tussen houden en weg: houden.
- Is de laptop of schijf F: niet bereikbaar, doe dan niets en meld dat kort.
