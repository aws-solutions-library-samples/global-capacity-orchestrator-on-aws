#!/usr/bin/env bats
# ─────────────────────────────────────────────────────────────────────────────
# BATS tests for .github/scripts/dependency-scan.sh — the whole driver
# ─────────────────────────────────────────────────────────────────────────────
# test_dependency_scan.bats covers the library the driver is built from. This
# suite runs the driver itself, end to end, against a faked upstream world
# (see dependency_scan_fakes.sh): every registry and AWS API it consults is a
# fake on PATH answering from a catalog of the pins in the tree the scan is
# pointed at, so a run is "everything current", "everything one release
# newer", "no credentials", "network down", or "nothing to parse" on request.
# The trees are a small hand-built checkout whose pins all agree (the only way
# to reach the all-clear exit deterministically), the real repository (the
# shapes the scan meets in production), and an empty directory (every
# missing-file branch at once).
#
# Run:  bats tests/BATS/test_dependency_scan_driver.bats
# ─────────────────────────────────────────────────────────────────────────────

load 'helpers.sh'

SCRIPT="$REPO_ROOT/.github/scripts/dependency-scan.sh"
LIB="$REPO_ROOT/.github/scripts/lib_dependency_scan.sh"
FAKE_LIB="$REPO_ROOT/tests/BATS/dependency_scan_fakes.sh"

# A 64-hex digest that is obviously fabricated.
DIGEST_A="1111111111111111111111111111111111111111111111111111111111111111"
DIGEST_B="2222222222222222222222222222222222222222222222222222222222222222"

setup() {
    # shellcheck source=.github/scripts/lib_dependency_scan.sh
    source "$LIB"
    FAKE_BIN="$BATS_TEST_TMPDIR/bin"
    export FAKE_CATALOG="$BATS_TEST_TMPDIR/catalog"
    export FAKE_LIB
    export CALLS="$BATS_TEST_TMPDIR/calls"
    : > "$CALLS"
    REAL_PYTHON3="$(command -v python3)"
    REAL_SHA256SUM="$(command -v sha256sum || true)"
    export REAL_PYTHON3 REAL_SHA256SUM
    # The library's constants helpers try `from gco.stacks.constants import`
    # before falling back to reading the source file. A host interpreter that
    # has the project installed (an editable install from another checkout,
    # say) would answer that import for an empty or fixture tree and hide the
    # fallback. Shadowing the package on PYTHONPATH leaves the tree under scan
    # as the only place it can come from: for `python3 -c` the working
    # directory precedes PYTHONPATH on sys.path, so a real checkout still wins.
    export FAKE_PYSHADOW="$BATS_TEST_TMPDIR/pyshadow"
    mkdir -p "$FAKE_PYSHADOW/gco"
    printf 'raise ImportError("gco is importable only from the tree under scan")\n' \
        > "$FAKE_PYSHADOW/gco/__init__.py"
    make_fakes
}

# ── The faked upstream world ─────────────────────────────────────────────────

make_fakes() {
    # curl: routes on the URL and answers from the catalog. Understands the
    # two call shapes the scan uses (body on stdout; `-o FILE -w %{http_code}`).
    # FAKE_NETWORK=down fails every request. FAKE_COMPANIONS=unhealthy answers
    # deprecated/yanked for packages the catalog does not know; =missing 404s.
    write_stub "$FAKE_BIN" curl <<'FAKE_CURL'
#!/usr/bin/env bash
source "$FAKE_LIB"
url="" out="" write_out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        -w) write_out="$2"; shift 2 ;;
        -H|--max-time) shift 2 ;;
        -*) shift ;;
        *) url="$1"; shift ;;
    esac
