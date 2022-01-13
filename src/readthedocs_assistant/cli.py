from __future__ import annotations

import asyncio
import base64
import logging  # TODO: Migrate to structlog
import os
import re
from typing import TYPE_CHECKING, Any, cast

import gidgethub
import gidgethub.httpx
import httpx
from jsonschema import validate
from yaml import Loader, load

from .migrators import use_build_tools
from .types import RTDConfig

if TYPE_CHECKING:
    from gidgethub.abc import GitHubAPI


logger = logging.getLogger(__name__)


# https://github.com/readthedocs/readthedocs.org/blob/2e1b121d/readthedocs/config/config.py#L59
CONFIG_FILENAME_REGEX = r"^\.?readthedocs.ya?ml$"

# https://www.schemastore.org/json/
SCHEMA_URL = (
    "https://raw.githubusercontent.com/readthedocs/readthedocs.org/"
    "master/readthedocs/rtd_tests/fixtures/spec/v2/schema.json"
)


async def fork_repo(owner: str, repository_name: str, *, gh: GitHubAPI) -> Any:
    # Create fork
    try:
        await gh.post(f"/repos/{owner}/{repository_name}/forks", data={})
    except gidgethub.HTTPException as exc:
        # 202 ACCEPTED raises an error,
        # see https://github.com/brettcannon/gidgethub/issues/171
        if exc.status_code != 202:
            raise

    forked_repo = await gh.getitem(f"/repos/readthedocs-assistant/{repository_name}")
    logger.debug(forked_repo)
    return forked_repo


async def find_config(repo: Any, *, gh: GitHubAPI) -> Any:
    default_branch = await gh.getitem(
        f"/repos/{repo['full_name']}/branches/{repo['default_branch']}"
    )

    tip_sha = default_branch["commit"]["sha"]
    tree = await gh.getitem(f"/repos/{repo['full_name']}/git/trees/{tip_sha}")
    logger.debug(tree)

    for item in tree["tree"]:
        if item["type"] == "blob" and re.match(CONFIG_FILENAME_REGEX, item["path"]):
            # TODO: Error early if there are more than one config files
            return item


async def load_contents(
    repo: Any, path: str, encoding: str = "utf-8", *, gh: GitHubAPI
) -> str:
    file_contents = await gh.getitem(f"/repos/{repo['full_name']}/contents/{path}")
    content = base64.b64decode(file_contents["content"].encode("ascii")).decode(
        encoding
    )
    return content


async def validate_config(
    config: Any, schema_url: str = SCHEMA_URL, *, client: httpx.AsyncClient
) -> RTDConfig:
    resp_schema = await client.get(SCHEMA_URL)
    resp_schema.raise_for_status()
    schema = resp_schema.json()
    logger.debug(schema)

    validate(instance=config, schema=schema)

    return cast(RTDConfig, config)


async def main(username: str, token: str, owner: str, repository_name: str) -> None:
    async with httpx.AsyncClient() as client:
        gh = gidgethub.httpx.GitHubAPI(client, username, oauth_token=token)

        all_repos = gh.getiter("/user/repos")
        logger.debug("%d repos found", len([r async for r in all_repos]))

        forked_repo = await fork_repo(owner, repository_name, gh=gh)
        logger.info("%s created", forked_repo["full_name"])

        config_item = await find_config(forked_repo, gh=gh)
        assert config_item

        unvalidated_config = load(
            await load_contents(forked_repo, config_item["path"], gh=gh), Loader=Loader
        )
        config = await validate_config(unvalidated_config, client=client)

        # At this point, the repository is forked and the configuration is validated
        # and we can do whatever change we want to do
        logger.info("Current config: %s", config)

        # For example, migrate to build.tools
        new_config = await use_build_tools(config)

        logger.info("New config: %s", new_config)

        # TODO: Create pull request with message
        # TODO: Add interactive/dry run mode to manually compare changes
        # before opening pull request


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # TODO: Add cli parameter to pick migrator
    # TODO: Detect migrations and write small report
    asyncio.run(
        main(
            os.environ["GH_USERNAME"],
            os.environ["GH_TOKEN"],
            "jupyterlite",  # TODO: Do not hardcode repositories
            "jupyterlite",
        )
    )
