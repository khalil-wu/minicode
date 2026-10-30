# Public plugin catalog

`public-catalog.json` and the files in `frontend/public/apps/plugin-logos/` are
generated from the public [OpenAI plugins repository](https://github.com/openai/plugins)
at revision `0722921d5542fc593105c27bd52630babd8b8c2a`. Each plugin's own
manifest and source repository determine its terms and product restrictions.
The catalog also records the pinned revisions of the three external sources
declared in that marketplace. Images are the logos declared by those manifests.

Run `python scripts/update-plugin-catalog.py <revision>` to regenerate the
snapshot from a checkout of the requested public revision. The snapshot is
discovery metadata; installing a plugin obtains its actual package from its
declared source. No plugin is installed or enabled by including it here.
