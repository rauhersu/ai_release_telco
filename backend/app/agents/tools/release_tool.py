"""Release automator tool for the LangGraph agent.

Allows the agent to invoke a Konflux release pipeline dry-run for
telco operators via natural language.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from datetime import date
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

KNOWN_PRODUCTS: dict[str, dict] = {
    "lifecycle-agent": {
        "display_name": "Lifecycle Agent",
        "upstream_repo": "https://github.com/openshift-kni/lifecycle-agent",
        "components": [
            {
                "type": "operator",
                "name": "lifecycle-agent",
                "jira_component": "LCA Operator",
                "fbc_application": "lifecycle-agent-fbc-{OCP}",
                "bundle_application": "lifecycle-agent-{OCP}",
                "fbc_release_plan": "rp-lifecycle-agent-fbc-prod-{OCP}",
                "bundle_release_plan": "releaseplan-lifecycle-agent-production-{OCP}",
            }
        ],
    },
    "topology-aware-lifecycle-manager": {
        "display_name": "Topology Aware Lifecycle Manager",
        "upstream_repo": "https://github.com/openshift-kni/cluster-group-upgrades-operator",
        "components": [
            {
                "type": "operator",
                "name": "topology-aware-lifecycle-manager",
                "jira_component": "TALM Operator",
                "fbc_application": "topology-aware-lifecycle-manager-fbc-{OCP}",
                "bundle_application": "topology-aware-lifecycle-manager-{OCP}",
                "fbc_release_plan": "rp-topology-aware-lifecycle-manager-fbc-prod-{OCP}",
                "bundle_release_plan": "releaseplan-topology-aware-lifecycle-manager-production-{OCP}",
            }
        ],
    },
    "numaresources-operator": {
        "display_name": "NUMA Resources Operator",
        "upstream_repo": "https://github.com/openshift-kni/numaresources-operator",
        "components": [
            {
                "type": "operator",
                "name": "numaresources-operator",
                "jira_component": "NROP Operator",
                "fbc_application": "numaresources-operator-fbc-{OCP}",
                "bundle_application": "numaresources-operator-{OCP}",
                "fbc_release_plan": "rp-numaresources-operator-fbc-prod-{OCP}",
                "bundle_release_plan": "releaseplan-numaresources-operator-production-{OCP}",
            }
        ],
    },
    "o-cloud-manager": {
        "display_name": "O-Cloud Manager",
        "upstream_repo": "",
        "components": [
            {
                "type": "operator",
                "name": "oran-o2ims",
                "jira_component": "O-Cloud Manager Operator",
                "fbc_application": "oran-o2ims-fbc-{OCP}",
                "bundle_application": "oran-o2ims-{OCP}",
                "fbc_release_plan": "rp-oran-o2ims-fbc-prod-{OCP}",
                "bundle_release_plan": "releaseplan-oran-o2ims-production-{OCP}",
            }
        ],
    },
    "ztp-site-generate": {
        "display_name": "ZTP Site Generate",
        "upstream_repo": "",
        "components": [
            {
                "type": "operator",
                "name": "ztp-site-generate",
                "jira_component": "ZTP Site Generate",
                "fbc_application": "ztp-site-generate-fbc-{OCP}",
                "bundle_application": "ztp-site-generate-{OCP}",
                "fbc_release_plan": "rp-ztp-site-generate-fbc-prod-{OCP}",
                "bundle_release_plan": "releaseplan-ztp-site-generate-production-{OCP}",
            }
        ],
    },
}


def _build_manifest(product: str, version: str, ocp_version: str) -> dict:
    """Build a release manifest dict from a known product template."""
    info = KNOWN_PRODUCTS[product]
    ocp_dash = ocp_version.replace(".", "-")

    is_ga = version.endswith(".0")
    release_type = "ga" if is_ga else "z-stream"
    upstream_ref = f"release-{ocp_version}" if info["upstream_repo"] else ""

    components = []
    for comp_template in info["components"]:
        comp = {}
        for key, value in comp_template.items():
            if isinstance(value, str):
                comp[key] = value.replace("{OCP}", ocp_dash)
            else:
                comp[key] = value
        components.append(comp)

    return {
        "product": info["display_name"],
        "version": version,
        "release_date": str(date.today()),
        "ocp_version": ocp_version,
        "release_type": release_type,
        "jira_template": "CNF-19096",
        "author": "ai-release-agent",
        "upstream_repo": info["upstream_repo"],
        "upstream_ref": upstream_ref,
        "components": components,
    }


def run_release_dry_run(product: str, version: str, ocp_version: str) -> str:
    """Generate a manifest and run the release pipeline in dry-run mode.

    Args:
        product: Product key (e.g. 'lifecycle-agent').
        version: Release version (e.g. '4.21.2').
        ocp_version: Target OCP version (e.g. '4.21').

    Returns:
        Combined stdout/stderr from the CLI, or an error message.
    """
    if product not in KNOWN_PRODUCTS:
        available = ", ".join(sorted(KNOWN_PRODUCTS.keys()))
        return f"Unknown product '{product}'. Available products: {available}"

    manifest_data = _build_manifest(product, version, ocp_version)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=f"release-{product}-", delete=False
    ) as f:
        yaml.dump(manifest_data, f, default_flow_style=False)
        manifest_path = f.name

    logger.info("Generated manifest at %s for %s %s", manifest_path, product, version)

    try:
        result = subprocess.run(
            ["release-pipeline", "run", "--dry-run", "-m", manifest_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return output.strip() or "(no output)"
    except FileNotFoundError:
        return "Error: 'release-pipeline' CLI not found in PATH."
    except subprocess.TimeoutExpired:
        return "Error: release-pipeline command timed out after 30 seconds."
    finally:
        Path(manifest_path).unlink(missing_ok=True)


def read_release_state(product: str, version: str) -> str:
    """Read the state file produced by a release pipeline run.

    Args:
        product: Product display name or key (e.g. 'lifecycle-agent').
        version: Release version (e.g. '4.21.2').

    Returns:
        The YAML contents of the state file, or an error message.
    """
    state_dir = Path(".state")
    filename = f"{product}-{version}.state.yaml"
    state_path = state_dir / filename

    if state_path.exists():
        return state_path.read_text()

    # Try matching by display name (e.g. "Lifecycle Agent" -> lookup)
    for key, info in KNOWN_PRODUCTS.items():
        if info["display_name"].lower() == product.lower():
            alt_path = state_dir / f"{key}-{version}.state.yaml"
            if alt_path.exists():
                return alt_path.read_text()

    if not state_dir.exists():
        return "No .state/ directory found. No releases have been run yet."

    available = [f.name for f in state_dir.glob("*.state.yaml")]
    if available:
        return f"State file '{filename}' not found. Available state files: {', '.join(available)}"
    return "No state files found. Run a release pipeline first."


def list_release_states() -> str:
    """List all release pipeline state files.

    Returns:
        A summary of available state files, or a message if none exist.
    """
    state_dir = Path(".state")
    if not state_dir.exists():
        return "No .state/ directory found. No releases have been run yet."

    files = sorted(state_dir.glob("*.state.yaml"))
    if not files:
        return "No state files found."

    lines = []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text())
            product = data.get("product", "?")
            version = data.get("version", "?")
            phase = data.get("current_phase", "?")
            lines.append(f"- {f.name}: {product} {version} (phase: {phase})")
        except Exception:
            lines.append(f"- {f.name}: (could not parse)")
    return "Release state files:\n" + "\n".join(lines)
