import reflex as rx

config = rx.Config(
    app_name="newsletter_curator",
    app_module_import="src.web.app",
    disable_plugins=["reflex.plugins.sitemap.SitemapPlugin"],
)