done
printf 'curl %s\n' "$url" >> "$CALLS"
reply() {
    local code="$1" body="$2"
    if [ -n "$out" ]; then printf '%s' "$body" > "$out"; else printf '%s' "$body"; fi
    [ -n "$write_out" ] && printf '%s' "$code"
    [ "$code" = "200" ]
}
unreachable() {
    [ -n "$write_out" ] && printf '000'
    exit 22
}
[ "${FAKE_NETWORK:-up}" = "down" ] && unreachable
case "$url" in
    *pypi.org/pypi/*/json)
        pkg="${url#*pypi.org/pypi/}"; pkg="${pkg%/json}"
        known="$(catalog_get pypi "$pkg")"
        [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "unreachable" ] && unreachable
        if [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "missing" ]; then reply 404 '{"message": "Not Found"}'; exit 0; fi
        yanked=false
        [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "unhealthy" ] && yanked=true
        reply 200 "{\"info\": {\"version\": \"$(answer pypi "$pkg" 1.0.0)\"}, \"urls\": [{\"yanked\": ${yanked}}]}"
        ;;
    *registry.npmjs.org/*/latest)
        pkg="${url#*registry.npmjs.org/}"; pkg="${pkg%/latest}"; pkg="${pkg//%2F//}"
        known="$(catalog_get npm "$pkg")"
        case "$pkg" in
            @anthropic-ai/claude-code|@openai/codex)
                if [ "${FAKE_ENGINE_PACKAGES:-ok}" = "deprecated" ]; then
                    reply 200 '{"version": "9.9.9", "deprecated": "this engine has moved to a new package"}'; exit 0
                fi
                ;;
        esac
        [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "unreachable" ] && unreachable
        if [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "missing" ]; then reply 404 '{"error": "Not found"}'; exit 0; fi
        if [ -z "$known" ] && [ "${FAKE_COMPANIONS:-healthy}" = "unhealthy" ]; then
            reply 200 '{"version": "1.0.0", "deprecated": "use the maintained fork instead"}'; exit
        fi
        reply 200 "{\"version\": \"$(answer npm "$pkg" 1.0.0)\"}"
        ;;
    *api.github.com/repos/*/releases/latest)
        repo="${url#*api.github.com/repos/}"; repo="${repo%/releases/latest}"
        reply 200 "{\"tag_name\": \"$(answer github-release "$repo" v1.0.0)\"}"
        ;;
    *api.github.com/repos/*/tags*)
        repo="${url#*api.github.com/repos/}"; repo="${repo%%/tags*}"
        if [ "$repo" = "aws/aws-cli" ]; then
            # The scan only accepts 2.x.y tags for the AWS CLI, so "newer"
            # has to stay inside the major.
            tag="$(catalog_get github-tags "$repo")"
            [ "${FAKE_DRIFT:-0}" = "1" ] && tag="$(bump_last "$tag")"
        else
            tag="$(answer github-tags "$repo" v1.0.0)"
        fi
        reply 200 "[{\"name\": \"${tag}\"}, {\"name\": \"nightly\"}, {\"name\": \"v0.0.1\"}]"
        ;;
    *nodejs/Release/main/schedule.json)
        major="$(answer node-lts major 24)"
        reply 200 "{\"v${major}\": {\"lts\": \"2020-10-01\", \"end\": \"2999-04-30\"}, \"v9999\": {\"lts\": \"2999-01-01\"}, \"v1\": {\"lts\": \"2010-01-01\", \"end\": \"2011-01-01\"}}"
        ;;
    *nodejs.org/dist/index.json)
        reply 200 "[{\"version\": \"$(answer node dist v24.0.0)\"}, {\"version\": \"v1.0.0\"}]"
        ;;
    *dl.k8s.io/release/stable-*.txt)
        minor="${url#*stable-}"; minor="${minor%.txt}"
        reply 200 "$(answer k8s-stable "$minor" "v${minor}.0")"
        ;;
    *endoflife.date/api/*.json)
        [ "${FAKE_ENDOFLIFE:-up}" = "down" ] && unreachable
        product="${url#*endoflife.date/api/}"; product="${product%.json}"
        reply 200 "[{\"cycle\": \"$(answer endoflife "$product" 1.0)\", \"releaseDate\": \"2020-01-01\", \"eol\": false}]"
        ;;
    *) reply 404 ''; exit 0 ;;
esac
FAKE_CURL

    # aws: the credential probe and the five lookups, answered from the tree's
    # own pins. FAKE_AWS=none removes the credentials; the per-section switches
    # provoke each lookup's failure branch.
    write_stub "$FAKE_BIN" aws <<'FAKE_AWS'
#!/usr/bin/env bash
source "$FAKE_LIB"
printf 'aws %s\n' "$*" >> "$CALLS"
case "${1:-} ${2:-}" in
    "sts get-caller-identity")
        [ "${FAKE_AWS:-ok}" = "none" ] && exit 255
        echo '{"Account": "123456789012"}'
        ;;
    "eks describe-addon-versions")
        [ "${FAKE_AWS_ADDON:-ok}" = "invalid" ] && { echo "None"; exit 0; }
        name=""
        while [ "$#" -gt 0 ]; do [ "$1" = "--addon-name" ] && name="$2"; shift; done
        answer eks-addon "$name" "v1.0.0-eksbuild.1"; echo
        ;;
    "eks describe-cluster-versions")
        case "${FAKE_AWS_K8S:-ok}" in
            fail) echo "An error occurred (AccessDeniedException) when calling DescribeClusterVersions" >&2; exit 254 ;;
            empty) echo '{"clusterVersions": []}' ;;
            *)
                current="$(catalog_get k8s current)"
                latest="$(answer k8s current 1.36)"
                printf '{"clusterVersions": [{"clusterVersion": "%s", "endOfStandardSupportDate": "2027-01-15T00:00:00+00:00"}, {"clusterVersion": "%s", "endOfStandardSupportDate": "2028-01-15T00:00:00+00:00"}]}\n' "$current" "$latest"
                ;;
        esac
        ;;
    "rds describe-db-engine-versions")
        [ "${FAKE_AWS_AURORA:-ok}" = "invalid" ] && { echo "unavailable"; exit 0; }
        printf '%s\t%s\n' "$(catalog_get aurora current)" "$(answer aurora current 17.0)"
        ;;
    "emr list-release-labels")
        current="$(catalog_get emr current)"
        case "${FAKE_AWS_EMR:-ok}" in
            empty) exit 0 ;;
            unparseable) echo "emr-preview" ;;
            *)
                labels="$current emr-7.0.0-preview"
                if [ "${FAKE_DRIFT:-0}" = "1" ]; then
                    [ "${FAKE_EMR_DRIFT:-patch}" = "patch" ] && labels="$labels $(bump_last "$current")"
                    labels="$labels $(bump "$current")"
                fi
                printf '%s\n' "$labels" | tr ' ' '\t'
                ;;
        esac
        ;;
    "bedrock list-inference-profiles")
        [ "${FAKE_AWS_BEDROCK:-ok}" = "empty" ] && { echo '{"inferenceProfileSummaries": []}'; exit 0; }
        printf '{"inferenceProfileSummaries": ['
        first=1
        while IFS= read -r pid; do
            [ -n "$pid" ] || continue
            [ "$first" -eq 1 ] || printf ','
            first=0
            printf '{"inferenceProfileId": "%s", "status": "ACTIVE"}' "$pid"
            [ "${FAKE_DRIFT:-0}" = "1" ] && printf ',{"inferenceProfileId": "%s", "status": "ACTIVE"},{"inferenceProfileId": "%s", "status": "LEGACY"}' "$(bump "$pid")" "$(bump "$(bump "$pid")")"
        done < <(catalog_values bedrock-profile | sort -u)
        printf ']}\n'
        ;;
    "bedrock list-foundation-models")
        [ "${FAKE_AWS_BEDROCK:-ok}" = "empty" ] && { echo '{"modelSummaries": []}'; exit 0; }
        printf '{"modelSummaries": ['
        first=1
        while IFS= read -r mid; do
            [ -n "$mid" ] || continue
            [ "$first" -eq 1 ] || printf ','
            first=0
            printf '{"modelId": "%s", "modelLifecycle": {"status": "ACTIVE"}}' "$mid"
            [ "${FAKE_DRIFT:-0}" = "1" ] && printf ',{"modelId": "%s", "modelLifecycle": {"status": "ACTIVE"}}' "$(bump "$mid")"
        done < <(catalog_values bedrock-embedding | sort -u)
        printf ']}\n'
        ;;
    *) exit 0 ;;
esac
FAKE_AWS

    # skopeo: the tag list of every image the tree pins (plus newer tags under
    # drift) and a stand-in manifest for the digest check.
    write_stub "$FAKE_BIN" skopeo <<'FAKE_SKOPEO'
#!/usr/bin/env bash
source "$FAKE_LIB"
printf 'skopeo %s\n' "$*" >> "$CALLS"
[ "${FAKE_NETWORK:-up}" = "down" ] && exit 1
argv="$*"
case "${1:-}" in
    list-tags)
        ref="${argv##*docker://}"
        # FAKE_SKOPEO=unlisted: the registry answers, but with a tag list
        # that no longer carries the pinned tag and offers nothing newer.
        [ "${FAKE_SKOPEO:-ok}" = "unlisted" ] && { echo '{"Tags": ["0.0.1"]}'; exit 0; }
        tags=()
        while IFS= read -r tag; do
            [ -n "$tag" ] || continue
            tags+=("$tag")
            # Under drift both a new major and a new patch exist: the image
            # sweep picks the newest same-variant tag, the kind node check
            # only looks inside the pinned minor.
            [ "${FAKE_DRIFT:-0}" = "1" ] && tags+=("$(bump "$tag")" "$(bump_last "$tag")")
        done < <(catalog_values image "$ref" | sort -u)
        printf '{"Tags": %s}\n' "$(json_list "${tags[@]+"${tags[@]}"}")"
        ;;
    inspect)
        ref="${argv##*docker://}"
        printf 'FAKE-MANIFEST %s\n' "$ref"
        ;;
esac
FAKE_SKOPEO
    # sha256sum: a stand-in manifest hashes to the digest the tree committed
    # for that tag (or, under drift, to a different one); anything else is
    # hashed for real.
    write_stub "$FAKE_BIN" sha256sum <<'FAKE_SHA256SUM'
#!/usr/bin/env bash
source "$FAKE_LIB"
file="${!#}"
if [ -f "$file" ] && [ "$(head -c 14 "$file" 2>/dev/null)" = "FAKE-MANIFEST " ]; then
    ref="$(sed -n '1s/^FAKE-MANIFEST //p' "$file")"
    digest="$(catalog_get digest "$ref")"
    [ -n "$digest" ] || digest="3333333333333333333333333333333333333333333333333333333333333333"
    [ "${FAKE_DRIFT:-0}" = "1" ] && digest="4444${digest#????}"
    printf '%s  %s\n' "$digest" "$file"
    exit 0
fi
exec "$REAL_SHA256SUM" "$@"
FAKE_SHA256SUM

    # helm: repository refresh, index search and OCI chart inspection.
    write_stub "$FAKE_BIN" helm <<'FAKE_HELM'
#!/usr/bin/env bash
source "$FAKE_LIB"
printf 'helm %s\n' "$*" >> "$CALLS"
case "${1:-} ${2:-}" in
    "repo add") [ "${FAKE_NETWORK:-up}" = "down" ] && exit 1; exit 0 ;;
    "search repo")
        version="$(answer helm "$3")"
        if [ -n "$version" ]; then printf '[{"version": "%s"}]\n' "$version"; else echo '[]'; fi
        ;;
    "show chart")
        [ "${FAKE_HELM:-ok}" = "oci-fails" ] && exit 1
        version="$(answer helm-oci "$3")"
        [ -n "$version" ] && printf 'apiVersion: v2\nversion: %s\n' "$version"
        ;;
esac
FAKE_HELM

    # pip: the editable install and the outdated listing. FAKE_PIP=install-fails
    # | list-fails | malformed; under drift every direct dependency is outdated.
    write_stub "$FAKE_BIN" pip <<'FAKE_PIP'
#!/usr/bin/env bash
source "$FAKE_LIB"
printf 'pip %s\n' "$*" >> "$CALLS"
case "${1:-}" in
    install) [ "${FAKE_PIP:-ok}" = "install-fails" ] && exit 1; exit 0 ;;
    list)
        case "${FAKE_PIP:-ok}" in
            list-fails) exit 1 ;;
            malformed) echo "not json"; exit 0 ;;
        esac
        if [ "${FAKE_DRIFT:-0}" != "1" ]; then echo "[]"; exit 0; fi
        printf '['
        first=1
        while IFS='|' read -r kind name version; do
            [ "$kind" = "pydep" ] || continue
            [ "$first" -eq 1 ] || printf ','
            first=0
            printf '{"name": "%s", "version": "%s", "latest_version": "%s"}' "$name" "$version" "$(bump "$version")"
        done < "$FAKE_CATALOG"
        # A transitive that is not a direct pin must be filtered out.
        [ "$first" -eq 1 ] || printf ','
        printf '{"name": "some-transitive", "version": "1.0.0", "latest_version": "2.0.0"}]\n'
        ;;
esac
FAKE_PIP

    # python3: the two helper programs the scan runs and the aws-cdk probes
    # are answered here; everything else — the library's own extractors, the
    # scan's inline scripts — runs on the real interpreter, with the project
    # package shadowed (see setup) so only the tree under scan can supply it.
    write_stub "$FAKE_BIN" python3 <<'FAKE_PYTHON'
#!/usr/bin/env bash
source "$FAKE_LIB"
export PYTHONPATH="$FAKE_PYSHADOW${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-}" in
    *check_runner_images.py)
        case "${FAKE_RUNNER_IMAGES:-current}" in
            fail) exit 2 ;;
            drift)
                [ "${3:-}" = "rows" ] && echo "ubuntu-latest|ubuntu-22.04|ubuntu-24.04"
                [ "${3:-}" = "notes" ] && echo "macos-15|macos-15|macos-26"
                ;;
            *) [ "${3:-}" = "notes" ] && echo "ubuntu-latest|ubuntu-24.04|ubuntu-26.04" ;;
        esac
        exit 0
        ;;
    *accelerator_catalog.py)
        out="" summary=0
        while [ "$#" -gt 0 ]; do
            case "$1" in --output|--report) out="$2"; shift 2 ;; --json-summary) summary=1; shift ;; *) shift ;; esac
        done
        case "${FAKE_ACCELERATOR:-clean}" in
            clean)
                [ -n "$out" ] && : > "$out"
                [ "$summary" -eq 1 ] && echo '{"status": "current", "drift_count": 0}'
                exit 0 ;;
            drift)
                [ -n "$out" ] && printf '## Findings\n\n### Retire p2 from the GPU pool\n\n### Add p6 to the watch list\n' > "$out"
                [ "$summary" -eq 1 ] && echo '{"status": "drift", "drift_count": 2}'
                exit 1 ;;
            drift-unparseable)
                [ -n "$out" ] && printf 'drift, but no headings\n' > "$out"
                [ "$summary" -eq 1 ] && echo '{"status": "drift", "drift_count": 2}'
                exit 1 ;;
            inconsistent)
                [ "$summary" -eq 1 ] && echo '{"status": "drift", "drift_count": 2}'
                exit 0 ;;
            malformed)
                [ "$summary" -eq 1 ] && echo 'not json'
                exit 0 ;;
            error)
                echo "boto3 is not installed" >&2
                exit 2 ;;
        esac
        ;;
    -c)
        case "${2:-}" in
            "import aws_cdk") [ "${FAKE_AWS_CDK:-absent}" = "present" ] && exit 0; exit 1 ;;
            *"e.get('name', '')"*)
                # The scan's own outdated-package filter; FAKE_PIP_FILTER=fail
                # is a broken interpreter, the one way that filter can fail.
                [ "${FAKE_PIP_FILTER:-ok}" = "fail" ] && exit 1
                ;;
            *"from aws_cdk import aws_lambda"*)
                case "${2:-}" in
                    *PYTHON_*) answer cdk-enum LAMBDA_PYTHON_RUNTIME; echo ;;
                    *NODEJS_*) answer cdk-enum LAMBDA_NODEJS_RUNTIME; echo ;;
                esac
                exit 0 ;;
        esac
        ;;
esac
exec "$REAL_PYTHON3" "$@"
FAKE_PYTHON
}

# build_catalog <root> — record the pins of the tree at <root> in $FAKE_CATALOG,
# using the scan's own extractors (this test process has the library sourced).
build_catalog() {
    (
        cd "$1" || exit 1
        {
            list_npm_package_dirs . | while IFS= read -r dir; do
                [ -n "$dir" ] || continue
                extract_npm_direct_pins "$dir/package.json" | sed 's/^/npm|/'
            done
            extract_dockerfile_pins Dockerfile.dev | while IFS='|' read -r name value; do
                case "$name" in
                    NPM_VERSION) echo "npm|npm|$value" ;;
                    CDK_VERSION) echo "npm|aws-cdk|$value" ;;
                    NODE_VERSION) echo "node|dist|$value"; echo "node-lts|major|$(printf '%s' "${value#v}" | cut -d. -f1)" ;;
                    KUBECTL_VERSION) echo "k8s-stable|$(printf '%s' "${value#v}" | cut -d. -f1-2)|$value" ;;
                    AWSCLI_VERSION) echo "github-tags|aws/aws-cli|$value" ;;
                    DOCKER_VERSION) echo "github-release|moby/moby|docker-v$value" ;;
                    BUILDX_VERSION) echo "github-release|docker/buildx|$value" ;;
                    UV_VERSION) echo "github-release|astral-sh/uv|$value" ;;
                esac
            done
            echo "npm|@anthropic-ai/claude-code|$(extract_claude_code_pin cli/autopilot.py)"
            echo "npm|@openai/codex|$(extract_codex_pin cli/autopilot.py)"
            extract_build_system_pins pyproject.toml | while IFS='|' read -r name version _raw; do
                [ -n "$version" ] && echo "pypi|$name|$version"
            done
            python3 - <<'PY'
import re, tomllib
try:
    data = tomllib.load(open("pyproject.toml", "rb"))
except Exception:
    raise SystemExit(0)
project = data.get("project", {}) or {}
specs = list(project.get("dependencies", []) or [])
for group in (project.get("optional-dependencies") or {}).values():
    specs.extend(group or [])
for spec in specs:
    m = re.search(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;]+)", spec)
    if m:
        print(f"pydep|{re.sub(r'[-_.]+', '-', m.group(1)).lower()}|{m.group(2)}")
PY
            echo "github-release|aquasecurity/trivy|$(extract_install_trivy_pin .github/actions/install-trivy/action.yml)"
            echo "github-release|rhysd/actionlint|$(extract_workflow_env_pin ACTIONLINT_VERSION | head -1)"
            echo "github-release|helm/helm|$(extract_helm_installer_pins lambda/helm-installer/Dockerfile | awk -F'|' '$1=="HELM_VERSION"{print $2}')"
            installer_kubectl="$(extract_helm_installer_pins lambda/helm-installer/Dockerfile | awk -F'|' '$1=="KUBECTL_VERSION"{print $2}')"
            [ -n "$installer_kubectl" ] && echo "k8s-stable|$(printf '%s' "${installer_kubectl#v}" | cut -d. -f1-2)|$installer_kubectl"
            echo "github-release|yannh/kubeconform|$(extract_workflow_env_pin KUBECONFORM_VERSION | head -1)"
            echo "github-release|kubernetes-sigs/metrics-server|$(extract_workflow_env_pin METRICS_SERVER_VERSION | head -1)"
            echo "github-release|projectcalico/calico|$(extract_workflow_env_pin CALICO_VERSION | head -1)"
            echo "github-release|kubernetes-sigs/kind|$(extract_kind_pins .github/workflows/integration-tests.yml | awk -F'|' '$1=="kind"{print $2}' | head -1)"
            kind_node="$(extract_kind_pins .github/workflows/integration-tests.yml | awk -F'|' '$1=="kind-node"{print $2}' | head -1)"
            [ -n "$kind_node" ] && echo "image|docker.io/${kind_node%%:*}|${kind_node##*:}"
            extract_precommit_hooks .pre-commit-config.yaml | while IFS='|' read -r repo rev; do
                repo="${repo%.git}"; repo="${repo%/}"
                echo "github-tags|${repo#https://github.com/}|$rev"
            done
            echo "endoflife|python|$(extract_constant_value LAMBDA_PYTHON_RUNTIME | sed -E 's/^PYTHON_([0-9]+)_([0-9]+)$/\1.\2/')"
            ruby_pin="$(read_ruby_version_pin .ruby-version)"
            [ -n "$ruby_pin" ] && echo "endoflife|ruby|$(printf '%s' "$ruby_pin" | cut -d. -f1-2)"
            # The image sweep the scan performs, source for source.
            {
                grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" .github/workflows 2>/dev/null | sed 's/image: //'
                grep -rhoE "[a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" .github/workflows 2>/dev/null \
                    | grep -E '^(alpine|hadolint|koalaman|semgrep|bridgecrew|checkmarx|trufflesecurity|zricethezav|aquasec|bats|python):' | sed 's/[[:space:]]*$//'
                grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+" lambda/kubectl-applier-simple/manifests/ examples/ scripts/live_release_validation/manifests/ 2>/dev/null \
                    | grep -v '{{' | sed 's/image: //'
                extract_chart_value_images lambda/helm-installer/charts.yaml
                extract_mooncake_default_image cli/images.py
                extract_python_string_constant AWS_CLI_IMAGE gco/services/inference_monitor.py | sed 's/@sha256:.*//'
                grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}" scripts/live_release_validation/manifests/ 2>/dev/null \
                    | sed 's/^image: //; s/@sha256:.*//'
            } | sort -u | while IFS= read -r img; do
                [ -n "$img" ] || continue
                parsed="$(parse_image_registry "${img%%:*}")"
                echo "image|${parsed%%|*}/${parsed#*|}|${img#*:}"
            done
            {
                extract_python_string_constant AWS_CLI_IMAGE gco/services/inference_monitor.py
                grep -rhoE "image: [a-zA-Z0-9_./-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}" scripts/live_release_validation/manifests/ 2>/dev/null | sed 's/^image: //'
            } | sort -u | while IFS= read -r ref; do
                case "$ref" in *@sha256:*) echo "digest|${ref%@sha256:*}|${ref##*@sha256:}" ;; esac
            done
            extract_helm_charts lambda/helm-installer/charts.yaml | while IFS= read -r entry; do
                [ -n "$entry" ] || continue
                if [ "$(jq -r '.use_oci' <<<"$entry")" = "true" ]; then
                    echo "helm-oci|$(jq -r '.repo_url + "/" + .chart' <<<"$entry")|$(jq -r '.version' <<<"$entry")"
                else
                    echo "helm|$(jq -r '.name + "/" + .chart' <<<"$entry")|$(jq -r '.version' <<<"$entry")"
                fi
            done
            extract_eks_addons gco/stacks/regional_stack.py | sed 's/^/eks-addon|/'
            echo "k8s|current|$(extract_k8s_version cdk.json)"
            extract_aurora_versions gco/stacks/regional_stack.py | sed 's/^/aurora|current|/'
            extract_emr_versions gco/stacks/constants.py | sed 's/^/emr|current|/'
            for leaf in mission_default_model_id capacity_advisor_default_model_id claude_code_default_model_id codex_default_model_id; do
                echo "bedrock-profile|${leaf}|$(extract_default_bedrock_model cdk.json "$leaf")"
            done
            echo "bedrock-embedding|bedrock|$(extract_default_bedrock_model cdk.json embedding_model_id)"
            echo "bedrock-embedding|vector_store|$(extract_default_bedrock_model cdk.json embedding_model_id vector_store)"
            echo "cdk-enum|LAMBDA_PYTHON_RUNTIME|$(extract_constant_value LAMBDA_PYTHON_RUNTIME)"
            echo "cdk-enum|LAMBDA_NODEJS_RUNTIME|$(extract_constant_value LAMBDA_NODEJS_RUNTIME)"
        } | grep -v '|$' > "$FAKE_CATALOG"
    )
}

