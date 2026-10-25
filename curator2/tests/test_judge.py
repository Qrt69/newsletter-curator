import json

import httpx
import yaml

from curator2.judge import Judge, clean_answers
from curator2.vault import BASE_FILE


def test_falls_back_when_server_has_no_structured_output(cfg):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        out = {"title": "X", "description_nl": "d", "category": "ai_tool", "new_category": "",
               "kind": "tool", "topic": "", "usable": "ja", "fits": "ja", "junk": "nee",
               "topper": "nee", "fit": 4, "reason_nl": "r"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(out) + "\n```"}}]})

    j = Judge(cfg, transport=httpx.MockTransport(handler))
    j.model = "m"
    a = j.judge({"resolved_url": "https://e.com", "link_text": "x", "text": "t"})
    assert a["category"] == "ai_tool" and a["fit"] == 4
    assert seen == [True, False]


def test_clean_answers_repairs_odd_values(cfg):
    cats = list(cfg.categories)
    a = clean_answers({"category": "Nonsense", "fit": "9", "usable": "yes", "fits": "partly",
                       "junk": None, "kind": "weird"}, cats, {"link_text": "Fallback title"})
    assert a["category"] == "article" and a["fit"] == 5
    assert a["usable"] == "ja" and a["fits"] == "deels" and a["junk"] == "nee"
    assert a["kind"] == "other" and a["title"] == "Fallback title"


def test_base_file_is_valid_yaml():
    d = yaml.safe_load(BASE_FILE.replace("{folder}", "Curator"))
    names = [v["name"] for v in d["views"]]
    assert names == ["Nieuwste", "Per categorie", "Om te gebruiken", "Per onderwerp", "Repo-review"]
