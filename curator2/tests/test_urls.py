

def test_kit_tracking_link_is_unwrapped():
    from curator2.urls import clean, github_repo, normalize, unwrap
    a = ("https://fff97757.click.kit-mail3.com/p9uggq7k8ob9hdk3px5bqhppvlwgrbrh2g4pp/"
         "m2h7h5h3xmr007imhq/aHR0cHM6Ly9naXRodWIuY29tL29ueXgtZG90LWFwcC9vbnl4")
    b = "https://d0ugg5.click.kit-mail3.com/zzz/yyy/aHR0cHM6Ly9naXRodWIuY29tL29ueXgtZG90LWFwcC9vbnl4"
    assert unwrap(a) == "https://github.com/onyx-dot-app/onyx"
    assert normalize(a) == normalize(b) == "github.com/onyx-dot-app/onyx"
    assert github_repo(a) == "onyx-dot-app/onyx"
    assert clean(a) == "https://github.com/onyx-dot-app/onyx"
    # ordinary links and look-alikes stay as they are
    assert unwrap("https://example.com/a/b") == "https://example.com/a/b"
    assert unwrap("https://example.com/aHR0cHM6Ly9ub3QgYSB1cmw") == "https://example.com/aHR0cHM6Ly9ub3QgYSB1cmw"