# make_consistent_checkout <dir> — a small checkout in which every pin the
# scan reads exists and agrees with its copies, so a scan against it with
# every upstream answering "current" finds nothing at all.
make_consistent_checkout() {
    local root="$1" today
    today="$(date -u +%Y-%m-%d)"
    mkdir -p "$root"/{.github/workflows,.github/actions/install-trivy,.github/config,gco/stacks,gco/services,cli,lambda/helm-installer,lambda/kubectl-applier-simple/manifests,examples,scripts/live_release_validation/manifests,dockerfiles}
    cat > "$root/pyproject.toml" <<'TOML'
[build-system]
requires = ["setuptools==84.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "gco-cli"
version = "0.0.0"
dependencies = ["boto3==1.40.0"]

[project.optional-dependencies]
dev = ["ruff==0.16.5"]
TOML
    printf 'boto3==1.40.0\nruff==0.16.5\n' > "$root/requirements-lock.txt"
    printf 'boto3==1.40.0\n' > "$root/lambda/helm-installer/requirements.txt"
    cat > "$root/package.json" <<'JSON'
{"name": "tooling", "packageManager": "npm@12.0.2", "engines": {"node": ">=24"},
 "devDependencies": {"aws-cdk": "2.1140.0"}}
JSON
    printf '{}\n' > "$root/package-lock.json"
    cat > "$root/.github/dependabot.yml" <<'YAML'
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: monthly
YAML
    cat > "$root/.github/workflows/lint.yml" <<'YAML'
name: Lint
env:
  ACTIONLINT_VERSION: "1.7.12"
jobs:
  ruff:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000
        with:
          python-version: "3.14"
      - uses: astral-sh/ruff-action@0000000000000000000000000000000000000000
        with:
          version: "0.16.5"
YAML
    cat > "$root/.github/workflows/integration-tests.yml" <<'YAML'
name: Integration Tests
env:
  KUBECONFORM_VERSION: "0.7.0"
  METRICS_SERVER_VERSION: "v0.8.0"
  METRICS_SERVER_SHA256: "aaaa"
  CALICO_VERSION: "v3.30.0"
  CALICO_SHA256: "bbbb"
jobs:
  e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000
        with:
          python-version: "3.14"
      - uses: helm/kind-action@0000000000000000000000000000000000000000
        with:
          version: "v0.33.0"
          node_image: "kindest/node:v1.36.4"
      - run: docker run --rm alpine:3.24.1 true
YAML
    cat > "$root/.github/actions/install-trivy/action.yml" <<'YAML'
name: Install Trivy
inputs:
  version:
    default: "v0.74.0"
YAML
    printf 'CVE-2026-0001 exp:2999-01-01 justification\n' > "$root/.github/config/.trivyignore"
    printf '# none\n' > "$root/.github/config/.pip-audit-ignore"
    printf 'GHSA-0000-0000-0000 exp:2999-06-30 justification\n' > "$root/.github/config/.npm-audit-ignore"
    cat > "$root/cdk.json" <<'JSON'
{"context": {"kubernetes_version": "1.36",
 "bedrock": {"mission_default_model_id": "global.anthropic.claude-opus-5",
             "capacity_advisor_default_model_id": "global.anthropic.claude-opus-5",
             "claude_code_default_model_id": "global.anthropic.claude-opus-5",
             "codex_default_model_id": "global.openai.gpt-5.6-sol",
             "embedding_model_id": "amazon.titan-embed-text-v2:0"},
 "vector_store": {"embedding_model_id": "amazon.titan-embed-text-v2:0"}}}
JSON
    cat > "$root/gco/stacks/constants.py" <<'PY'
EKS_ADDON_POD_IDENTITY_AGENT = "v1.4.0-eksbuild.1"
EKS_ADDON_METRICS_SERVER = "v0.9.0-eksbuild.7"
EKS_ADDON_EFS_CSI_DRIVER = "v3.4.2-eksbuild.1"
EKS_ADDON_CLOUDWATCH_OBSERVABILITY = "v6.6.0-eksbuild.1"
EKS_ADDON_FSX_CSI_DRIVER = "v1.10.0-eksbuild.1"
AURORA_POSTGRES_VERSION = "17.10"
EMR_SERVERLESS_RELEASE_LABEL = "emr-7.14.0"
LAMBDA_PYTHON_RUNTIME = "PYTHON_3_14"
LAMBDA_NODEJS_RUNTIME = "NODEJS_24_X"
PY
    printf '# the stack reads its pins from constants.py\n' > "$root/gco/stacks/regional_stack.py"
    printf 'AWS_CLI_IMAGE = "public.ecr.aws/aws-cli/aws-cli:2.36.41@sha256:%s"\n' "$DIGEST_A" > "$root/gco/services/inference_monitor.py"
    printf '_DISAGGREGATED_DEFAULT_IMAGE = "vllm/vllm-openai:v0.28.0"\n' > "$root/cli/images.py"
    cat > "$root/cli/autopilot.py" <<'PY'
CLAUDE_CODE_VERSION = "2.1.252"
CODEX_VERSION = "0.152.0"
COMPANION_MCP_SERVERS = (
    CompanionServer(name="aws-docs", registry="pypi", package="awslabs.aws-documentation-mcp-server", command="uvx"),
    CompanionServer(name="memory", registry="npm", package="@modelcontextprotocol/server-memory", command="npx"),
)
PY
    cat > "$root/Dockerfile.dev" <<EOF
FROM python:3.14.7-slim
ARG APT_SECURITY_EPOCH=${today}
ARG NODE_VERSION=v24.21.0
ARG NPM_VERSION=12.0.2
ARG CDK_VERSION=2.1140.0
ARG KUBECTL_VERSION=v1.36.4
ARG AWSCLI_VERSION=2.36.41
ARG DOCKER_VERSION=29.8.0
ARG BUILDX_VERSION=v0.37.0
ARG UV_VERSION=0.12.11
EOF
    cat > "$root/lambda/helm-installer/Dockerfile" <<EOF
FROM public.ecr.aws/lambda/python:3.14
ARG DNF_SECURITY_EPOCH=${today}
RUN curl -fsSL https://get.helm.sh/helm-v4.2.4-linux-amd64.tar.gz -o /tmp/helm.tar.gz \\
    && echo "${DIGEST_A}  /tmp/helm.tar.gz" | sha256sum -c - \\
    && curl -fsSL https://dl.k8s.io/release/v1.36.4/bin/linux/amd64/kubectl -o /tmp/kubectl \\
    && echo "${DIGEST_B}  /tmp/kubectl" | sha256sum -c -
EOF
    cat > "$root/lambda/helm-installer/charts.yaml" <<'YAML'
charts:
  keda:
    repo_url: https://kedacore.github.io/charts
    chart: keda
    version: 2.20.2
    values:
      image:
        registry: ghcr.io
        repository: kedacore/keda
        tag: 2.20.2
  kueue:
    repo_url: oci://registry.k8s.io/kueue/charts
    chart: kueue
    version: 0.19.2
    use_oci: true
YAML
    printf 'kind: Pod\nspec:\n  containers:\n    - image: busybox:1.38.0\n    - image: gco/api:1.0.0\n' > "$root/lambda/kubectl-applier-simple/manifests/10-probe.yaml"
    printf 'kind: Job\nspec:\n  containers:\n    - image: rayproject/ray:2.58.0\n    - image: busybox:latest\n' > "$root/examples/ray-job.yaml"
    printf 'kind: Pod\nspec:\n  containers:\n    - image: public.ecr.aws/docker/library/busybox:1.38.0@sha256:%s\n' "$DIGEST_B" > "$root/scripts/live_release_validation/manifests/smoke.yaml"
    cat > "$root/.pre-commit-config.yaml" <<'YAML'
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.5
    hooks:
      - id: ruff
  - repo: https://github.com/DavidAnson/markdownlint-cli2
    rev: v0.23.2
    hooks:
      - id: markdownlint-cli2
YAML
    printf '3.14\n' > "$root/.python-version"
    printf '4.0.1\n' > "$root/.ruby-version"
    printf 'v24.21.0\n' > "$root/.nvmrc"
    printf 'FROM python:3.14.7-slim\nARG APT_SECURITY_EPOCH=%s\n' "$today" > "$root/dockerfiles/api-dockerfile"
}

# run_scan <root> [VAR=value ...] — the driver from <root>, faked PATH, the
# two GitHub files captured under $BATS_TEST_TMPDIR.
run_scan() {
    local root="$1"
    shift
    GITHUB_OUTPUT="$BATS_TEST_TMPDIR/github-output"
    GITHUB_STEP_SUMMARY="$BATS_TEST_TMPDIR/github-step-summary"
    : > "$GITHUB_OUTPUT"
    : > "$GITHUB_STEP_SUMMARY"
    run env PATH="$FAKE_BIN:$PATH" TMPDIR="$BATS_TEST_TMPDIR" \
        GITHUB_OUTPUT="$GITHUB_OUTPUT" GITHUB_STEP_SUMMARY="$GITHUB_STEP_SUMMARY" \
        FAKE_AWS_CDK=present "$@" \
        bash -c 'cd "$1" && exec bash "$2"' _ "$root" "$SCRIPT"
}

report_path() {
    sed -n 's/^report_path=//p' "$GITHUB_OUTPUT"
}

@test "dependency-scan.sh passes bash -n and shellcheck" {
    bash -n "$SCRIPT"
    command -v shellcheck &>/dev/null || skip "shellcheck not installed"
    shellcheck -x "$SCRIPT"
}

@test "a checkout whose pins are all current and consistent scans clean and complete" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root"

    [ "$status" -eq 0 ]
    [[ "$output" == *"All Python dependencies are up to date."* ]]
    [[ "$output" == *"All npm direct dependencies are up to date."* ]]
    [[ "$output" == *"every runner label in use is the newest generally-available image."* ]]
    [[ "$output" == *"ubuntu-latest pins ubuntu-24.04; ubuntu-26.04 exists but is still in preview."* ]]
    [[ "$output" == *".ruby-version pins 4.0.1; newest supported series is 4.0."* ]]
    [[ "$output" == *"Offline NodePool/watch-list policy is current."* ]]
    [[ "$output" == *"Live EC2 accelerator catalog is current."* ]]
    [[ "$output" == *"Incomplete lookups:       0"* ]]
    [[ "$output" == *"All dependencies are up to date."* ]]
    [[ "$output" != *"INCOMPLETE:"* ]]
    grep -qx 'has_drift=false' "$GITHUB_OUTPUT"
    grep -qx 'scan_complete=true' "$GITHUB_OUTPUT"
    ! grep -q 'report_path=' "$GITHUB_OUTPUT"
    grep -q '^All dependencies are up to date.$' "$GITHUB_STEP_SUMMARY"
    ! grep -q 'Incomplete or skipped' "$GITHUB_STEP_SUMMARY"
    # Every surface was consulted through the faked tools.
    grep -q '^pip install -e .\[dev\]' "$CALLS"
    grep -q '^curl https://registry.npmjs.org/aws-cdk/latest' "$CALLS"
    grep -q '^curl https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest' "$CALLS"
    grep -q '^curl https://pypi.org/pypi/setuptools/json' "$CALLS"
    grep -q '^curl https://dl.k8s.io/release/stable-1.36.txt' "$CALLS"
    grep -q '^skopeo list-tags --retry-times 3 docker://docker.io/library/busybox' "$CALLS"
    grep -q '^skopeo inspect --raw docker://public.ecr.aws/aws-cli/aws-cli:2.36.41' "$CALLS"
    grep -q '^helm repo add keda https://kedacore.github.io/charts --force-update' "$CALLS"
    grep -q '^helm show chart oci://registry.k8s.io/kueue/charts/kueue' "$CALLS"
    grep -q '^aws eks describe-addon-versions --addon-name metrics-server --kubernetes-version 1.36' "$CALLS"
    grep -q '^aws bedrock list-foundation-models --by-output-modality EMBEDDING' "$CALLS"
}

