"""Category and subject notes that tie the items together in Obsidian."""
import yaml

from curator2.hubs import plain, read_props, refresh
from curator2.vault import Vault

from .test_pipeline import answers, build_mail, go, make


def test_items_link_to_category_notes_with_live_overview(cfg, tmp_path):
    from .conftest import FakeLLM
    llm = FakeLLM(answers=answers())
    state, judge, src, gh = make(cfg, tmp_path, llm, build_mail())
    go(cfg, state, judge, src, gh)
    cat_dir = cfg.root / "Categorieën"
    hub = (cat_dir / "Python-bibliotheken.md").read_text(encoding="utf-8")
    assert "file.hasLink(this.file)" in hub and 'file.inFolder("Curator/Items")' in hub
    block = hub.split("```base\n", 1)[1].split("```", 1)[0]
    assert yaml.safe_load(block)["views"][0]["name"] == "Python-bibliotheken"
    start = (cfg.root / "Curator.md").read_text(encoding="utf-8")
    assert "[[Curator/Categorieën/Python-bibliotheken|Python-bibliotheken]] (1)" in start
    assert "[[Curator/Runs/2026-10-26|2026-10-26]]" in start


def test_old_notes_are_converted_and_shared_subjects_get_a_note(cfg, tmp_path):
    v = Vault(cfg)
    v.prepare()
    for name, topic in (("A", "Kimi K3.1"), ("B", "kimi k3.1"), ("C", "Other")):
        (v.items / f"{name}.md").write_text(
            f'---\ntitel: "{name}"\ncategorie: "Modellen"\nonderwerp: "{topic}"\ndatum: "2026-10-26"\n---\n\n# {name}\n',
            encoding="utf-8")
    info = refresh(cfg.root, cfg.folder)
    assert info == {"categorieen": 1, "onderwerpen": 1, "items": 3}
    a = read_props((v.items / "A.md").read_text(encoding="utf-8"))
    c = read_props((v.items / "C.md").read_text(encoding="utf-8"))
    assert a["categorie"] == "[[Curator/Categorieën/Modellen|Modellen]]"
    assert a["onderwerp"] == "[[Curator/Onderwerpen/Kimi K3.1|Kimi K3.1]]"
    assert c["onderwerp"] == "Other"                      # only one item: no subject note
    assert (cfg.root / "Onderwerpen" / "Kimi K3.1.md").exists()
    assert not (cfg.root / "Onderwerpen" / "Other.md").exists()
    # running again changes nothing and keeps Kurt's own text in a hub
    hub = cfg.root / "Categorieën" / "Modellen.md"
    hub.write_text(hub.read_text(encoding="utf-8") + "\nMijn notitie.\n", encoding="utf-8")
    before = (v.items / "A.md").read_text(encoding="utf-8")
    refresh(cfg.root, cfg.folder)
    assert (v.items / "A.md").read_text(encoding="utf-8") == before
    assert "Mijn notitie." in hub.read_text(encoding="utf-8")


def test_plain():
    assert plain("[[Curator/Categorieën/AI-tools|AI-tools]]") == "AI-tools"
    assert plain("[[Curator/Categorieën/AI-tools]]") == "AI-tools"
    assert plain("AI-tools") == "AI-tools"
