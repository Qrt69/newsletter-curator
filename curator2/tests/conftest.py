import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from curator2 import config as config_mod  # noqa: E402


def item(n, url=None, link_text=None, text="Some article text about the subject.", title=None):
    return {
        "source_url": url or f"https://example.com/article-{n}?utm_source=news",
        "resolved_url": url or f"https://example.com/article-{n}?utm_source=news",
        "link_text": link_text or f"Article number {n} about things",
        "title": title or f"Article {n}",
        "description": "desc",
        "text": text,
        "hostname": "example.com",
    }


DEFAULT = {"description_nl": "Twee zinnen.", "category": "article", "new_category": "",
           "kind": "article", "topic": "", "usable": "nee", "fits": "nee", "junk": "nee",
           "topper": "nee", "fit": 1, "reason_nl": "reden"}


class FakeLLM:
    """Answers by link text; `answers` maps link text -> overrides of DEFAULT."""

    def __init__(self, answers=None, novelty=None, fail_after=None, down=False):
        self.answers = answers or {}
        self.novelty = novelty or {}
        self.fail_after = fail_after
        self.down = down
        self.judge_calls = 0
        self.novelty_calls = 0

    def handler(self, request: httpx.Request):
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake-model"}]})
        body = json.loads(request.content)
        schema_name = body.get("response_format", {}).get("json_schema", {}).get("name")
        user = body["messages"][1]["content"]
        if schema_name == "novelty":
            self.novelty_calls += 1
            title = user.split("New article: ", 1)[1].split("\n", 1)[0]
            adds = self.novelty.get(title, "ja")
            out = {"adds": adds, "what_nl": "voegt benchmarks toe" if adds == "ja" else "niets nieuws"}
        else:
            self.judge_calls += 1
            if self.fail_after is not None and self.judge_calls > self.fail_after:
                raise httpx.ConnectError("gone", request=request)
            lt = user.split("Link text in the newsletter: ", 1)[1].split("\n", 1)[0]
            title = user.split("Title: ", 1)[1].split("\n", 1)[0]
            out = {**DEFAULT, "title": title or lt, **self.answers.get(lt, {})}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(out)}}]})

    def transport(self):
        return httpx.MockTransport(self.handler)


class FakeGitHub:
    def __init__(self, repos):
        self.repos = repos

    def repo(self, name):
        return self.repos.get(name)

    def close(self):
        pass


@pytest.fixture
def cfg(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return config_mod.load(vault_override=vault)