@test "one release newer on every surface produces a complete drift report with every section" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_DRIFT=1 FAKE_RUNNER_IMAGES=drift FAKE_ACCELERATOR=drift

    [ "$status" -eq 0 ]
    [[ "$output" == *"Found 3 outdated Python package(s)"* ]]
    [[ "$output" == *"  - boto3: 1.40.0 -> 2.40.0"* ]]
    [[ "$output" == *"  - setuptools: 84.0.0 -> 85.0.0"* ]]
    [[ "$output" == *"  - AWSCLI_VERSION: 2.36.41 -> 2.36.42"* ]]
    [[ "$output" == *"  - .: aws-cdk 2.1140.0 -> 3.1140.0"* ]]
    [[ "$output" == *"  - busybox:1.38.0 -> 2.38.0"* ]]
    [[ "$output" == *"  - public.ecr.aws/aws-cli/aws-cli:2.36.41: committed digest does not match the tag"* ]]
    [[ "$output" == *"  - keda (keda): 2.20.2 -> 3.20.2"* ]]
    [[ "$output" == *"  - kueue (kueue): 0.19.2 -> 1.19.2"* ]]
    [[ "$output" == *"  - metrics-server: v0.9.0-eksbuild.7 -> v1.9.0-eksbuild.7"* ]]
    [[ "$output" == *"  - kubernetes_version: 1.36 -> 2.36 (std support ends 2027-01-15)"* ]]
    [[ "$output" == *"  - aurora-postgresql: 17.10 -> 18.10"* ]]
    [[ "$output" == *"  - emr-serverless: emr-7.14.0 -> emr-7.14.1"* ]]
    [[ "$output" == *"  - bedrock mission_default_model_id: global.anthropic.claude-opus-5 -> global.anthropic.claude-opus-6"* ]]
    [[ "$output" == *"  - vector_store embedding_model_id: amazon.titan-embed-text-v2:0 -> amazon.titan-embed-text-v3:0"* ]]
    [[ "$output" == *"  - NODE_VERSION: v24.21.0 -> v25.21.0"* ]]
    [[ "$output" == *"  - UV_VERSION: 0.12.11 -> 1.12.11"* ]]
    [[ "$output" == *"  - CLAUDE_CODE_VERSION: 2.1.252 -> 3.1.252"* ]]
    [[ "$output" == *"  - https://github.com/astral-sh/ruff-pre-commit: v0.16.5 -> v1.16.5"* ]]
    [[ "$output" == *"  - LAMBDA_PYTHON_RUNTIME: PYTHON_3_14 -> PYTHON_4_14"* ]]
    [[ "$output" == *"  - LAMBDA_NODEJS_RUNTIME: NODEJS_24_X -> NODEJS_25_X"* ]]
    [[ "$output" == *"  - python (LAMBDA_PYTHON_RUNTIME): 3.14 -> 4.14"* ]]
    [[ "$output" == *"  - ruby (.ruby-version): 4.0.1 -> 5.0"* ]]
    [[ "$output" == *"  - runner ubuntu-latest: ubuntu-22.04 -> ubuntu-24.04"* ]]
    [[ "$output" == *"  - Trivy (install-trivy action default): v0.74.0 -> v1.74.0"* ]]
    [[ "$output" == *"  - kubectl (helm-installer Dockerfile): v1.36.4 -> v2.36.4"* ]]
    [[ "$output" == *"  - kind node image (kindest/node): v1.36.4 -> v1.36.5"* ]]
    [[ "$output" == *"Found 2 offline accelerator policy finding(s)."* ]]
    [[ "$output" == *"Found 2 live EC2 catalog drift finding(s)."* ]]
    [[ "$output" == *"Incomplete lookups:       0"* ]]
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    grep -qx 'scan_complete=true' "$GITHUB_OUTPUT"
    local report
    report="$(report_path)"
    [ -f "$report" ]
    local section
    for section in "## Summary" "## Python Packages" "## npm Packages" "## Docker Images" "## Helm Charts" \
            "## EKS Add-ons" "## EKS Kubernetes Version" "## Aurora PostgreSQL Engine" "## EMR Serverless" \
            "## Bedrock Default Model" "## Accelerator Catalog and NodePools" "## Dockerfile.dev Pins" \
            "## GCO Autopilot Pins" "## Pre-commit Hooks" "## CDK Enum Constants" "## Python Release" \
            "## Runner Images" "## Ruby Release" "## CI Tooling" "## Action Required"; do
        grep -qF -- "$section" "$report" || { echo "missing section: $section"; false; }
    done
    grep -qF -- "| [Python Packages](#python-packages) | 3 update(s) | routine |" "$report"
    grep -qF -- "| \`.\` | aws-cdk | 2.1140.0 | 3.1140.0 | [npm](https://www.npmjs.com/package/aws-cdk) |" "$report"
    grep -qF -- "#### Retire p2 from the GPU pool" "$report"
    ! grep -q "Skipped checks" "$report"
    [[ "$output" == *"Wrote report to ${report}"* ]]
    # The report is mirrored into the job summary.
    grep -q '^# Dependency Update Report' "$GITHUB_STEP_SUMMARY"
}

