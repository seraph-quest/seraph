"""#1008: the backend image builds and ships the reviewed Cordis research runtime.

Static wiring only: no image is built here. It asserts the Compose context, the
pinned Node build stage, the copied reviewed sources and that the final image
places the package where the backend resolves it.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re

import yaml

from src.runtime_plugins.composition import BUILD_FILES, NPM_VERSION

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = (REPO / "backend/Dockerfile").read_text()
NODE = "24.13.1"


def test_compose_passes_runtime_cordis_as_a_named_build_context():
    for name, service in (("dev", "backend-dev"), ("prod", "backend-prod")):
        build = yaml.safe_load((REPO / f"docker-compose.{name}.yaml").read_text())["services"][service]["build"]
        # Backend context unchanged; Compose resolves additional contexts from the project directory.
        assert build["context"] == "./backend" and build["dockerfile"] == "./Dockerfile"
        assert build["additional_contexts"] == {"cordis": "./runtime/cordis"}
        environment = yaml.safe_load((REPO / f"docker-compose.{name}.yaml").read_text())["services"][service]["environment"]
        # Compose `environment` overrides `env_file`, so a host CORDIS_NODE_PATH cannot replace the image's Node.
        assert environment["CORDIS_NODE_PATH"] == "/usr/local/bin/node"
        assert (REPO / build["additional_contexts"]["cordis"] / "package-lock.json").is_file()


def test_dockerfile_builds_the_reviewed_package_in_a_pinned_node_stage():
    stages = re.findall(r"^FROM (\S+)(?: AS (\S+))?$", DOCKERFILE, re.M)
    # The exact Node tag, pinned by an image digest (#1113) so a re-pushed tag never changes the build.
    assert re.fullmatch(rf"node:{re.escape(NODE)}-bookworm-slim@sha256:[0-9a-f]{{64}}", stages[0][0]), stages[0]
    assert stages[0][1] == "cordis-build"
    assert stages[-1][0] == "ghcr.io/astral-sh/uv:python3.12-bookworm-slim" and stages[-1][1] == ""
    build = DOCKERFILE[DOCKERFILE.index("AS cordis-build"):DOCKERFILE.index("FROM ghcr.io/astral-sh/uv")]
    assert f'test "$(node --version)" = "v{NODE}"' in build and f'test "$(npm --version)" = "{NPM_VERSION}"' in build
    assert "npm ci --ignore-scripts" in build and "npm run build" in build and "npm install" not in build
    copied = {item for line in re.findall(r"^COPY --from=cordis (.+)$", build, re.M) for item in line.split()[:-1]}
    # Every reviewed non-output file comes from the named context; host dist/node_modules never do.
    roots = {PurePosixPath(path).parts[0] for path in BUILD_FILES if not path.startswith("dist/")}
    assert roots <= copied and not copied & {".", "dist", "node_modules"}


def test_final_image_places_node_and_package_where_the_backend_resolves_them():
    final = DOCKERFILE[DOCKERFILE.index("FROM ghcr.io/astral-sh/uv"):]
    assert "COPY --from=cordis-build /usr/local/bin/node /usr/local/bin/node" in final
    assert "COPY --from=cordis-build /runtime/cordis /runtime/cordis" in final
    # The backend lifespan hands this reviewed Node to cordis_host (settings.cordis_node_path).
    assert re.search(r"^ENV CORDIS_NODE_PATH=/usr/local/bin/node$", final, re.M)
    from config.settings import Settings
    assert "cordis_node_path" in Settings.model_fields
    # The image ADDs backend/ at /app; composition.PACKAGE_ROOT is parents[3] / runtime / cordis.
    assert re.search(r"^ADD \. /app$", final, re.M) and re.search(r"^WORKDIR /app$", final, re.M)
    in_image = PurePosixPath("/app/src/runtime_plugins/composition.py").parents[3] / "runtime" / "cordis"
    assert str(in_image) == "/runtime/cordis"
    workflow = (REPO / ".github/workflows/test.yml").read_text()
    assert f'node-version: "{NODE}"' in workflow and f'test "$(npm --version)" = "{NPM_VERSION}"' in workflow