@test "without AWS credentials every credential-dependent section is skipped and the scan is incomplete" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_AWS=none

    [ "$status" -eq 0 ]
    [[ "$output" == *"No AWS credentials available (scan needs eks:DescribeAddonVersions)"* ]]
    [[ "$output" == *"No AWS credentials available (scan needs eks:DescribeClusterVersions)"* ]]
    [[ "$output" == *"No AWS credentials available (scan needs rds:DescribeDBEngineVersions)"* ]]
    [[ "$output" == *"No AWS credentials available (scan needs elasticmapreduce:ListReleaseLabels)"* ]]
    [[ "$output" == *"No AWS credentials available (scan needs bedrock:ListInferenceProfiles)"* ]]
    [[ "$output" == *"No AWS credentials available for the online EC2 catalog check"* ]]
    [[ "$output" == *"EKS add-ons outdated:     (skipped)"* ]]
    [[ "$output" == *"Accelerator catalog:      0 (online skipped)"* ]]
    [[ "$output" == *"No drift was found in completed checks, but the scan is incomplete."* ]]
    grep -qx 'has_drift=false' "$GITHUB_OUTPUT"
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
    grep -q '_Incomplete or skipped checks: EKS add-ons skipped: No AWS credentials' "$GITHUB_STEP_SUMMARY"
    ! grep -q '^aws eks' "$CALLS"
}

@test "with drift and skipped sections the report carries the incomplete warning and the collapsed skip list" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_AWS=none FAKE_DRIFT=1 FAKE_RUNNER_IMAGES=fail FAKE_AWS_CDK=absent

    [ "$status" -eq 0 ]
    [[ "$output" == *"aws-cdk-lib not importable. Install with 'pip install aws-cdk-lib' to enable."* ]]
    [[ "$output" == *"Could not read the actions/runner-images catalog"* ]]
    [[ "$output" == *"CDK enum constants:       (skipped)"* ]]
    [[ "$output" == *"Runner images:            (skipped)"* ]]
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
    local report
    report="$(report_path)"
    grep -qF -- "> **Incomplete scan.** Zero-count surfaces are provisional, not confirmed current." "$report"
    grep -qF -- "> One or more credential-dependent or optional checks were skipped; see the workflow log." "$report"
    grep -qF -- "| EKS Add-ons | skipped | — |" "$report"
    grep -qF -- "<summary>Skipped checks</summary>" "$report"
    grep -qF -- "- **EKS Add-ons:** No AWS credentials available" "$report"
    grep -qF -- "- **CDK Enum Constants:** aws-cdk-lib not importable" "$report"
    grep -qF -- "- **Runner Images:** Could not read the actions/runner-images catalog" "$report"
    # A zero-count surface in an incomplete scan is provisional, not "up to date".
    grep -qF -- "| Base-image Security Epochs | no drift found (incomplete scan) | — |" "$report"
}

@test "an unreachable network marks every lookup incomplete instead of inventing drift" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_NETWORK=down FAKE_PIP=list-fails FAKE_AWS_ADDON=invalid FAKE_AWS_K8S=fail \
        FAKE_AWS_AURORA=invalid FAKE_AWS_EMR=empty FAKE_AWS_BEDROCK=empty FAKE_HELM=oci-fails FAKE_ACCELERATOR=error

    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: pip list --outdated failed."* ]]
    [[ "$output" == *"INCOMPLETE: PyPI lookup failed or returned an invalid version for build dependency setuptools."* ]]
    [[ "$output" == *"INCOMPLETE: npm registry lookup failed or returned an invalid version for aws-cdk."* ]]
    [[ "$output" == *"INCOMPLETE: Container registry tag lookup failed for docker.io/library/busybox."* ]]
    [[ "$output" == *"INCOMPLETE: Container manifest lookup failed for public.ecr.aws/aws-cli/aws-cli:2.36.41."* ]]
    [[ "$output" == *"INCOMPLETE: Helm repository refresh failed for keda (https://kedacore.github.io/charts)."* ]]
    [[ "$output" == *"INCOMPLETE: Helm OCI lookup failed for oci://registry.k8s.io/kueue/charts/kueue."* ]]
    [[ "$output" == *"EKS add-on lookup failed or returned an invalid version for eks-pod-identity-agent."* ]]
    [[ "$output" == *"EKS Kubernetes version lookup failed: An error occurred (AccessDeniedException)"* ]]
    [[ "$output" == *"Aurora PostgreSQL engine lookup failed or returned an invalid version for major 17."* ]]
    [[ "$output" == *"EMR release-label lookup failed or returned an empty response."* ]]
    [[ "$output" == *"Bedrock model lookup failed or returned no active release in the model family of context.bedrock.mission_default_model_id."* ]]
    [[ "$output" == *"INCOMPLETE: Node.js release-schedule lookup failed or returned no active LTS major."* ]]
    [[ "$output" == *"INCOMPLETE: Upstream version lookup failed for Dockerfile.dev pin CDK_VERSION."* ]]
    [[ "$output" == *"npm lookup for @anthropic-ai/claude-code failed (network)."* ]]
    [[ "$output" == *"INCOMPLETE: Pre-commit tag lookup failed for https://github.com/astral-sh/ruff-pre-commit."* ]]
    [[ "$output" == *"endoflife.date query failed (network or schema change)."* ]]
    [[ "$output" == *"INCOMPLETE: GitHub release lookup failed for Trivy (install-trivy action default) (aquasecurity/trivy)."* ]]
    [[ "$output" == *"INCOMPLETE: kubectl stable-version lookup failed for minor 1.36."* ]]
    [[ "$output" == *"INCOMPLETE: Container registry lookup failed for kindest/node minor 1.36."* ]]
    [[ "$output" == *"Offline accelerator validator failed operationally."* ]]
    [[ "$output" == *"Online accelerator scanner failed operationally."* ]]
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
    # The accelerator failures are findings (the report carries them), so drift is reported.
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    local report
    report="$(report_path)"
    grep -qF -- "**Status: OPERATIONAL ERROR.**" "$report"
    grep -qF -- "The deterministic validator exited with status 2." "$report"
    grep -qF -- "The online scanner exited with status 2." "$report"
    grep -qF -- "    boto3 is not installed" "$report"
    grep -qF -- "> Recorded failures: " "$report"
    grep -qF -- "- **Incomplete lookup or parse:** pip list --outdated failed." "$report"
}

@test "the remaining lookup and parse failure branches each degrade as designed" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    # pip cannot install, its listing is not JSON, the k8s API answers with
    # nothing parseable, EMR lists only previews, the online accelerator
    # summary disagrees with its exit status.
    run_scan "$root" FAKE_PIP=install-fails FAKE_AWS_K8S=empty FAKE_AWS_EMR=unparseable FAKE_ACCELERATOR=inconsistent
    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Python dependency installation failed."* ]]
    [[ "$output" == *"EKS Kubernetes version response contained no parseable standard-support versions."* ]]
    [[ "$output" == *"EMR release-label response contained no parseable stable releases."* ]]
    [[ "$output" == *"Online accelerator scan returned an inconsistent result."* ]]
    grep -qF -- "The command exit status disagreed with its JSON drift summary." "$(report_path)"

    build_catalog "$root"
    run_scan "$root" FAKE_PIP=malformed FAKE_ACCELERATOR=malformed
    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: pip list --outdated returned malformed JSON."* ]]
    [[ "$output" == *"Online accelerator scan summary could not be parsed."* ]]

    run_scan "$root" FAKE_ACCELERATOR=drift-unparseable
    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Offline accelerator catalog validation: The validator reported drift but emitted no parseable actionable findings."* ]]
    grep -qF -- "The validator reported drift but emitted no parseable actionable findings." "$(report_path)"

    # A new EMR major with no newer patch in the pinned line.
    run_scan "$root" FAKE_DRIFT=1 FAKE_EMR_DRIFT=major
    [ "$status" -eq 0 ]
    [[ "$output" == *"  - emr-serverless: emr-7.14.0 -> emr-8.14.0 (new major available)"* ]]

    # Unhealthy companions are drift; missing ones too.
    run_scan "$root" FAKE_COMPANIONS=unhealthy
    [ "$status" -eq 0 ]
    [[ "$output" == *"  - companion memory: deprecated: use the maintained fork instead"* ]]
    [[ "$output" == *"  - companion aws-docs: yanked: 1.0.0"* ]]
    grep -qF -- "| companion memory (npm: @modelcontextprotocol/server-memory) | \`launch-time (unpinned)\` | deprecated: use the maintained fork instead | [registry](https://www.npmjs.com/package/@modelcontextprotocol/server-memory) |" "$(report_path)"
    run_scan "$root" FAKE_COMPANIONS=missing
    [ "$status" -eq 0 ]
    [[ "$output" == *"  - companion memory: missing"* ]]
    [[ "$output" == *"  - companion aws-docs: missing"* ]]
}

@test "pre-commit revisions that are not semver are handled: full SHAs pass, mutable refs and other hosts are incomplete" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    cat >> "$root/.pre-commit-config.yaml" <<'YAML'
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: 0123456789abcdef0123456789abcdef01234567
    hooks:
      - id: mypy
  - repo: https://github.com/adrienverge/yamllint
    rev: main
    hooks:
      - id: yamllint
  - repo: https://gitlab.com/someone/hook
    rev: v1.0.0
    hooks:
      - id: hook
  - repo: local
    hooks:
      - id: local-check
YAML
    build_catalog "$root"
    run_scan "$root"

    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Unsupported mutable or non-semver pre-commit rev 'main' for https://github.com/adrienverge/yamllint."* ]]
    [[ "$output" == *"INCOMPLETE: Pre-commit tag lookup failed for https://gitlab.com/someone/hook."* ]]
    [[ "$output" != *"mirrors-mypy"* ]]
    ! grep -q 'mirrors-mypy' "$CALLS"
}

@test "version-consistency findings name every copy that disagrees or is missing" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    # ruff: pre-commit ahead of pyproject; npm: package.json behind Dockerfile.dev;
    # CDK CLI: only Dockerfile.dev; Node: .nvmrc removed and Dockerfile.dev on 22;
    # a literal HELM_VERSION reintroduced in a workflow, disagreeing with the
    # installer; KUBECTL_VERSION disagreeing between Dockerfile.dev and installer;
    # CALICO_VERSION pinned twice; a non-exact build-system pin; a Lambda
    # requirements copy behind pyproject; the same digest tag pinned twice.
    sed -i.bak 's/rev: v0.16.5/rev: v0.17.0/' "$root/.pre-commit-config.yaml"
    sed -i.bak 's/"packageManager": "npm@12.0.2"/"packageManager": "npm@11.0.0"/; s/"aws-cdk": "2.1140.0"/"aws-cdk": "^2"/' "$root/package.json"
    rm -f "$root/.nvmrc"
    sed -i.bak 's/^ARG NODE_VERSION=v24.21.0/ARG NODE_VERSION=v22.0.0/; s/^ARG KUBECTL_VERSION=v1.36.4/ARG KUBECTL_VERSION=v1.36.9/' "$root/Dockerfile.dev"
    cat >> "$root/.github/workflows/lint.yml" <<'YAML'
  stray:
    runs-on: ubuntu-latest
    env:
      HELM_VERSION: "v4.0.0"
      CALICO_VERSION: "v3.29.0"
    steps:
      - run: echo
YAML
    sed -i.bak 's/requires = \["setuptools==84.0.0"\]/requires = ["setuptools>=84"]/' "$root/pyproject.toml"
    printf 'boto3==1.39.0\n' > "$root/lambda/helm-installer/requirements.txt"
    printf 'kind: Pod\nspec:\n  containers:\n    - image: public.ecr.aws/docker/library/busybox:1.38.0@sha256:%s\n' "$DIGEST_A" > "$root/scripts/live_release_validation/manifests/second.yaml"
    rm -f "$root"/*.bak "$root"/.pre-commit-config.yaml.bak
    build_catalog "$root"
    run_scan "$root"

    [ "$status" -eq 0 ]
    [[ "$output" == *"  - ruff pins disagree: pyproject=0.16.5 precommit=0.17.0 lint-action=0.16.5"* ]]
    [[ "$output" == *"  - Node.js major pins disagree or are missing: Dockerfile.dev=22 gco/stacks/constants.py=24 package.json=24; missing=.nvmrc"* ]]
    [[ "$output" == *"  - npm pins disagree or are missing: Dockerfile.dev=12.0.2 package.json=11.0.0"* ]]
    [[ "$output" == *"  - AWS CDK CLI pins disagree or are missing: Dockerfile.dev=2.1140.0; missing=package.json"* ]]
    [[ "$output" == *"  - npm dependency management: package.json: devDependencies.aws-cdk must use an exact version pin"* ]]
    [[ "$output" == *"  - CALICO_VERSION disagrees across workflows: v3.29.0,v3.30.0"* ]]
    [[ "$output" == *"  - HELM_VERSION is declared literally in a workflow again (should derive from the installer Dockerfile): v4.0.0"* ]]
    [[ "$output" == *"  - HELM_VERSION disagrees between helm-installer Dockerfile, workflows, and Dockerfile.dev: v4.0.0,v4.2.4"* ]]
    [[ "$output" == *"  - KUBECTL_VERSION disagrees between helm-installer Dockerfile, workflows, and Dockerfile.dev: v1.36.4,v1.36.9"* ]]
    [[ "$output" == *"  - build-system requires entry is not an exact ==X.Y.Z pin: setuptools>=84"* ]]
    [[ "$output" == *"  - Lambda runtime pin: lambda/helm-installer/requirements.txt:"*"boto3"* ]]
    [[ "$output" == *"  - image digest: public.ecr.aws/docker/library/busybox:"* ]]
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    grep -qF -- "## Version Consistency" "$(report_path)"
}

@test "stale security epochs, expiring suppressions and a stale lockfile are reported with their numbers" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    sed -i.bak 's/^ARG APT_SECURITY_EPOCH=.*/ARG APT_SECURITY_EPOCH=2020-01-01/' "$root/Dockerfile.dev"
    printf 'CVE-2026-0002 exp:%s justification\n' "$(date -u +%Y-%m-%d)" > "$root/.github/config/.trivyignore"
    printf 'boto3==1.39.0\nruff==0.16.5\n' > "$root/requirements-lock.txt"
    rm -f "$root"/*.bak
    build_catalog "$root"
    run_scan "$root" SECURITY_EPOCH_STALE_DAYS=45 SUPPRESSION_EXPIRY_WARN_DAYS=30

    [ "$status" -eq 0 ]
    [[ "$output" == *"  - Dockerfile.dev (APT_SECURITY_EPOCH): 2020-01-01 ("*" days old)"* ]]
    [[ "$output" == *"  - .trivyignore: CVE-2026-0002 expires "*" (0 days)"* ]]
    [[ "$output" == *"  - direct dep version mismatch: boto3==1.40.0 (lock has 1.39.0)"* ]]
    local report
    report="$(report_path)"
    grep -qF -- "## Base-image Security Epochs" "$report"
    grep -qF -- "## Suppression Expiries" "$report"
    grep -qF -- "## Lockfile Freshness" "$report"
    grep -qF -- "| \`boto3\` | \`1.40.0\` | \`1.39.0\` |" "$report"

    # A direct dependency absent from the lock, and an unparseable lock.
    printf 'ruff==0.16.5\n' > "$root/requirements-lock.txt"
    run_scan "$root"
    [[ "$output" == *"  - direct dep missing from requirements-lock.txt: boto3==1.40.0"* ]]
    rm -f "$root/requirements-lock.txt"
    run_scan "$root"
    [[ "$output" == *"INCOMPLETE: Lockfile freshness validation failed; inspect its error above."* ]]
}

@test "an empty directory trips every missing-file branch in one pass" {
    local root="$BATS_TEST_TMPDIR/empty"
    mkdir -p "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_PIP=install-fails

    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Could not enumerate optional dependency groups from pyproject.toml."* ]]
    [[ "$output" == *"INCOMPLETE: Python dependency installation failed and optional groups could not be enumerated."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse direct Python dependencies from pyproject.toml."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse [build-system] requirements from pyproject.toml."* ]]
    [[ "$output" == *"INCOMPLETE: Could not enumerate repository-owned npm package manifests."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse Helm chart value images."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the Mooncake default image from cli/images.py."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse an immutable AWS_CLI_IMAGE from gco/services/inference_monitor.py."* ]]
    [[ "$output" == *"INCOMPLETE: No digest-pinned smoke images found under scripts/live_release_validation/manifests/."* ]]
    [[ "$output" == *"INCOMPLETE: lambda/helm-installer/charts.yaml is missing."* ]]
    [[ "$output" == *"Could not read EKS add-on pins from gco/stacks/regional_stack.py."* ]]
    [[ "$output" == *"Could not read AURORA_POSTGRES_VERSION from gco/stacks/constants.py."* ]]
    [[ "$output" == *"Could not read the EMR Serverless release-label pin from gco/stacks/constants.py."* ]]
    [[ "$output" == *"Could not read context.bedrock.mission_default_model_id from cdk.json."* ]]
    [[ "$output" == *"INCOMPLETE: Dockerfile.dev is missing."* ]]
    [[ "$output" == *"cli/autopilot.py not found."* ]]
    [[ "$output" == *"INCOMPLETE: .pre-commit-config.yaml is missing."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the current or latest Lambda Python runtime enum."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the current or latest Lambda Node.js runtime enum."* ]]
    [[ "$output" == *"Could not parse LAMBDA_PYTHON_RUNTIME for the Python release comparison."* ]]
    [[ "$output" == *"Could not parse .ruby-version for the Ruby release comparison."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the committed version pin for Trivy (install-trivy action default)."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the kubectl pin from lambda/helm-installer/Dockerfile."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse the kind node-image pin from integration-tests.yml."* ]]
    [[ "$output" == *"  - pyproject.toml [build-system] requires is missing or unparseable"* ]]
    [[ "$output" == *"  - Lambda runtime pin: pyproject.toml: missing or unparseable, cannot verify Lambda pins"* ]]
    [[ "$output" == *"INCOMPLETE: Lockfile freshness validation failed; inspect its error above."* ]]
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
}

@test "a chart entry the parser cannot complete and an addon lookup that fails midway stop their sections" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    cat >> "$root/lambda/helm-installer/charts.yaml" <<'YAML'
  broken:
    repo_url: https://example.invalid/charts
YAML
    build_catalog "$root"
    run_scan "$root" FAKE_AWS_ADDON=invalid
    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Helm chart parser emitted an incomplete record from lambda/helm-installer/charts.yaml."* ]]
    [[ "$output" == *"EKS add-on lookup failed or returned an invalid version for eks-pod-identity-agent."* ]]
    [ "$(grep -c '^aws eks describe-addon-versions' "$CALLS")" -eq 1 ]

    # A chart the index search knows nothing about (the catalog still
    # describes the previous charts.yaml, so the fake index has no entry).
    printf 'charts:\n  ghost:\n    repo_url: https://example.invalid/charts\n    chart: ghost\n    version: 1.0.0\n' > "$root/lambda/helm-installer/charts.yaml"
    run_scan "$root"
    [[ "$output" == *"INCOMPLETE: Helm chart lookup failed for ghost/ghost."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse Helm chart value images."* ]]
}

@test "the real repository scans end to end against the faked upstreams" {
    # The shapes the scan meets in production. Whether this tree has drift of
    # its own is not this test's business, only that every section runs to a
    # verdict against pins the fakes report as current; a run with every
    # upstream one release newer exercises the same sections' drift paths.
    build_catalog "$REPO_ROOT"
    run_scan "$REPO_ROOT"
    [ "$status" -eq 0 ]
    [[ "$output" == *"=== Checking version consistency ==="* ]]
    [[ "$output" == *"=== Summary ==="* ]]
    [[ "$output" != *"INCOMPLETE:"* ]]
    grep -qx 'scan_complete=true' "$GITHUB_OUTPUT"
    grep -q '^skopeo list-tags --retry-times 3 docker://docker.io/vllm/vllm-openai' "$CALLS"
    grep -q '^curl https://registry.npmjs.org/@aws-sdk%2F' "$CALLS"

    run_scan "$REPO_ROOT" FAKE_DRIFT=1
    [ "$status" -eq 0 ]
    grep -qx 'has_drift=true' "$GITHUB_OUTPUT"
    [[ "$output" == *"Found "*" outdated Python package(s)"* ]]
    local report
    report="$(report_path)"
    grep -qF -- "## Docker Images" "$report"
    grep -qF -- "## Helm Charts" "$report"
    grep -qF -- "## GCO Autopilot Pins" "$report"
}

@test "no drift with every optional section skipped lists each skip in the all-clear summary" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    rm -f "$root/cli/autopilot.py"
    build_catalog "$root"
    run_scan "$root" FAKE_AWS=none FAKE_AWS_CDK=absent FAKE_RUNNER_IMAGES=fail FAKE_ENDOFLIFE=down

    [ "$status" -eq 0 ]
    [[ "$output" == *"cli/autopilot.py not found."* ]]
    [[ "$output" == *"Python release:           (skipped)"* ]]
    [[ "$output" == *"Ruby release:             (skipped)"* ]]
    [[ "$output" == *"GCO autopilot pins:       (skipped)"* ]]
    [[ "$output" == *"No drift was found in completed checks, but the scan is incomplete."* ]]
    grep -qx 'has_drift=false' "$GITHUB_OUTPUT"
    local notes
    notes="$(grep '_Incomplete or skipped checks:' "$GITHUB_STEP_SUMMARY")"
    [[ "$notes" == *"GCO autopilot pins skipped: cli/autopilot.py not found."* ]]
    [[ "$notes" == *"CDK enums skipped: aws-cdk-lib not importable."* ]]
    [[ "$notes" == *"Python release skipped: endoflife.date query failed"* ]]
    [[ "$notes" == *"Ruby release skipped: endoflife.date query failed"* ]]
    [[ "$notes" == *"Runner images skipped: Could not read the actions/runner-images catalog"* ]]
    [[ "$notes" != *"Incomplete checks:"* ]]
}

@test "files that exist but hold no pins, and lookups the registry cannot place, are reported by name" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    printf 'FROM python:3.14.7-slim\n' > "$root/Dockerfile.dev"
    printf 'repos: []\n' > "$root/.pre-commit-config.yaml"
    printf 'not_charts: {}\n' > "$root/lambda/helm-installer/charts.yaml"
    printf 'COMPANION_MCP_SERVERS = ()\n' > "$root/cli/autopilot.py"
    # A digest-pinned reference the tag/digest splitter cannot take apart.
    printf 'AWS_CLI_IMAGE = ":x:2.0@sha256:%s"\n' "$DIGEST_A" > "$root/gco/services/inference_monitor.py"
    build_catalog "$root"
    run_scan "$root" FAKE_SKOPEO=unlisted FAKE_PIP_FILTER=fail

    [ "$status" -eq 0 ]
    [[ "$output" == *"INCOMPLETE: Could not parse or filter pip's outdated-package response."* ]]
    [[ "$output" == *"INCOMPLETE: Pinned tag 1.38.0 is no longer listed by docker.io/library/busybox."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse an immutable image reference from gco/services/inference_monitor.py."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse Helm chart pins from lambda/helm-installer/charts.yaml."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse tooling pins from Dockerfile.dev."* ]]
    [[ "$output" == *"CLAUDE_CODE_VERSION not found in cli/autopilot.py."* ]]
    [[ "$output" == *"INCOMPLETE: Could not parse hook pins from .pre-commit-config.yaml."* ]]
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
}

@test "engine packages that went unhealthy are drift; companions the registry cannot answer for are a skip" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    build_catalog "$root"
    run_scan "$root" FAKE_ENGINE_PACKAGES=deprecated
    [ "$status" -eq 0 ]
    [[ "$output" == *"  - @anthropic-ai/claude-code: deprecated"* ]]
    [[ "$output" == *"  - @openai/codex: deprecated"* ]]
    grep -qF -- "| @anthropic-ai/claude-code (CLAUDE_CODE_VERSION) | \`2.1.252\` | deprecated | [registry](https://www.npmjs.com/package/@anthropic-ai/claude-code) |" "$(report_path)"

    run_scan "$root" FAKE_COMPANIONS=unreachable
    [ "$status" -eq 0 ]
    [[ "$output" == *"Registry lookup failed for awslabs.aws-documentation-mcp-server (pypi); companion liveness incomplete."* ]]
    [[ "$output" == *"GCO autopilot pins:       (skipped)"* ]]
    grep -qx 'scan_complete=false' "$GITHUB_OUTPUT"
}

@test "kind-action steps and workflow python-version pins that disagree are consistency findings" {
    local root="$BATS_TEST_TMPDIR/checkout"
    make_consistent_checkout "$root"
    cat >> "$root/.github/workflows/integration-tests.yml" <<'YAML'
  smoke:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@0000000000000000000000000000000000000000
        with:
          python-version: "3.13"
      - uses: helm/kind-action@0000000000000000000000000000000000000000
        with:
          version: "v0.32.0"
          node_image: "kindest/node:v1.35.0"
YAML
    build_catalog "$root"
    run_scan "$root"

    [ "$status" -eq 0 ]
    [[ "$output" == *"  - kind pins disagree across kind-action steps: v0.33.0,v0.32.0"* ]]
    [[ "$output" == *"  - kind-node pins disagree across kind-action steps: kindest/node:v1.36.4,kindest/node:v1.35.0"* ]]
    [[ "$output" == *"  - python-version pins: 3.13,3.14 (project runtime: 3.14)"* ]]
    grep -qF -- "| kind (across kind-action steps) | v0.33.0,v0.32.0 |" "$(report_path)"
    grep -qF -- "| python-version (CI vs runtime) | CI: 3.13,3.14; runtime: 3.14 |" "$(report_path)"
}
